"""Biweekly pay reconciliation engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from pay_periods import PayPeriod, pay_periods_in_range
from pay_rules import PayConfig, ShiftEarnings, compute_shift_earnings
from schedule import Shift, load_shifts, load_all_shifts


@dataclass
class WeekResult:
    week_num: int           # 1 or 2
    start: date
    end: date
    shifts: list[Shift] = field(default_factory=list)
    earnings: list[ShiftEarnings] = field(default_factory=list)

    @property
    def actual_hours(self) -> float:
        return sum(e.actual_hours for e in self.earnings)

    @property
    def admin_hours(self) -> float:
        return _default_cfg().admin_hours_per_week

    @property
    def total_hours(self) -> float:
        return self.actual_hours + self.admin_hours

    @property
    def evening_hours(self) -> float:
        return sum(e.evening_hours for e in self.earnings)

    @property
    def night_hours(self) -> float:
        return sum(e.night_hours for e in self.earnings)

    @property
    def weekend_hours(self) -> float:
        return sum(e.weekend_hours for e in self.earnings)

    @property
    def holiday_hours(self) -> float:
        return sum(e.holiday_hours for e in self.earnings)

    @property
    def pto_hours(self) -> float:
        """Hours charged as PTO this week: max(0, 36h standard − actual worked)."""
        return max(0.0, _default_cfg().pto_standard_weekly_hours - self.actual_hours)


@dataclass
class PeriodResult:
    period: PayPeriod
    week1: WeekResult
    week2: WeekResult
    cfg: PayConfig

    # Optionally set from a real paystub for comparison
    actual_gross: Optional[float] = None

    @property
    def total_actual_hours(self) -> float:
        return self.week1.actual_hours + self.week2.actual_hours

    @property
    def total_admin_hours(self) -> float:
        return self.week1.admin_hours + self.week2.admin_hours

    @property
    def total_paid_hours(self) -> float:
        return self.total_actual_hours + self.total_admin_hours

    @property
    def regular_hours(self) -> float:
        return min(self.total_paid_hours, self.cfg.ot_threshold)

    @property
    def perdiem_hours(self) -> float:
        return max(0.0, self.total_paid_hours - self.cfg.ot_threshold)

    @property
    def evening_hours(self) -> float:
        return self.week1.evening_hours + self.week2.evening_hours

    @property
    def night_hours(self) -> float:
        return self.week1.night_hours + self.week2.night_hours

    @property
    def weekend_hours(self) -> float:
        return self.week1.weekend_hours + self.week2.weekend_hours

    @property
    def holiday_hours(self) -> float:
        return self.week1.holiday_hours + self.week2.holiday_hours

    def base_pay(self) -> float:
        return self.regular_hours * self.cfg.base_rate

    def evening_pay(self) -> float:
        """Evening differential on regular (non-perdiem) hours."""
        return min(self.evening_hours, self.regular_hours) * self.cfg.evening_rate

    def ot_evening_pay(self) -> float:
        """Evening differential on per-diem (OT) hours at the higher OT rate."""
        if self.perdiem_hours <= 0:
            return 0.0
        # OT evening hours = evening hours that overflow into the per-diem portion.
        # Proportion: if all eve hours were evenly spread, perdiem_hours / total_paid share is OT.
        # Simpler & more accurate: ot_eve_h = max(0, evening_hours - regular_hours)
        ot_eve_h = max(0.0, self.evening_hours - self.regular_hours)
        # Also cap at perdiem_hours (can't have more OT diff hours than OT hours)
        ot_eve_h = min(ot_eve_h, self.perdiem_hours)
        return ot_eve_h * self.cfg.ot_evening_rate

    def night_pay(self) -> float:
        return min(self.night_hours, self.regular_hours) * self.cfg.night_rate

    def weekend_pay(self) -> float:
        """Weekend differential on regular (non-perdiem) hours."""
        return min(self.weekend_hours, self.regular_hours) * self.cfg.weekend_rate

    def ot_weekend_pay(self) -> float:
        """Weekend differential on per-diem hours at the higher OT rate."""
        if self.perdiem_hours <= 0:
            return 0.0
        ot_wknd_h = max(0.0, self.weekend_hours - self.regular_hours)
        ot_wknd_h = min(ot_wknd_h, self.perdiem_hours)
        return ot_wknd_h * self.cfg.ot_weekend_rate

    def holiday_pay(self) -> float:
        return min(self.holiday_hours, self.regular_hours) * self.cfg.base_rate * self.cfg.holiday_pct

    def perdiem_pay(self) -> float:
        return self.perdiem_hours * self.cfg.perdiem_rate

    def total_estimated_gross(self) -> float:
        return (
            self.base_pay()
            + self.evening_pay()
            + self.ot_evening_pay()
            + self.night_pay()
            + self.weekend_pay()
            + self.ot_weekend_pay()
            + self.holiday_pay()
            + self.perdiem_pay()
        )

    def discrepancy(self) -> Optional[float]:
        if self.actual_gross is None:
            return None
        return self.actual_gross - self.total_estimated_gross()

    @property
    def pto_used(self) -> float:
        """Total PTO hours charged this period (sum of both weeks' shortfalls vs 36h)."""
        return self.week1.pto_hours + self.week2.pto_hours


# ---------------------------------------------------------------------------
# PTO balance projection
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dc
from datetime import date as _date

@_dc
class PTOProjectionRow:
    period_label: str
    paydate: _date
    accrual: float
    pto_used: float
    balance: float
    schedule_complete: bool   # False when ≥1 week in the period has no posted shifts


def project_pto(
    results: list["PeriodResult"],
    starting_balance: float,
    starting_after_paydate: _date,
    extra_pto: dict[_date, float] | None = None,
    last_scheduled: _date | None = None,
    project_through: _date | None = None,
) -> list[PTOProjectionRow]:
    """
    Project PTO balance across future pay periods.

    - results: output of reconcile()
    - starting_balance: PTO hours on last known stub
    - starting_after_paydate: the paydate of that stub; project periods AFTER this
    - extra_pto: additional PTO hours on specific paydates (user-stated planned PTO)
    - last_scheduled: last date with a posted shift; weeks beyond this are unscheduled
    - project_through: generate empty periods up to (and including) this date even if
                       no shifts are posted (default: 6 months past the last result)
    """
    if extra_pto is None:
        extra_pto = {}

    cfg = results[0].cfg if results else PayConfig.load()

    # Infer last scheduled date from results
    if last_scheduled is None:
        last_scheduled = max(
            (s.start.date() for r in results for wk in (r.week1, r.week2) for s in wk.shifts),
            default=_date.today(),
        )

    # Default projection horizon: 6 months past last result's paydate (or last_scheduled)
    if project_through is None:
        horizon = max(
            (r.period.paydate for r in results),
            default=last_scheduled,
        )
        from datetime import timedelta as _td
        project_through = horizon + _td(days=180)

    # Build a combined list: results periods + empty continuation periods
    from pay_periods import pay_periods_in_range as _ppr, get_pay_period as _gpp
    from datetime import timedelta as _td

    # All periods from first result through project_through
    if results:
        first_date = results[0].period.start
    else:
        first_date = _date.today()

    all_periods = _ppr(first_date, project_through)

    # Map existing results by period start date
    result_map = {r.period.start: r for r in results}

    balance = starting_balance
    rows: list[PTOProjectionRow] = []

    for pp in all_periods:
        if pp.paydate <= starting_after_paydate:
            continue

        r = result_map.get(pp.start)

        if r is not None:
            # Period has schedule data
            w1_complete = r.week1.start <= last_scheduled
            w2_complete = r.week2.start <= last_scheduled
            pto_used = 0.0
            if w1_complete:
                pto_used += r.week1.pto_hours
            if w2_complete:
                pto_used += r.week2.pto_hours
            schedule_complete = w1_complete and w2_complete
        else:
            # No schedule posted for this period
            pto_used = 0.0
            schedule_complete = False

        pto_used += extra_pto.get(pp.paydate, 0.0)

        balance += cfg.pto_accrual_per_period
        balance -= pto_used

        rows.append(PTOProjectionRow(
            period_label=pp.label,
            paydate=pp.paydate,
            accrual=cfg.pto_accrual_per_period,
            pto_used=pto_used,
            balance=round(balance, 2),
            schedule_complete=schedule_complete,
        ))

    return rows


_cached_cfg: Optional[PayConfig] = None
def _default_cfg() -> PayConfig:
    global _cached_cfg
    if _cached_cfg is None:
        _cached_cfg = PayConfig.load()
    return _cached_cfg


def _week_result(
    week_num: int,
    period_start: date,
    shifts: list[Shift],
    cfg: PayConfig,
) -> WeekResult:
    """Build WeekResult for week_num (1 or 2) of a biweekly period."""
    wk_start = period_start if week_num == 1 else period_start + timedelta(days=7)
    wk_end = wk_start + timedelta(days=6)
    wk_shifts = [s for s in shifts if wk_start <= s.start.date() <= wk_end]
    earnings = [compute_shift_earnings(s, cfg) for s in wk_shifts]
    wr = WeekResult(week_num=week_num, start=wk_start, end=wk_end,
                    shifts=wk_shifts, earnings=earnings)
    return wr


def reconcile(
    shifts: Optional[list[Shift]] = None,
    cfg: Optional[PayConfig] = None,
) -> list[PeriodResult]:
    if shifts is None:
        shifts = load_all_shifts()
    if cfg is None:
        cfg = PayConfig.load()

    if not shifts:
        return []

    periods = pay_periods_in_range(shifts[0].start.date(), shifts[-1].start.date())
    results: list[PeriodResult] = []
    for pp in periods:
        pp_shifts = [s for s in shifts if s.start.date() in pp]
        w1 = _week_result(1, pp.start, pp_shifts, cfg)
        w2 = _week_result(2, pp.start, pp_shifts, cfg)
        results.append(PeriodResult(period=pp, week1=w1, week2=w2, cfg=cfg))
    return results


def print_report(results: list[PeriodResult]) -> None:
    cfg = results[0].cfg if results else PayConfig.load()
    CONF_NOTE = ""

    print("=" * 78)
    print("  HHC PAY RECONCILIATION  —  Scheduled hours (ICS) vs. pay rules")
    print("=" * 78)
    if CONF_NOTE:
        print(CONF_NOTE)
    print()

    total_estimated = 0.0
    for r in results:
        hols = r.period.holidays()
        hol_str = f"  holidays: {', '.join(h.strftime('%b %d') for h in hols)}" if hols else ""
        print(f"┌─ {r.period.label}  paydate {r.period.paydate}{hol_str}")

        for wk in (r.week1, r.week2):
            adm = wk.admin_hours
            adm_str = f"  +{adm:.0f}h admin"
            diff_parts = [f"eve {wk.evening_hours:.1f}h"]
            if wk.night_hours:
                diff_parts.append(f"night {wk.night_hours:.1f}h")
            diff_parts += [f"wknd {wk.weekend_hours:.1f}h", f"hol {wk.holiday_hours:.1f}h"]
            print(f"│  Wk{wk.week_num} ({wk.start.strftime('%b %d')}–{wk.end.strftime('%b %d')}): "
                  f"{wk.actual_hours:.1f}h actual{adm_str}  "
                  f"| {' · '.join(diff_parts)}")

        print(f"│  Totals: {r.total_actual_hours:.1f}h actual "
              f"+ {r.total_admin_hours:.1f}h admin "
              f"= {r.total_paid_hours:.1f}h paid  │  "
              f"OT: {r.perdiem_hours:.1f}h @ ${cfg.perdiem_rate:.0f}/hr")
        print(f"│  Base pay:     ${r.base_pay():>9,.2f}  ({r.regular_hours:.1f}h × ${cfg.base_rate})")
        if r.evening_hours:
            print(f"│  Evening diff: ${r.evening_pay():>9,.2f}  ({r.evening_hours:.1f}h × ${cfg.evening_rate})")
        if r.night_hours:
            print(f"│  Night diff:   ${r.night_pay():>9,.2f}  ({r.night_hours:.1f}h × ${cfg.night_rate})")
        if r.weekend_hours:
            print(f"│  Weekend diff: ${r.weekend_pay():>9,.2f}  ({r.weekend_hours:.1f}h × ${cfg.weekend_rate})")
        if r.holiday_hours:
            print(f"│  Holiday diff: ${r.holiday_pay():>9,.2f}  ({r.holiday_hours:.1f}h × 50%)")
        if r.perdiem_hours:
            print(f"│  Per diem OT:  ${r.perdiem_pay():>9,.2f}  ({r.perdiem_hours:.1f}h × ${cfg.perdiem_rate:.0f})")
        est = r.total_estimated_gross()
        total_estimated += est
        print(f"│  ─────────────────────────────")
        if r.actual_gross is not None:
            disc = r.discrepancy()
            disc_str = f"  Δ ${disc:+,.2f}" if disc is not None else ""
            print(f"│  ESTIMATED GROSS: ${est:>9,.2f}   ACTUAL GROSS: ${r.actual_gross:>9,.2f}{disc_str}")
        else:
            print(f"│  ESTIMATED GROSS: ${est:>9,.2f}   (enter actual paystub amount to compare)")
        print()

    print(f"{'=' * 78}")
    print(f"  TOTAL ESTIMATED GROSS (all periods): ${total_estimated:,.2f}")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    results = reconcile()
    print_report(results)
