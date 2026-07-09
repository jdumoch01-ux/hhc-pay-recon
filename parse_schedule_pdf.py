"""
Parse ShiftAdmin monthly calendar PDFs into Shift objects.

Each PDF is a one-page calendar grid like:

    My Schedule - March 2025
    Sun Mon Tue Wed Thu Fri Sat
    23 24 25 26 27 28 1
    CHHAPP 1p-1a  CHHAPP 1p-1a  …
    Dumoch        1300-1800 Dumoch
    2  3  4  5  …

Uses pdfplumber word positions (x-coordinate) to map each piece of text to
the correct day-of-week column, then infers actual dates from the month/year
header + week-Sunday arithmetic.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pdfplumber

from schedule import Shift


# ---------------------------------------------------------------------------
# Shift-type defaults  →  (start_h, start_m, end_h, end_m, next_day)
# ---------------------------------------------------------------------------
SHIFT_DEFAULTS: dict[str, tuple[int, int, int, int, bool]] = {
    "1p-1a":   (13, 0,  1, 0, True),
    "11a-11p": (11, 0, 23, 0, False),
    "6a-4p":   (6,  0, 16, 0, False),
}

MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3,    "April": 4,
    "May": 5,     "June": 6,     "July": 7,      "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# HHMM-HHMM  (custom time override, e.g. "1300-1800")
_CUSTOM_RE = re.compile(r'^(\d{2})(\d{2})-(\d{2})(\d{2})$')

# Words to silently ignore in content rows
_IGNORE = {
    "Dumoch", "Open", "Pending", "My", "Picked", "Up", "Posted",
    "Vacation", "Split", "Modified", "Times", "TCPDF",
    "Powered", "by", "CHHAPP",   # "CHHAPP" handled explicitly below
}

# Rows whose text contains any of these prefixes are footer/legend rows — skip entirely
_FOOTER_KW = {
    "Totals", "Schedules", "Printed:", "Powered", "Open", "1/1",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_custom_time(s: str) -> Optional[tuple[int, int, int, int, bool]]:
    """Parse 'HHMM-HHMM' → (sh, sm, eh, em, next_day). Returns None if no match."""
    m = _CUSTOM_RE.match(s)
    if not m:
        return None
    sh, sm = int(m.group(1)), int(m.group(2))
    eh, em = int(m.group(3)), int(m.group(4))
    # Overnight if end < start numerically
    next_day = (eh * 60 + em) < (sh * 60 + sm)
    return (sh, sm, eh, em, next_day)


def _make_shift(
    shift_date: date, sh: int, sm: int, eh: int, em: int, next_day: bool,
    label: str, local_tz: ZoneInfo, uid_extra: str = "",
) -> Shift:
    start_dt = datetime(shift_date.year, shift_date.month, shift_date.day,
                        sh, sm, tzinfo=local_tz)
    end_date = shift_date + timedelta(days=1) if next_day else shift_date
    end_dt   = datetime(end_date.year, end_date.month, end_date.day,
                        eh, em, tzinfo=local_tz)
    uid = f"pdf-{shift_date.isoformat()}-{sh:02d}{sm:02d}{uid_extra}"
    return Shift(
        uid=uid,
        start=start_dt,
        end=end_dt,
        summary=f"CHHAPP {label}",
        facility="CHH",
        shift_label=label,
        shift_type="APP",
        is_split_segment=False,
    )


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def _build_col_bounds(col_centers: list[float]) -> list[tuple[float, float]]:
    bounds = []
    for i, cx in enumerate(col_centers):
        left  = (col_centers[i-1] + cx) / 2 if i > 0  else 0.0
        right = (cx + col_centers[i+1]) / 2 if i < 6 else 9999.0
        bounds.append((left, right))
    return bounds


def _word_col(w: dict, bounds: list[tuple[float, float]]) -> int:
    xc = (w['x0'] + w['x1']) / 2
    for i, (l, r) in enumerate(bounds):
        if l <= xc < r:
            return i
    return -1


# ---------------------------------------------------------------------------
# Row grouping
# ---------------------------------------------------------------------------

def _group_rows(words: list[dict], above_y: float) -> list[tuple[float, list[dict]]]:
    """Group words into horizontal rows (y-bucket = round to nearest 4px)."""
    buckets: dict[float, list[dict]] = defaultdict(list)
    for w in words:
        if w['top'] <= above_y + 2:
            continue
        key = round(w['top'] / 4) * 4
        buckets[key].append(w)
    return sorted(
        ((y, sorted(ws, key=lambda w: w['x0'])) for y, ws in buckets.items()),
        key=lambda t: t[0],
    )


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_schedule_pdf(pdf_path: str, local_tz: ZoneInfo) -> list[Shift]:
    """Parse one ShiftAdmin monthly calendar PDF. Returns Shift list."""

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(x_tolerance=3, y_tolerance=3)

    if not words:
        return []

    # ---- 1. Find month / year from title line ----
    year: Optional[int] = None
    month: Optional[int] = None
    for i, w in enumerate(words):
        if w['text'] in MONTH_NAMES and i + 1 < len(words):
            try:
                yr = int(words[i + 1]['text'])
                if 2000 <= yr <= 2100:
                    year  = yr
                    month = MONTH_NAMES[w['text']]
                    break
            except ValueError:
                pass

    if year is None or month is None:
        return []

    # ---- 2. Find "Sun Mon Tue Wed Thu Fri Sat" header → column centres ----
    col_centers: Optional[list[float]] = None
    header_y: float = 0.0

    for i, w in enumerate(words):
        if w['text'] != 'Sun':
            continue
        # Collect up to 7 DOW words near this y
        cands: list[dict] = []
        j = i
        while j < len(words) and len(cands) < 7:
            wj = words[j]
            if abs(wj['top'] - w['top']) > 8:
                break
            if wj['text'] in DOW_NAMES:
                cands.append(wj)
            j += 1
        if len(cands) == 7:
            header_y = w['top']
            col_centers = [(c['x0'] + c['x1']) / 2 for c in cands]
            break

    if col_centers is None:
        return []

    col_bounds = _build_col_bounds(col_centers)

    # ---- 3. Determine first calendar Sunday ----
    first = date(year, month, 1)
    days_back = (first.weekday() + 1) % 7   # Mon=0…Sun=6 → 1..7 days back
    week1_sunday = first - timedelta(days=days_back)

    # ---- 4. Walk rows, accumulate per-week cell data ----
    all_rows = _group_rows(words, header_y)

    # cell_labels[col] = shift_label string (e.g. "1p-1a")
    # cell_times[col]  = (sh, sm, eh, em, next_day) override, or None
    # cell_pending_chhapp = cols where CHHAPP was seen but label is on next row (11a-11p style)
    cell_labels:         dict[int, str]                          = {}
    cell_times:          dict[int, tuple[int,int,int,int,bool]]  = {}
    cell_pending_chhapp: set[int]                                = set()
    date_map:            dict[int, date]                         = {}
    week_count   = 0
    finished     = False
    shifts: list[Shift] = []

    def _flush() -> None:
        for col, lbl in cell_labels.items():
            if col not in date_map:
                continue
            sd = date_map[col]
            if col in cell_times:
                sh, sm, eh, em, nd = cell_times[col]
            elif lbl in SHIFT_DEFAULTS:
                sh, sm, eh, em, nd = SHIFT_DEFAULTS[lbl]
            else:
                continue
            shifts.append(_make_shift(sd, sh, sm, eh, em, nd, lbl, local_tz))

    def _is_date_row(rw: list[dict]) -> bool:
        nums = [w for w in rw if w['text'].isdigit() and 1 <= int(w['text']) <= 31]
        if len(nums) < 3:
            return False
        cols = [_word_col(w, col_bounds) for w in nums]
        # All should map to distinct valid columns
        valid = [c for c in cols if c >= 0]
        return len(valid) == len(set(valid)) and len(valid) >= 3

    for _y, row_words in all_rows:
        if finished:
            break

        texts = [w['text'] for w in row_words]

        # Detect footer/legend rows
        if any(t in _FOOTER_KW for t in texts):
            finished = True
            break

        # ---- Date row → start new week ----
        if _is_date_row(row_words):
            _flush()
            cell_labels         = {}
            cell_times          = {}
            cell_pending_chhapp = set()
            date_map            = {}

            week_sunday = week1_sunday + timedelta(days=7 * week_count)
            week_count += 1

            for w in row_words:
                if w['text'].isdigit() and 1 <= int(w['text']) <= 31:
                    c = _word_col(w, col_bounds)
                    if c >= 0:
                        actual = week_sunday + timedelta(days=c)
                        # Sanity: day number should match
                        if actual.day == int(w['text']):
                            date_map[c] = actual
                        # else: mismatch (overflow date from prior/next month) → skip
            continue

        # ---- Content row (shift labels + custom times) ----
        if not date_map:
            continue

        i = 0
        while i < len(row_words):
            w = row_words[i]
            col_i = _word_col(w, col_bounds)

            if w['text'] == 'CHHAPP':
                # Style A: "CHHAPP <label>" both in the same column on this row
                if i + 1 < len(row_words):
                    nw = row_words[i + 1]
                    nw_col = _word_col(nw, col_bounds)
                    if nw['text'] in SHIFT_DEFAULTS and nw_col == col_i:
                        if col_i >= 0:
                            cell_labels[col_i] = nw['text']
                            cell_pending_chhapp.discard(col_i)
                        i += 2
                        continue
                # Style B: label appears on the following row (e.g. 11a-11p)
                if col_i >= 0:
                    cell_pending_chhapp.add(col_i)
                i += 1
                continue

            # Shift label appearing alone — resolves a Style-B pending CHHAPP
            if w['text'] in SHIFT_DEFAULTS:
                if col_i >= 0 and col_i in cell_pending_chhapp:
                    cell_labels[col_i] = w['text']
                    cell_pending_chhapp.discard(col_i)
                i += 1
                continue

            # Custom time override (e.g. "1300-1800", "2015-0100")
            ct = _parse_custom_time(w['text'])
            if ct is not None and col_i >= 0:
                cell_times[col_i] = ct

            i += 1

    # Flush final week
    _flush()

    return shifts


# ---------------------------------------------------------------------------
# Multi-PDF loader
# ---------------------------------------------------------------------------

def load_historical_shifts(
    pdf_paths: list[str],
    tz_name: str = "America/New_York",
) -> list[Shift]:
    """
    Parse multiple ShiftAdmin monthly PDFs and return a deduplicated,
    sorted list of Shift objects.
    """
    local_tz = ZoneInfo(tz_name)
    all_shifts: list[Shift] = []

    for path in pdf_paths:
        try:
            parsed = parse_schedule_pdf(path, local_tz)
            all_shifts.extend(parsed)
        except Exception as exc:
            print(f"Warning: could not parse {path}: {exc}")

    # Deduplicate by (start, end) — same month appears in two PDFs sometimes
    seen: set[tuple[datetime, datetime]] = set()
    unique: list[Shift] = []
    for s in all_shifts:
        key = (s.start, s.end)
        if key not in seen:
            seen.add(key)
            unique.append(s)

    return sorted(unique, key=lambda s: s.start)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from zoneinfo import ZoneInfo

    if len(sys.argv) < 2:
        print("Usage: python parse_schedule_pdf.py schedule.pdf [...]")
        sys.exit(1)

    tz = ZoneInfo("America/New_York")
    all_s = load_historical_shifts(sys.argv[1:], "America/New_York")

    print(f"Parsed {len(all_s)} shifts\n")
    prev_month = None
    for s in all_s:
        mo = s.start.strftime("%B %Y")
        if mo != prev_month:
            print(f"\n── {mo} ──")
            prev_month = mo
        day_label = s.start.strftime("%a %b %d")
        start_str = s.start.strftime("%H:%M")
        end_str   = s.end.strftime("%H:%M")
        end_day   = " +1" if s.end.date() > s.start.date() else ""
        print(f"  {day_label}  {start_str}–{end_str}{end_day}  {s.shift_label}  ({s.hours:.2f}h)")
