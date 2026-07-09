"""
Stub-based reconciliation — three analysis modules:

1. audit_pto()         — was PTO deducted correctly based on hours actually worked?
2. audit_differentials() — were differentials (eve/wknd/holiday/OT) paid correctly?
3. pto_plan()          — forward PTO balance projection anchored to latest stub

All three take a list[StubData] as the source of truth for actual hours/pay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from parse_stub import StubData, WeekSummary
from pay_rules import PayConfig


# ---------------------------------------------------------------------------
# 1. PTO audit
# ---------------------------------------------------------------------------

@dataclass
class PtoAuditRow:
    advice_date:    Optional[date]
    period_label:   str
    stub_balance:   float          # PTO balance on this stub
    stub_pto_used:  float          # PTO hours shown in current-period earnings
    # Per week breakdown
    weeks:          list[dict]     # [{begin, actual_h, pto_charged, expected_pto, diff}]
    balance_matches: bool          # does running balance agree with accrual model?
    running_balance: float         # our calculated running balance at this point


def audit_pto(
    stubs: list[StubData],
    cfg: PayConfig,
    opening_balance: float = 0.0,
    opening_before_date: Optional[date] = None,
) -> list[PtoAuditRow]:
    """
    For each stub period:
      - Show actual hours worked per week (from stub earnings lines)
      - Show PTO hours charged (from stub earnings lines)
      - Show what the 36h rule would predict
      - Flag discrepancies
      - Track running PTO balance using accrual + actual deductions

    opening_balance: PTO balance on the stub BEFORE the first stub in the list
                     (i.e. the previous stub's balance).
    """
    rows: list[PtoAuditRow] = []
    running = opening_balance
    STANDARD = cfg.pto_standard_weekly_hours
    ACCRUAL  = cfg.pto_accrual_per_period

    for stub in stubs:
        running += ACCRUAL
        running -= stub.pto_hours_used

        week_rows = []
        for w in stub.weeks():
            expected_pto = max(0.0, STANDARD - w.worked_hours)
            diff = w.pto_hours - expected_pto
            week_rows.append({
                "week_begin":    w.week_begin,
                "week_end":      w.week_end,
                "actual_worked": round(w.worked_hours, 2),
                "pto_charged":   round(w.pto_hours, 2),
                "expected_pto":  round(expected_pto, 2),
                "diff":          round(diff, 2),   # + = more PTO than expected
            })

        balance_matches = abs(running - stub.pto_balance) < 0.5

        period_str = (
            f"{stub.period_start.strftime('%b %d')}–{stub.period_end.strftime('%b %d, %Y')}"
            if stub.period_start and stub.period_end else
            str(stub.advice_date or "unknown")
        )

        rows.append(PtoAuditRow(
            advice_date=stub.advice_date,
            period_label=period_str,
            stub_balance=stub.pto_balance,
            stub_pto_used=stub.pto_hours_used,
            weeks=week_rows,
            balance_matches=balance_matches,
            running_balance=round(running, 2),
        ))

    return rows


# ---------------------------------------------------------------------------
# 2. Differential audit
# ---------------------------------------------------------------------------

@dataclass
class DiffLine:
    label:       str
    hours_paid:  float    # hours the stub shows differential was paid on
    rate_paid:   float    # rate that was actually used
    amt_paid:    float    # dollars actually paid
    hours_owed:  float    # hours differential SHOULD apply to (engine calc)
    rate_owed:   float    # correct rate
    amt_owed:    float    # dollars that should have been paid
    delta:       float    # amt_owed - amt_paid  (positive = underpaid)


@dataclass
class DiffAuditRow:
    advice_date:    Optional[date]
    period_label:   str
    lines:          list[DiffLine]
    total_paid:     float
    total_owed:     float
    underpayment:   float   # total_owed - total_paid  (positive = underpaid)
    has_ot:         bool


def _hours_from_stub(stub: StubData, category: str) -> float:
    return round(sum(e.hours for e in stub.earnings if e.category == category), 2)


def _amt_from_stub(stub: StubData, category: str) -> float:
    return round(sum(e.current_amt for e in stub.earnings if e.category == category), 2)


def audit_differentials(
    stubs: list[StubData],
    cfg: PayConfig,
) -> list[DiffAuditRow]:
    """
    For each stub, compare what was paid in differentials to what the pay rules say
    should have been paid — using the stub's actual hours as the source of truth
    (not the ICS schedule).

    The engine's differential logic:
      - Evening (regular):  min(eve_h, reg_h) × $6.72
      - Evening (OT):       max(0, eve_h − reg_h) × $8.40
      - Weekend (regular):  min(wknd_h, reg_h) × $8.40
      - Weekend (OT):       max(0, wknd_h − reg_h) × $14.00
      - Holiday:            min(hol_h, reg_h) × ($94.37 × 50%)

    Where:
      reg_h  = min(total_paid_hours, 80)
      ot_h   = max(0, total_paid_hours − 80)
    """
    rows: list[DiffAuditRow] = []

    for stub in stubs:
        # Reconstruct total paid hours from stub (exclude PTO, edu, differentials)
        total_actual = sum(e.hours for e in stub.earnings
                           if e.category == "base"
                           and "over standard" not in e.description.lower()
                           and e.category not in ("pto", "edu"))
        total_actual += sum(e.hours for e in stub.earnings
                            if "over standard" in e.description.lower()
                            and e.category == "base")
        total_ot     = _hours_from_stub(stub, "ot_base")
        total_pto    = stub.pto_hours_used
        total_edu    = sum(e.hours for e in stub.earnings
                           if "education" in e.description.lower())
        # Admin: not shown on stub, add it back
        admin_weeks = len({e.week_begin for e in stub.earnings if e.week_begin}) or 2
        admin_total = cfg.admin_hours_per_week * admin_weeks

        total_paid = total_actual + total_ot + total_pto + total_edu + admin_total
        reg_h      = min(total_paid, cfg.ot_threshold)
        ot_h       = max(0.0, total_paid - cfg.ot_threshold)

        # Hours from stub earnings lines
        eve_h_paid   = _hours_from_stub(stub, "eve")
        eve_ot_paid  = _hours_from_stub(stub, "ot_eve")
        wknd_h_paid  = _hours_from_stub(stub, "wknd")
        wknd_ot_paid = _hours_from_stub(stub, "ot_wknd")
        hol_h_paid   = _hours_from_stub(stub, "holiday")

        # What engine says should have been paid
        # (using stub-derived regular/OT split)
        eve_h_owed      = min(eve_h_paid + eve_ot_paid, reg_h)
        eve_ot_h_owed   = max(0.0, (eve_h_paid + eve_ot_paid) - reg_h) if ot_h > 0 else 0.0
        wknd_h_owed     = min(wknd_h_paid + wknd_ot_paid, reg_h)
        wknd_ot_h_owed  = max(0.0, (wknd_h_paid + wknd_ot_paid) - reg_h) if ot_h > 0 else 0.0
        hol_h_owed      = min(hol_h_paid, reg_h)

        diff_lines: list[DiffLine] = []

        def _line(label, h_paid, rate_paid, h_owed, rate_owed) -> DiffLine:
            return DiffLine(
                label=label,
                hours_paid=h_paid,
                rate_paid=rate_paid,
                amt_paid=round(h_paid * rate_paid, 2),
                hours_owed=h_owed,
                rate_owed=rate_owed,
                amt_owed=round(h_owed * rate_owed, 2),
                delta=round(h_owed * rate_owed - h_paid * rate_paid, 2),
            )

        if eve_h_paid or eve_h_owed:
            diff_lines.append(_line("Evening (regular)", eve_h_paid,
                                    cfg.evening_rate, eve_h_owed, cfg.evening_rate))
        if eve_ot_paid or eve_ot_h_owed:
            diff_lines.append(_line("Evening (OT)", eve_ot_paid,
                                    cfg.ot_evening_rate, eve_ot_h_owed, cfg.ot_evening_rate))
        if wknd_h_paid or wknd_h_owed:
            diff_lines.append(_line("Weekend (regular)", wknd_h_paid,
                                    cfg.weekend_rate, wknd_h_owed, cfg.weekend_rate))
        if wknd_ot_paid or wknd_ot_h_owed:
            diff_lines.append(_line("Weekend (OT)", wknd_ot_paid,
                                    cfg.ot_weekend_rate, wknd_ot_h_owed, cfg.ot_weekend_rate))
        if hol_h_paid or hol_h_owed:
            diff_lines.append(_line("Holiday", hol_h_paid,
                                    cfg.base_rate * cfg.holiday_pct,
                                    hol_h_owed,
                                    cfg.base_rate * cfg.holiday_pct))

        total_paid_diff  = round(sum(ln.amt_paid for ln in diff_lines), 2)
        total_owed_diff  = round(sum(ln.amt_owed for ln in diff_lines), 2)
        underpayment     = round(total_owed_diff - total_paid_diff, 2)

        period_str = (
            f"{stub.period_start.strftime('%b %d')}–{stub.period_end.strftime('%b %d, %Y')}"
            if stub.period_start and stub.period_end else
            str(stub.advice_date or "unknown")
        )

        rows.append(DiffAuditRow(
            advice_date=stub.advice_date,
            period_label=period_str,
            lines=diff_lines,
            total_paid=total_paid_diff,
            total_owed=total_owed_diff,
            underpayment=underpayment,
            has_ot=ot_h > 0,
        ))

    return rows


def total_underpayment(diff_rows: list[DiffAuditRow]) -> float:
    """Sum of all differential underpayments across all periods."""
    return round(sum(r.underpayment for r in diff_rows), 2)


# ---------------------------------------------------------------------------
# 3. PTO forward plan
# ---------------------------------------------------------------------------

@dataclass
class PtoPlanRow:
    period_label: str
    paydate:      date
    accrual:      float
    pto_used:     float
    balance:      float
    note:         str = ""


def pto_plan(
    stubs: list[StubData],
    future_results,           # list[PeriodResult] from reconcile()
    planned_pto: dict[date, float] | None = None,  # paydate → planned hours
    last_scheduled: Optional[date] = None,
    project_through: Optional[date] = None,
) -> tuple[list[PtoPlanRow], list[PtoPlanRow]]:
    """
    Returns (historical_rows, future_rows).

    Historical: one row per stub showing balance trajectory.
    Future: projected from the latest stub balance using accrual rate,
            deducting ICS-schedule-derived PTO and any planned_pto.
    """
    from reconcile import project_pto  # avoid circular at module level

    # Historical rows from stubs
    hist: list[PtoPlanRow] = []
    cfg = PayConfig.load()
    ACCRUAL = cfg.pto_accrual_per_period

    running = stubs[0].pto_balance - ACCRUAL + stubs[0].pto_hours_used if stubs else 0.0
    for stub in stubs:
        running += ACCRUAL
        running -= stub.pto_hours_used
        period_str = (
            f"{stub.period_start.strftime('%b %d')}–{stub.period_end.strftime('%b %d, %Y')}"
            if stub.period_start and stub.period_end else str(stub.advice_date)
        )
        hist.append(PtoPlanRow(
            period_label=period_str,
            paydate=stub.advice_date or stub.period_end or date.min,
            accrual=ACCRUAL,
            pto_used=stub.pto_hours_used,
            balance=round(running, 2),
            note="" if abs(running - stub.pto_balance) < 0.5 else
                  f"⚠️ stub shows {stub.pto_balance:.2f}h",
        ))

    # Latest verified balance
    if stubs:
        latest_balance = stubs[-1].pto_balance
        latest_paydate = stubs[-1].advice_date or date.min
    else:
        return hist, []

    # Future projection
    proj_rows = project_pto(
        future_results,
        starting_balance=latest_balance,
        starting_after_paydate=latest_paydate,
        extra_pto=planned_pto or {},
        last_scheduled=last_scheduled,
        project_through=project_through,
    )

    future: list[PtoPlanRow] = [
        PtoPlanRow(
            period_label=r.period_label,
            paydate=r.paydate,
            accrual=r.accrual,
            pto_used=r.pto_used,
            balance=r.balance,
            note="" if r.schedule_complete else "schedule not posted",
        )
        for r in proj_rows
    ]

    return hist, future


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from parse_stub import parse_stub_pdf
    from pay_rules import PayConfig
    from reconcile import reconcile

    if len(sys.argv) < 2:
        print("Usage: python reconcile_stubs.py stub1.pdf [stub2.pdf ...]")
        sys.exit(1)

    cfg = PayConfig.load()
    all_stubs: list[StubData] = []
    for path in sys.argv[1:]:
        all_stubs.extend(parse_stub_pdf(path))
    all_stubs.sort(key=lambda s: (s.advice_date or date.min))
    print(f"Loaded {len(all_stubs)} stub(s)\n")

    print("=" * 70)
    print("DIFFERENTIAL AUDIT")
    print("=" * 70)
    diff_rows = audit_differentials(all_stubs, cfg)
    for r in diff_rows:
        if not r.lines:
            continue
        flag = "⚠️ " if abs(r.underpayment) > 1 else "✅ "
        print(f"\n{flag}{r.period_label}  (advice {r.advice_date})")
        for ln in r.lines:
            delta_str = f"${ln.delta:+,.2f}" if abs(ln.delta) > 0.01 else "✅"
            print(f"  {ln.label:<22}  paid {ln.hours_paid:.1f}h × ${ln.rate_paid:.2f} = ${ln.amt_paid:,.2f}"
                  f"  |  owed {ln.hours_owed:.1f}h × ${ln.rate_owed:.2f} = ${ln.amt_owed:,.2f}"
                  f"  Δ {delta_str}")
        print(f"  Period underpayment: ${r.underpayment:+,.2f}")
    print(f"\nTOTAL DIFFERENTIAL UNDERPAYMENT: ${total_underpayment(diff_rows):+,.2f}")

    print()
    print("=" * 70)
    print("PTO AUDIT")
    print("=" * 70)
    pto_rows = audit_pto(all_stubs, cfg)
    for r in pto_rows:
        bal_flag = "✅" if r.balance_matches else f"⚠️  (calc {r.running_balance:.2f}h)"
        print(f"\n{r.period_label}  |  stub balance {r.stub_balance:.2f}h {bal_flag}")
        print(f"  PTO used this period: {r.stub_pto_used:.2f}h")
        for w in r.weeks:
            diff_str = f"  Δ{w['diff']:+.1f}h" if abs(w['diff']) > 0.5 else ""
            print(f"  Week {w['week_begin']}: worked {w['actual_worked']:.1f}h  "
                  f"PTO charged {w['pto_charged']:.1f}h  "
                  f"(36h rule expects {w['expected_pto']:.1f}h){diff_str}")
