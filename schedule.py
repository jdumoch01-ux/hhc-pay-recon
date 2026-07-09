"""Fetch and parse the ShiftAdmin ICS feed into Shift records."""
from __future__ import annotations

import ssl
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass(frozen=True)
class Shift:
    uid: str
    start: datetime          # tz-aware, local
    end: datetime            # tz-aware, local
    summary: str             # e.g. "CHHAPP 1p-1a"
    facility: str
    shift_label: str         # e.g. "1p-1a"
    shift_type: str          # e.g. "APP"
    is_split_segment: bool

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def _unfold(text: str) -> list[str]:
    """ICS spec: lines starting with space/tab continue the previous line."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _parse_dt(value: str, local_tz: ZoneInfo) -> datetime:
    # ICS UTC form: 20251107T180000Z
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(local_tz)
    # Floating local time fallback
    dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return dt.replace(tzinfo=local_tz)


def _kv(line: str) -> tuple[str, str]:
    # Strip params off the key (e.g. DTSTART;TZID=...:value)
    key_part, _, val = line.partition(":")
    key = key_part.split(";", 1)[0]
    return key, val


def _field_from_description(desc: str, label: str) -> str:
    # DESCRIPTION uses literal "\n" sequences as separators
    for seg in desc.split("\\n"):
        seg = seg.strip()
        if seg.startswith(label):
            return seg[len(label):].strip()
    return ""


def parse_ics(text: str, local_tz: ZoneInfo) -> list[Shift]:
    lines = _unfold(text)
    shifts: list[Shift] = []
    cur: dict[str, str] = {}
    in_event = False
    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
        elif line == "END:VEVENT":
            in_event = False
            desc = cur.get("DESCRIPTION", "")
            facility = _field_from_description(desc, "Facility:")
            shift_label = _field_from_description(desc, "Shift:")
            shift_type = _field_from_description(desc, "Shift Type:")
            is_split = "Split Shift" in desc
            shifts.append(
                Shift(
                    uid=cur.get("UID", ""),
                    start=_parse_dt(cur["DTSTART"], local_tz),
                    end=_parse_dt(cur["DTEND"], local_tz),
                    summary=cur.get("SUMMARY", ""),
                    facility=facility,
                    shift_label=shift_label,
                    shift_type=shift_type,
                    is_split_segment=is_split,
                )
            )
        elif in_event:
            k, v = _kv(line)
            if k in {"UID", "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION"}:
                cur[k] = v
    return sorted(shifts, key=lambda s: s.start)


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def fetch_ics(url: str) -> str:
    url = url.replace("webcal://", "https://", 1)
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=30, context=ctx) as resp:
        return resp.read().decode("utf-8")


def load_shifts(ics_url: str | None = None) -> list[Shift]:
    """Load shifts from the ICS feed only (rolling ~8-month window)."""
    cfg = load_config()["schedule"]
    url = ics_url or cfg["ics_url"]
    text = fetch_ics(url)
    return parse_ics(text, ZoneInfo(cfg["local_tz"]))


def load_all_shifts(ics_url: str | None = None) -> list[Shift]:
    """
    Load shifts from both the ICS feed (current/future) and any historical
    ShiftAdmin PDF exports found in config [schedule] pdf_dir.
    Deduplicates by start time — ICS entries take precedence.
    """
    from pathlib import Path
    cfg_sched = load_config()["schedule"]
    local_tz = ZoneInfo(cfg_sched["local_tz"])

    # --- ICS shifts ---
    ics_shifts = load_shifts(ics_url=ics_url)
    ics_keys = {s.start: s for s in ics_shifts}  # start → Shift (ICS preferred)

    # --- Historical PDF shifts ---
    pdf_dir = cfg_sched.get("pdf_dir", "")
    pdf_shifts: list[Shift] = []
    if pdf_dir:
        from parse_schedule_pdf import load_historical_shifts
        pdfs = sorted(Path(pdf_dir).glob("schedule_*.pdf"))
        if pdfs:
            pdf_shifts = load_historical_shifts([str(p) for p in pdfs],
                                                cfg_sched["local_tz"])

    # Merge: PDF shifts that don't conflict with ICS (by start time)
    combined = list(ics_shifts)
    for s in pdf_shifts:
        if s.start not in ics_keys:
            combined.append(s)

    return sorted(combined, key=lambda s: s.start)


def main() -> None:
    shifts = load_shifts()
    print(f"Loaded {len(shifts)} shifts")
    print(f"First: {shifts[0].start.isoformat()} → {shifts[0].end.isoformat()}  {shifts[0].summary}")
    print(f"Last:  {shifts[-1].start.isoformat()} → {shifts[-1].end.isoformat()}  {shifts[-1].summary}")
    split = sum(1 for s in shifts if s.is_split_segment)
    print(f"Split-shift segments: {split}")
    print(f"Total scheduled hours: {sum(s.hours for s in shifts):.1f}")
    print()
    print("First 5 shifts (local time):")
    for s in shifts[:5]:
        flag = " [split]" if s.is_split_segment else ""
        print(f"  {s.start.strftime('%a %Y-%m-%d %H:%M')} → {s.end.strftime('%H:%M')}  {s.shift_label} ({s.hours:.1f}h){flag}")


if __name__ == "__main__":
    main()
