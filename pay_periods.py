"""HHC bi-weekly pay period definitions, derived from the 2026 HHC Payroll Calendar."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Confirmed from HHC 2026 Bi-Weekly Payroll Calendar (Hartford HealthCare).
# Periods run Sunday through Saturday; paydate is the Friday 6 days after period end.
_ANCHOR = date(2025, 12, 14)  # Sunday — first visible period start on the calendar

# HHC observed holidays from the 2026 payroll calendar (teal-highlighted dates).
# Confirm Good Friday (Apr 3) with your HR if holiday pay applies.
HHC_HOLIDAYS: frozenset[date] = frozenset({
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    date(2026,  1,  1),  # New Year's Day
    date(2026,  1, 19),  # MLK Day
    date(2026,  2, 16),  # Presidents' Day
    date(2026,  4,  3),  # Good Friday (confirm with HR)
    date(2026,  5, 25),  # Memorial Day
    date(2026,  7,  4),  # Independence Day
    date(2026,  9,  7),  # Labor Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    date(2027,  1,  1),  # New Year's Day
})


@dataclass(frozen=True)
class PayPeriod:
    start: date      # Sunday
    end: date        # Saturday (start + 13 days)
    paydate: date    # Friday (end + 6 days)

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def label(self) -> str:
        return f"{self.start.strftime('%b %d')}–{self.end.strftime('%b %d, %Y')}"

    def holidays(self) -> list[date]:
        return [d for d in HHC_HOLIDAYS if d in self]


def get_pay_period(d: date) -> PayPeriod:
    """Return the PayPeriod that contains date d."""
    n = (d - _ANCHOR).days // 14          # works for dates before anchor (floor division)
    start = _ANCHOR + timedelta(days=14 * n)
    end = start + timedelta(days=13)
    paydate = end + timedelta(days=6)
    return PayPeriod(start=start, end=end, paydate=paydate)


def pay_periods_in_range(first: date, last: date) -> list[PayPeriod]:
    """All PayPeriods that overlap the [first, last] date range."""
    pp = get_pay_period(first)
    result: list[PayPeriod] = []
    while pp.start <= last:
        result.append(pp)
        pp = get_pay_period(pp.start + timedelta(days=14))
    return result


if __name__ == "__main__":
    from schedule import load_shifts
    shifts = load_shifts()
    first_shift_date = shifts[0].start.date()
    last_shift_date = shifts[-1].start.date()
    periods = pay_periods_in_range(first_shift_date, last_shift_date)
    print(f"Pay periods covering schedule ({first_shift_date} → {last_shift_date}):\n")
    for pp in periods:
        pp_shifts = [s for s in shifts if s.start.date() in pp]
        hours = sum(s.hours for s in pp_shifts)
        hols = pp.holidays()
        hol_str = f"  holidays: {', '.join(str(h) for h in hols)}" if hols else ""
        print(f"  {pp.label}  paydate {pp.paydate}  |  {len(pp_shifts):2d} shifts  {hours:5.1f}h{hol_str}")
