"""Parse HHC PDF pay stubs (single or multi-stub PDFs) using pdfplumber."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pdfplumber

# ---------------------------------------------------------------------------
# Rate → category
# OT-evening and regular-weekend share $8.40; disambiguated by description.
# $91.72 = pre-Oct-2025 base rate; $94.37 = post-Oct-2025 base rate.
# Holiday appears as $47.18 or $47.19 depending on rounding.
# ---------------------------------------------------------------------------
_RATE_TO_CAT: dict[float, str] = {
    91.72: "base",     # pre-Oct 2025 base rate
    94.37: "base",
    6.72:  "eve",
    8.40:  "wknd",      # may be overridden to ot_eve by description
    14.00: "ot_wknd",
    110.00: "ot_base",
    47.18: "holiday",   # actual stub rounding variant
    47.19: "holiday",
}
_OT_EVE_KW  = {"ot eve", "ot evening", "overtime eve", "pd eve"}
_OT_WKND_KW = {"ot wknd", "ot weekend", "overtime wknd", "pd wknd"}


def _classify(description: str, rate: float) -> str:
    dl = description.lower()
    # Description-based overrides take priority (PTO/edu must not land in "base")
    if "paid time off" in dl or dl.strip() == "pto":
        return "pto"
    if "education" in dl:
        return "edu"
    if rate == 8.40:
        if any(k in dl for k in _OT_EVE_KW):
            return "ot_eve"
        # "Prem - Evening" at $8.40 is the OT-evening rate
        if "evening" in dl:
            return "ot_eve"
        return "wknd"
    return _RATE_TO_CAT.get(rate, "other")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EarningsLine:
    description: str
    week_begin:  Optional[date]
    week_end:    Optional[date]
    rate:        float
    hours:       float
    current_amt: float
    ytd_amt:     float
    category:    str


@dataclass
class WeekSummary:
    """Actual hours in one payroll week, reconstructed from stub earnings lines."""
    week_begin: date
    week_end:   date
    regular_hours: float = 0.0   # Regular + Over Standard (both at base rate)
    ot_hours:      float = 0.0   # Per diem ($110)
    pto_hours:     float = 0.0
    edu_hours:     float = 0.0   # Education
    other_hours:   float = 0.0

    @property
    def total_hours(self) -> float:
        return self.regular_hours + self.ot_hours + self.pto_hours + self.edu_hours + self.other_hours

    @property
    def worked_hours(self) -> float:
        """Hours actually at work (excludes PTO/education)."""
        return self.regular_hours + self.ot_hours


@dataclass
class StubData:
    advice_number: str = ""
    advice_date:   Optional[date] = None
    period_start:  Optional[date] = None
    period_end:    Optional[date] = None
    pay_date:      Optional[date] = None     # same as advice_date for HHC
    earnings:      list[EarningsLine] = field(default_factory=list)
    raw_gross:     float = 0.0
    ytd_gross:     float = 0.0               # Total Gross YTD from stub summary line
    pto_balance:   float = 0.0               # balance shown on this stub
    raw_text:      str = ""

    # ---- derived ---------------------------------------------------------

    @property
    def computed_gross(self) -> float:
        return round(sum(e.current_amt for e in self.earnings), 2)

    @property
    def total_gross(self) -> float:
        return self.raw_gross or self.computed_gross

    def amount_by_cat(self, *cats: str) -> float:
        return round(sum(e.current_amt for e in self.earnings if e.category in cats), 2)

    @property
    def base_pay(self)    -> float: return self.amount_by_cat("base")
    @property
    def evening_pay(self) -> float: return self.amount_by_cat("eve", "ot_eve")
    @property
    def weekend_pay(self) -> float: return self.amount_by_cat("wknd", "ot_wknd")
    @property
    def holiday_pay(self) -> float: return self.amount_by_cat("holiday")
    @property
    def ot_base_pay(self) -> float: return self.amount_by_cat("ot_base")
    @property
    def ot_diff_pay(self) -> float: return self.amount_by_cat("ot_eve", "ot_wknd")

    def hours_by_cat(self, *cats: str) -> float:
        return round(sum(e.hours for e in self.earnings if e.category in cats), 2)

    @property
    def regular_hours(self) -> float:
        return self.hours_by_cat("base")

    @property
    def ot_hours(self) -> float:
        return self.hours_by_cat("ot_base")

    @property
    def pto_hours_used(self) -> float:
        return round(sum(
            e.hours for e in self.earnings
            if e.category == "pto"
            or "paid time off" in e.description.lower()
            or e.description.lower().strip() == "pto"
        ), 2)

    def weeks(self) -> list[WeekSummary]:
        """Build WeekSummary objects from earnings lines."""
        by_week: dict[date, WeekSummary] = {}
        for e in self.earnings:
            if e.week_begin is None:
                continue
            if e.week_begin not in by_week:
                by_week[e.week_begin] = WeekSummary(
                    week_begin=e.week_begin,
                    week_end=e.week_end or e.week_begin,
                )
            ws = by_week[e.week_begin]
            dl = e.description.lower()
            # Description checks first — PTO/edu must not land in regular_hours
            if e.category in ("pto",) or "paid time off" in dl or dl.strip() == "pto":
                ws.pto_hours += e.hours
            elif e.category in ("edu",) or "education" in dl:
                ws.edu_hours += e.hours
            elif e.category == "base" and "over standard" not in dl:
                ws.regular_hours += e.hours
            elif "over standard" in dl and e.category == "base":
                ws.regular_hours += e.hours
            elif e.category == "ot_base":
                ws.ot_hours += e.hours
            elif e.category not in ("eve", "wknd", "ot_eve", "ot_wknd", "holiday"):
                ws.other_hours += e.hours
        return sorted(by_week.values(), key=lambda w: w.week_begin)


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------
_DATE_FMTS = ["%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]


def _parse_date(s: str) -> Optional[date]:
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


_DATE_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b")


def _find_dates(text: str) -> list[date]:
    out = []
    for m in _DATE_RE.finditer(text):
        d = _parse_date(m.group(1))
        if d:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Per-page stub parser
# ---------------------------------------------------------------------------

_MONEY_RE  = r"-?\$?[\d,]+\.\d{2}"
_HOURS_RE  = r"\d{1,4}(?:\.\d{1,4})?"
_GROSS_KW  = ("total gross", "gross pay", "gross earnings", "current gross")
_ADVICE_RE = re.compile(r"advice\s*(?:#|number|num|no\.?)?:?\s*0*(\d+)", re.IGNORECASE)
_ADVICE_DATE_RE = re.compile(r"advice\s*date:?\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_PTO_BAL_RE = re.compile(r"pto\s+(\d{1,4}(?:\.\d{1,4})?)", re.IGNORECASE)
_PERIOD_RE  = re.compile(
    r"(?:pay\s*period|period\s*begin|period\s*start|period:?)\s*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})"
    r"(?:\s*[-–to]+\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4}))?",
    re.IGNORECASE,
)

# Earnings line: Description  WeekBegin  WeekEnd  Rate  Hours  Current  [YTD]
# The HHC Lawson stub puts week begin/end dates inside the description area.
_EARN_RE = re.compile(
    r"^(.+?)\s+"                                        # description
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"  # week_begin  week_end
    r"(" + _MONEY_RE + r")\s+"                          # rate
    r"(" + _HOURS_RE + r")\s+"                          # hours
    r"(" + _MONEY_RE + r")"                             # current
    r"(?:\s+(" + _MONEY_RE + r"))?",                    # optional YTD
    re.IGNORECASE,
)

# Date-range lines with no rate/hours: "Lump Sum 06/14 06/27 8622.06 [8622.06]"
# Dates may appear as MM/DD (Lump Sum) or MM/DD/YYYY (other bonuses).
_DATE_ONLY_EARN_RE = re.compile(
    r"^(.+?)\s+"
    r"(\d{2}/\d{2}(?:/\d{4})?)\s+(\d{2}/\d{2}(?:/\d{4})?)\s+"
    r"(" + _MONEY_RE + r")"                             # current amount
    r"(?:\s+(" + _MONEY_RE + r"))?",                   # optional YTD earnings
    re.IGNORECASE,
)

# Fallback: line has 3+ money values but no dates (YTD-only rows, totals, etc.)
_FALLBACK_EARN_RE = re.compile(
    r"^(.+?)\s+"
    r"(" + _HOURS_RE + r")\s+"
    r"(" + _MONEY_RE + r")\s+"
    r"(" + _MONEY_RE + r")",
    re.IGNORECASE,
)

_SKIP_DESCS = {
    "description", "earnings type", "earn code", "earn type",
    "hours and earnings", "hours", "rate", "current", "ytd",
}
_STOP_KW = {"deduction", "deductions", "net pay",
            "direct deposit", "before-tax", "after-tax",
            "employer paid", "time off"}


def _clean(s: str) -> float:
    s = s.replace(",", "").replace("$", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return 0.0


def _parse_page(text: str) -> StubData:
    """Parse one page of text into a StubData."""
    stub = StubData(raw_text=text)
    lines = text.splitlines()

    # ---- header fields ----
    m = _ADVICE_RE.search(text)
    if m:
        stub.advice_number = m.group(1)

    m = _ADVICE_DATE_RE.search(text)
    if m:
        stub.advice_date = _parse_date(m.group(1))
        stub.pay_date    = stub.advice_date

    m = _PTO_BAL_RE.search(text)
    if m:
        try:
            stub.pto_balance = float(m.group(1))
        except ValueError:
            pass

    m = _PERIOD_RE.search(text)
    if m:
        stub.period_start = _parse_date(m.group(1))
        if m.group(2):
            stub.period_end = _parse_date(m.group(2))

    # If period not found via label, infer from earnings line dates
    # (first and last week dates seen)
    all_week_dates: list[date] = []

    # ---- earnings lines ----
    in_earnings = False
    raw_gross = 0.0
    ytd_gross_direct = 0.0
    earnings: list[EarningsLine] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        ll = stripped.lower()

        if re.search(r"\bearnings\b", ll) and "year" not in ll:
            in_earnings = True
            continue

        if in_earnings and any(kw in ll for kw in _STOP_KW):
            in_earnings = False

        if any(kw in ll for kw in _GROSS_KW):
            nums = re.findall(_MONEY_RE, stripped)
            if nums:
                raw_gross = _clean(nums[0])
            continue

        # Bottom summary row: "YTD 127,980.71 108,783.84 37,773.37 ..."
        # First value = Total Gross YTD (the authoritative number from the stub).
        if re.match(r"^ytd\s", ll) and not in_earnings:
            nums = re.findall(_MONEY_RE, stripped)
            if len(nums) >= 3 and ytd_gross_direct == 0.0:
                ytd_gross_direct = _clean(nums[0])
            continue

        # Bottom summary row: "Current 17,040.85 15,280.07 3,182.39 ..."
        # First value = Total Gross current period. Only set if _GROSS_KW didn't fire.
        if re.match(r"^current\s", ll) and not in_earnings:
            nums = re.findall(_MONEY_RE, stripped)
            if len(nums) >= 3 and raw_gross == 0.0:
                raw_gross = _clean(nums[0])
            continue

        # Try full earnings-line pattern (has week begin/end dates)
        m = _EARN_RE.match(stripped)
        if m:
            desc     = m.group(1).strip()
            wb       = _parse_date(m.group(2))
            we       = _parse_date(m.group(3))
            rate     = _clean(m.group(4))
            hours    = _clean(m.group(5))
            current  = _clean(m.group(6))
            # The stub is two-column: pdfplumber merges each earnings row with the
            # tax row at the same y-position. trailing[0] is YTD earnings; later
            # values are tax amounts which we ignore.
            trailing = re.findall(_MONEY_RE, stripped[m.end():])
            ytd      = _clean(trailing[0]) if trailing else 0.0

            if desc.lower() in _SKIP_DESCS:
                continue

            if wb:
                all_week_dates.append(wb)
            if we:
                all_week_dates.append(we)

            cat = _classify(desc, rate)
            earnings.append(EarningsLine(
                description=desc, week_begin=wb, week_end=we,
                rate=rate, hours=hours, current_amt=current,
                ytd_amt=ytd, category=cat,
            ))
            continue

        # Lines with date range but no rate/hours (e.g. Lump Sum, bonuses, corrections)
        if in_earnings:
            m_dto = _DATE_ONLY_EARN_RE.match(stripped)
            if m_dto:
                desc    = m_dto.group(1).strip()
                wb      = _parse_date(m_dto.group(2))
                we      = _parse_date(m_dto.group(3))
                current = _clean(m_dto.group(4))
                ytd     = _clean(m_dto.group(5)) if m_dto.group(5) else 0.0
                if desc.lower() not in _SKIP_DESCS and current != 0:
                    if wb: all_week_dates.append(wb)
                    if we: all_week_dates.append(we)
                    earnings.append(EarningsLine(
                        description=desc, week_begin=wb, week_end=we,
                        rate=0.0, hours=0.0, current_amt=current,
                        ytd_amt=ytd, category="other",
                    ))
                continue

        # YTD-only rows (no week dates, just description + hours + current + YTD)
        if in_earnings and stripped and "0.00" not in stripped[:5]:
            m2 = _FALLBACK_EARN_RE.match(stripped)
            if m2:
                desc    = m2.group(1).strip()
                hours   = _clean(m2.group(2))
                rate_or_current = _clean(m2.group(3))
                ytd     = _clean(m2.group(4))
                if desc.lower() in _SKIP_DESCS:
                    continue
                if rate_or_current > 0:
                    # When hours==0.0, rate_or_current is YTD hours, not current dollars.
                    current_amt = 0.0 if hours == 0.0 else rate_or_current
                    earnings.append(EarningsLine(
                        description=desc, week_begin=None, week_end=None,
                        rate=0.0, hours=hours, current_amt=current_amt,
                        ytd_amt=ytd, category="other",
                    ))

    stub.earnings  = earnings
    stub.raw_gross = raw_gross
    stub.ytd_gross = ytd_gross_direct

    # Infer period from week dates if not found above
    if all_week_dates:
        if stub.period_start is None:
            stub.period_start = min(all_week_dates)
        if stub.period_end is None:
            stub.period_end = max(all_week_dates)

    return stub


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_stub_pdf(file_like) -> list[StubData]:
    """
    Parse a PDF that may contain one or more HHC pay stubs (one per page).
    Returns a list sorted by advice_date ascending.
    """
    stubs: list[StubData] = []
    with pdfplumber.open(file_like) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            if not text.strip():
                continue
            # Skip continuation pages (no Advice # found = likely page 2 of a long stub)
            if not _ADVICE_RE.search(text):
                # Append text to previous stub if present
                if stubs:
                    stubs[-1].raw_text += "\n" + text
                continue
            stub = _parse_page(text)
            if stub.earnings or stub.pto_balance:
                stubs.append(stub)

    stubs.sort(key=lambda s: (s.advice_date or date.min))
    return stubs


def parse_stub(file_like) -> StubData:
    """Parse the first (or only) stub from a PDF."""
    results = parse_stub_pdf(file_like)
    if not results:
        # Fallback: treat whole PDF as one stub
        with pdfplumber.open(file_like) as pdf:
            text = "\n".join(p.extract_text(x_tolerance=3, y_tolerance=3) or "" for p in pdf.pages)
        return _parse_page(text)
    return results[0]


# ---------------------------------------------------------------------------
# CLI debug
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parse_stub.py <stub.pdf>")
        sys.exit(1)

    all_stubs = parse_stub_pdf(sys.argv[1])
    print(f"Found {len(all_stubs)} stub(s)\n")
    for i, s in enumerate(all_stubs, 1):
        print(f"{'='*60}")
        print(f"Stub {i}: Advice #{s.advice_number}  Date {s.advice_date}")
        print(f"  Period:      {s.period_start} → {s.period_end}")
        print(f"  PTO balance: {s.pto_balance:.2f}h")
        print(f"  Gross:       ${s.total_gross:,.2f}")
        print(f"  PTO used:    {s.pto_hours_used:.2f}h")
        print()
        print(f"  {'Description':<30} {'WkBegin':>12} {'Rate':>7} {'Hrs':>6} {'Current':>10} {'Cat'}")
        print(f"  {'-'*80}")
        for e in s.earnings:
            wb = str(e.week_begin) if e.week_begin else "—"
            print(f"  {e.description:<30} {wb:>12} {e.rate:>7.2f} {e.hours:>6.2f} "
                  f"${e.current_amt:>9,.2f}  {e.category}")
        print()
        for w in s.weeks():
            print(f"  Week {w.week_begin}: reg={w.regular_hours:.1f}h  ot={w.ot_hours:.1f}h  "
                  f"pto={w.pto_hours:.1f}h  total={w.total_hours:.1f}h")
        print()
