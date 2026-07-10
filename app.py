"""APP Pay Reconciliation — Streamlit dashboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from datetime import date

from parse_stub import StubData, parse_stub, parse_stub_pdf
from pay_rules import PayConfig
from reconcile import PeriodResult, reconcile
from reconcile_stubs import audit_pto, audit_differentials, total_underpayment

import auth

st.set_page_config(
    page_title="APP Pay Reconciliation",
    page_icon="💵",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Stub persistence helpers (Supabase-backed)
# ---------------------------------------------------------------------------

def _stub_to_earnings_list(stub: "StubData") -> list[dict]:
    return [
        {
            "description": e.description,
            "week_begin":  e.week_begin.isoformat() if e.week_begin else None,
            "week_end":    e.week_end.isoformat()   if e.week_end   else None,
            "rate":        e.rate,
            "hours":       e.hours,
            "current_amt": e.current_amt,
            "ytd_amt":     e.ytd_amt,
            "category":    e.category,
        }
        for e in stub.earnings
    ]


def _stub_from_db_row(row: dict) -> "StubData":
    from parse_stub import EarningsLine
    def _d(s): return date.fromisoformat(s) if s else None
    earnings = [
        EarningsLine(
            description=e["description"],
            week_begin=_d(e.get("week_begin")),
            week_end=_d(e.get("week_end")),
            rate=e["rate"],
            hours=e["hours"],
            current_amt=e["current_amt"],
            ytd_amt=e.get("ytd_amt", 0.0),
            category=e["category"],
        )
        for e in (row.get("earnings_json") or [])
    ]
    return StubData(
        advice_number=row.get("advice_number", ""),
        period_start=_d(row.get("period_start")),
        period_end=_d(row.get("period_end")),
        raw_gross=row.get("raw_gross", 0.0),
        ytd_gross=row.get("ytd_gross", 0.0),
        pto_balance=row.get("pto_balance", 0.0),
        earnings=earnings,
    )


@st.cache_data(show_spinner=False)
def _load_stubs_cached(user_id: str, _v: int = 2) -> dict[str, "StubData"]:
    """Returns {period_start_iso: StubData} for all uploaded stubs."""
    rows = auth.load_stubs(user_id)
    return {row["period_start"]: _stub_from_db_row(row) for row in rows if row.get("period_start")}


def _save_stub_to_db(user_id: str, stub: "StubData", period_start_iso: str) -> tuple[bool, str]:
    return auth.save_stub(
        user_id=user_id,
        period_start=period_start_iso,
        period_end=stub.period_end.isoformat() if stub.period_end else None,
        raw_gross=stub.raw_gross or stub.computed_gross,
        pto_balance=stub.pto_balance,
        advice_number=stub.advice_number,
        earnings=_stub_to_earnings_list(stub),
        ytd_gross=stub.ytd_gross,
    )


# ---------------------------------------------------------------------------
# Data loading (cached per session — ICS fetch only happens once)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Fetching schedule from ShiftAdmin…")
def _load_results(ics_url: str, base_rate: float, accrual_per_period: float) -> list[PeriodResult]:
    from schedule import load_all_shifts
    cfg = PayConfig.load(base_rate_override=base_rate, accrual_override=accrual_per_period)
    shifts = load_all_shifts(ics_url=ics_url)
    return reconcile(shifts=shifts, cfg=cfg)


@st.cache_data(show_spinner=False)
def _load_cfg(base_rate: float, accrual_per_period: float) -> PayConfig:
    return PayConfig.load(
        base_rate_override=base_rate,
        accrual_override=accrual_per_period,
    )


# ---------------------------------------------------------------------------
# Delta helpers
# ---------------------------------------------------------------------------

def _icon(delta: float) -> str:
    a = abs(delta)
    if a <= 1.0:   return "✅"
    if a <= 10.0:  return "⚠️"
    return "❌"


def _delta_css(val: str) -> str:
    """For st.dataframe cell styling — returns CSS colour string."""
    try:
        v = float(str(val).replace(",", "").replace("+", "").replace("$", ""))
        a = abs(v)
        if a <= 1.0:  return "color: #2ecc71; font-weight:600"
        if a <= 10.0: return "color: #f39c12; font-weight:600"
        return "color: #e74c3c; font-weight:600"
    except ValueError:
        return ""


def _fmt_delta(delta: float) -> str:
    """Format a pay delta with an icon prefix so sign is visible without CSS."""
    a = abs(delta)
    if a <= 1.0:   icon = "✅"
    elif a <= 10.0: icon = "⚠️"
    else:           icon = "🔴"
    return f"{icon} ${delta:+,.2f}"


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _build_summary_df(
    results: list[PeriodResult],
    stubs: dict[str, "StubData"],
    cfg: PayConfig,
) -> pd.DataFrame:
    rows = []
    for r in results:
        label  = r.period.label
        est    = r.total_estimated_gross()
        stub        = stubs.get(r.period.start.isoformat())
        actual      = stub.total_gross    if stub else None
        comparable  = stub.recurring_gross if stub else None
        rows.append({
            "Period":     label,
            "Paydate":    r.period.paydate.strftime("%b %d, %Y"),
            "Shifts":     len(r.week1.shifts) + len(r.week2.shifts),
            "Paid Hrs":   f"{r.total_paid_hours:.1f}",
            "OT Hrs":     f"{r.perdiem_hours:.1f}" if r.perdiem_hours else "—",
            "Holidays":   ", ".join(h.strftime("%b %d") for h in r.period.holidays()) or "—",
            "Est. Gross": f"${est:,.2f}",
            "Stub Gross": f"${actual:,.2f}" if actual else "—",
            "Δ":          _fmt_delta(comparable - est) if comparable is not None else "—",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Detail panel — engine breakdown
# ---------------------------------------------------------------------------

def _engine_breakdown_df(r: PeriodResult, cfg: PayConfig) -> pd.DataFrame:
    rows = []

    def _row(label, hours, rate, amount, note=""):
        return {"Line Item": label, "Hours": f"{hours:.1f}",
                "Rate": f"${rate:.2f}", "Amount": f"${amount:,.2f}", "Note": note}

    rows.append(_row(
        "Base Pay", r.regular_hours, cfg.base_rate, r.base_pay(),
        f"{r.regular_hours:.1f}h × ${cfg.base_rate}",
    ))
    if r.evening_hours:
        rows.append(_row(
            "Evening Diff", r.evening_hours, cfg.evening_rate, r.evening_pay(),
            "15:00–23:00, ≥4h/shift",
        ))
    if r.night_hours:
        rows.append(_row(
            "Night Diff", r.night_hours, cfg.night_rate, r.night_pay(),
            "23:00–07:00, any hours",
        ))
    if r.ot_evening_pay():
        rows.append(_row(
            "OT Evening Diff", max(0.0, r.evening_hours - r.regular_hours),
            cfg.ot_evening_rate, r.ot_evening_pay(),
            "OT hours in eve window × $8.40",
        ))
    if r.weekend_hours:
        rows.append(_row(
            "Weekend Diff", r.weekend_hours, cfg.weekend_rate, r.weekend_pay(),
            "Fri 23:00–Sun 23:00, ≥4h/shift",
        ))
    if r.ot_weekend_pay():
        rows.append(_row(
            "OT Weekend Diff", max(0.0, r.weekend_hours - r.regular_hours),
            cfg.ot_weekend_rate, r.ot_weekend_pay(),
            "OT hours in wknd window × $14.00",
        ))
    if r.holiday_hours:
        rows.append(_row(
            "Holiday Diff", r.holiday_hours, cfg.base_rate * cfg.holiday_pct,
            r.holiday_pay(), "+50% base on holidays",
        ))
    if r.perdiem_hours:
        rows.append(_row(
            "Per Diem OT", r.perdiem_hours, cfg.perdiem_rate, r.perdiem_pay(),
            f"Hours > {cfg.ot_threshold:.0f}h biweekly",
        ))

    total = r.total_estimated_gross()
    rows.append({
        "Line Item": "TOTAL ESTIMATED GROSS",
        "Hours": "",
        "Rate": "",
        "Amount": f"${total:,.2f}",
        "Note": "",
    })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Detail panel — comparison table (when actual stub is known)
# ---------------------------------------------------------------------------

def _comparison_df(r: PeriodResult, stub: StubData, cfg: PayConfig) -> pd.DataFrame:
    rows = []

    def _row(label, engine, actual, note=""):
        delta = actual - engine
        return {
            " ":          _icon(delta),
            "Line Item":  label,
            "Engine":     f"${engine:,.2f}",
            "Stub":       f"${actual:,.2f}",
            "Δ":          f"${delta:+,.2f}",
            "Note":       note,
        }

    rows.append(_row("Base Pay",     r.base_pay(),     stub.base_pay,
                     f"{r.regular_hours:.1f}h × ${cfg.base_rate}"))
    if r.evening_hours or stub.evening_pay:
        eve_engine = r.evening_pay() + r.ot_evening_pay()
        rows.append(_row("Evening Diff (all)",  eve_engine,  stub.evening_pay,
                         f"engine: {r.evening_hours:.1f}h regular + OT"))
    if r.night_hours or stub.night_pay:
        rows.append(_row("Night Diff", r.night_pay(), stub.night_pay,
                         f"engine: {r.night_hours:.1f}h × ${cfg.night_rate}"))
    if r.weekend_hours or stub.weekend_pay:
        wknd_engine = r.weekend_pay() + r.ot_weekend_pay()
        rows.append(_row("Weekend Diff (all)",  wknd_engine,  stub.weekend_pay,
                         f"engine: {r.weekend_hours:.1f}h regular + OT"))
    if r.holiday_hours or stub.holiday_pay:
        rows.append(_row("Holiday Diff",  r.holiday_pay(),  stub.holiday_pay,
                         f"{r.holiday_hours:.1f}h × 50%"))
    if r.perdiem_hours or stub.ot_base_pay:
        rows.append(_row("Per Diem OT",   r.perdiem_pay(),  stub.ot_base_pay,
                         f"{r.perdiem_hours:.1f}h × ${cfg.perdiem_rate:.0f}"))

    other = stub.amount_by_cat("other")
    if other:
        rows.append({
            " ":         "ℹ️",
            "Line Item": "Other / unrecognised lines",
            "Engine":    "—",
            "Stub":      f"${other:,.2f}",
            "Δ":         "—",
            "Note":      "See raw text below",
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Match stub to engine period
# ---------------------------------------------------------------------------

def _match_stub(results: list[PeriodResult], stub: StubData) -> Optional[PeriodResult]:
    if stub.period_start is None:
        return None
    for r in results:
        if r.period.start <= stub.period_start <= r.period.end:
            return r
        if stub.period_end and abs((stub.period_end - r.period.end).days) <= 1:
            return r
    return None


# ---------------------------------------------------------------------------
# Discrepancy explanation + per-period draft email
# ---------------------------------------------------------------------------

def _show_discrepancy_and_email(
    r: PeriodResult,
    stub: StubData,
    recurring_gross: float,
    delta: float,
    est: float,
    cfg: PayConfig,
) -> None:
    label = r.period.label
    paydate_str = r.period.paydate.strftime("%B %d, %Y")

    # Build a plain-language list of short-paid components.
    problem_lines: list[str] = []

    base_d = stub.base_pay - r.base_pay()
    if base_d < -1.0:
        problem_lines.append(
            f"Base pay — expected ${r.base_pay():,.2f}, received ${stub.base_pay:,.2f} "
            f"(${abs(base_d):,.2f} short, {r.regular_hours:.1f}h × ${cfg.base_rate:.2f})"
        )

    if r.evening_hours or stub.evening_pay:
        eve_eng = round(r.evening_pay() + r.ot_evening_pay(), 2)
        eve_d = stub.evening_pay - eve_eng
        if eve_d < -1.0:
            problem_lines.append(
                f"Evening differential — expected ${eve_eng:,.2f}, received ${stub.evening_pay:,.2f} "
                f"(${abs(eve_d):,.2f} short, {r.evening_hours:.1f}h in 15:00–23:00 window)"
            )

    if r.night_hours or stub.night_pay:
        night_eng = r.night_pay()
        night_d = stub.night_pay - night_eng
        if night_d < -1.0:
            problem_lines.append(
                f"Night differential — expected ${night_eng:,.2f}, received ${stub.night_pay:,.2f} "
                f"(${abs(night_d):,.2f} short, {r.night_hours:.1f}h × ${cfg.night_rate})"
            )

    if r.weekend_hours or stub.weekend_pay:
        wknd_eng = round(r.weekend_pay() + r.ot_weekend_pay(), 2)
        wknd_d = stub.weekend_pay - wknd_eng
        if wknd_d < -1.0:
            problem_lines.append(
                f"Weekend differential — expected ${wknd_eng:,.2f}, received ${stub.weekend_pay:,.2f} "
                f"(${abs(wknd_d):,.2f} short, {r.weekend_hours:.1f}h Fri 23:00–Sun 23:00)"
            )

    if r.holiday_hours or stub.holiday_pay:
        hol_d = stub.holiday_pay - r.holiday_pay()
        if hol_d < -1.0:
            problem_lines.append(
                f"Holiday pay — expected ${r.holiday_pay():,.2f}, received ${stub.holiday_pay:,.2f} "
                f"(${abs(hol_d):,.2f} short, {r.holiday_hours:.1f}h × 50% holiday rate)"
            )

    if r.perdiem_hours or stub.ot_base_pay:
        pd_d = stub.ot_base_pay - r.perdiem_pay()
        if pd_d < -1.0:
            problem_lines.append(
                f"Per diem OT — expected ${r.perdiem_pay():,.2f}, received ${stub.ot_base_pay:,.2f} "
                f"(${abs(pd_d):,.2f} short, {r.perdiem_hours:.1f}h over threshold)"
            )

    total_gross = stub.total_gross or recurring_gross

    st.divider()
    st.warning(
        f"**Discrepancy detected: ${abs(delta):,.2f} short** — "
        f"engine estimate ${est:,.2f} vs ${recurring_gross:,.2f} recurring pay "
        f"(stub total ${total_gross:,.2f}; lump sums and bonuses excluded from comparison)."
    )

    if problem_lines:
        st.markdown("**Where the gap is:**")
        for line in problem_lines:
            st.markdown(f"- {line}")
    else:
        st.markdown(
            "_The gross totals differ but no single line item is short by more than $1. "
            "Check the comparison table above for rounding or classification differences._"
        )

    breakdown_text = (
        "\n".join(f"  • {ln}" for ln in problem_lines)
        if problem_lines
        else "  (see attached pay stub for details)"
    )

    email_text = f"""To: Payroll Department

Subject: Pay Discrepancy — {label} (pay date {paydate_str})

Hi,

I'm writing to request a review of my pay for the period {label} (pay date {paydate_str}).

After comparing my pay stub against my scheduled hours, I'm showing a shortfall of approximately ${abs(delta):,.2f} in base pay and differentials. Based on my records I should have received ${est:,.2f} in recurring pay, but my stub shows ${recurring_gross:,.2f} (total stub gross ${total_gross:,.2f}, which includes separate one-time payments).

Where the gap appears:
{breakdown_text}

I've attached my pay stub for reference. Could you please review and let me know if a correction is warranted?

Thank you,

[Your name]
[Your department / employee ID]"""

    with st.expander("📧 Draft Email to Payroll"):
        st.code(email_text, language=None)


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------

def _show_detail(
    r: PeriodResult,
    existing_stub: Optional[StubData],
    cfg: PayConfig,
    results: list[PeriodResult],
    user: dict,
) -> None:
    label = r.period.label
    est   = r.total_estimated_gross()
    period_start_iso = r.period.start.isoformat()

    hols = r.period.holidays()
    hol_str = f"  ·  holidays: {', '.join(h.strftime('%b %d') for h in hols)}" if hols else ""
    col_title, col_remove = st.columns([5, 1])
    col_title.subheader(f"{label}  ·  paydate {r.period.paydate}{hol_str}")

    if existing_stub:
        confirm_key = f"confirm_remove_{period_start_iso}"
        if col_remove.button("🗑️ Remove stub", key=f"remove_{period_start_iso}"):
            st.session_state[confirm_key] = True
        if st.session_state.get(confirm_key):
            st.warning(f"Remove the saved stub for **{label}**? You can re-upload it later.")
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Yes, remove", key=f"remove_yes_{period_start_iso}", type="primary"):
                ok, err = auth.delete_stub(user["id"], period_start_iso)
                if ok:
                    st.session_state.pop(confirm_key, None)
                    st.cache_data.clear()
                    st.rerun()
                else:
                    st.error(f"Remove failed: {err}")
            if c2.button("Cancel", key=f"remove_no_{period_start_iso}"):
                st.session_state.pop(confirm_key, None)
                st.rerun()

    # ---- Hours summary ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Actual Hours",   f"{r.total_actual_hours:.1f}h")
    c2.metric("Admin Hours",    f"{r.total_admin_hours:.1f}h")
    c3.metric("Total Paid",     f"{r.total_paid_hours:.1f}h")
    c4.metric("OT / Per Diem",  f"{r.perdiem_hours:.1f}h")

    # ---- Stub upload / replace ----
    uploaded = st.file_uploader(
        "Replace stub for this period" if existing_stub else "Upload PDF stub for this period",
        type=["pdf"],
        key=f"up_{label}",
        help="Drop the PDF pay stub for this period to get a line-by-line comparison.",
    )

    stub: Optional[StubData] = existing_stub
    if uploaded is not None:
        try:
            with st.spinner("Parsing PDF…"):
                parsed = parse_stub_pdf(uploaded)
                new_stub = parsed[0] if parsed else None
            if new_stub:
                ok, err = _save_stub_to_db(user["id"], new_stub, period_start_iso)
                if ok:
                    st.cache_data.clear()
                    stub = new_stub
                else:
                    st.error(f"Save failed: {err}")
        except Exception as exc:
            st.error(f"PDF parse error: {exc}")

    if stub is not None:
        if stub.period_start is not None:
            matched = _match_stub(results, stub)
            if matched is None or matched.period.label != label:
                st.warning(
                    f"Stub dates ({stub.period_start} → {stub.period_end}) "
                    f"don't match the selected period ({r.period.start} → {r.period.end}). "
                    "Check that you uploaded the right stub."
                )

        display_gross  = stub.total_gross or stub.computed_gross
        comparable     = stub.recurring_gross or stub.computed_gross
        delta          = comparable - est
        accuracy       = (1 - abs(delta) / comparable) * 100 if comparable else 100.0

        # ---- Discrepancy summary first (if underpaid) ----
        if delta < -1.0:
            _show_discrepancy_and_email(r, stub, comparable, delta, est, cfg)
        else:
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("Engine Estimate",      f"${est:,.2f}")
            cc2.metric("Stub Gross",           f"${display_gross:,.2f}")
            cc3.metric("Δ (recurring pay)",    f"${delta:+,.2f}",
                       delta_color="normal" if delta >= 0 else "inverse",
                       help="Compares recurring pay only — excludes lump sums and bonuses.")
            cc4.metric("Accuracy", f"{accuracy:.1f}%")

        # ---- Supporting detail (always in expanders) ----
        with st.expander("Line-by-line comparison"):
            comp_df = _comparison_df(r, stub, cfg)
            st.dataframe(
                comp_df.style.map(_delta_css, subset=["Δ"]),
                use_container_width=True,
                hide_index=True,
            )

        with st.expander("Engine estimate breakdown"):
            eng_df = _engine_breakdown_df(r, cfg)
            st.dataframe(
                eng_df.style.apply(
                    lambda col: ["font-weight:700" if i == len(eng_df)-1 else "" for i in range(len(col))],
                    axis=0,
                ),
                use_container_width=True,
                hide_index=True,
            )

        with st.expander("Raw extracted text / parsed lines"):
            st.text(stub.raw_text[:5000] if stub.raw_text else "(none)")
            if stub.earnings:
                st.dataframe(pd.DataFrame([
                    {"Desc": e.description, "Hrs": e.hours, "Rate": e.rate,
                     "Current": e.current_amt, "YTD": e.ytd_amt, "Cat": e.category}
                    for e in stub.earnings
                ]), use_container_width=True, hide_index=True)

    else:
        # No stub yet — show engine estimate so user knows what to expect
        with st.expander("Engine estimate breakdown", expanded=True):
            eng_df = _engine_breakdown_df(r, cfg)
            st.dataframe(
                eng_df.style.apply(
                    lambda col: ["font-weight:700" if i == len(eng_df)-1 else "" for i in range(len(col))],
                    axis=0,
                ),
                use_container_width=True,
                hide_index=True,
            )

    # ---- Week-by-week shift detail ----
    with st.expander("Week-by-week shift detail"):
        from pay_rules import _raw_hours_per_bucket
        for wk in (r.week1, r.week2):
            st.markdown(
                f"**Wk{wk.week_num}** "
                f"({wk.start.strftime('%b %d')}–{wk.end.strftime('%b %d')}): "
                f"{wk.actual_hours:.1f}h actual + {wk.admin_hours:.0f}h admin = "
                f"{wk.total_hours:.1f}h  ·  "
                f"eve {wk.evening_hours:.1f}h · night {wk.night_hours:.1f}h · "
                f"wknd {wk.weekend_hours:.1f}h · hol {wk.holiday_hours:.1f}h"
            )
            if wk.shifts:
                rows = []
                for s in wk.shifts:
                    ev, ni, wk_h, ho = _raw_hours_per_bucket(s.start, s.end, cfg)
                    rows.append({
                        "Shift":      s.summary,
                        "Start":      s.start.strftime("%a %b %d %H:%M"),
                        "End":        s.end.strftime("%H:%M"),
                        "Hours":      round(s.hours, 2),
                        "Eve (raw)":  round(ev, 1),
                        "Night (raw)": round(ni, 1),
                        "Wknd (raw)": round(wk_h, 1),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                st.caption(f"Raw = hours in window before 4h minimum rule. "
                           f"Totals after minimum: eve {wk.evening_hours:.1f}h · "
                           f"night {wk.night_hours:.1f}h · wknd {wk.weekend_hours:.1f}h")

    _show_notes(r, stub)


def _show_ytd_panel(stubs: dict[str, "StubData"]) -> None:
    """YTD and PTO summary drawn from the most recently uploaded stub."""
    if not stubs:
        return

    latest_key = max(stubs.keys())
    latest     = stubs[latest_key]

    # Prefer the Total Gross YTD parsed directly from the stub's summary line —
    # it includes all earnings types and pre-tax contributions without guessing.
    # Fall back to summing individual YTD amounts if the direct value wasn't captured.
    if latest.ytd_gross > 0:
        ytd_gross = latest.ytd_gross
    else:
        ytd_by_desc: dict[str, float] = {}
        for e in latest.earnings:
            if e.ytd_amt > ytd_by_desc.get(e.description, 0.0):
                ytd_by_desc[e.description] = e.ytd_amt
        ytd_gross = round(sum(ytd_by_desc.values()), 2)

    current_gross  = latest.total_gross
    pto_period_hrs = latest.pto_hours_used
    pto_remaining  = latest.pto_balance

    st.subheader("Year-to-Date Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("YTD Gross", f"${ytd_gross:,.2f}",
              help="Total Gross YTD from your most recently uploaded stub.")
    c2.metric("Current Period Gross", f"${current_gross:,.2f}",
              help="Gross paid on the most recent uploaded stub.")
    c3.metric("PTO Remaining", f"{pto_remaining:.2f} hrs",
              help="PTO balance shown on the most recent stub.")
    c4.metric("PTO This Period", f"{pto_period_hrs:.2f} hrs",
              help="PTO hours used in the most recent stub period.")


def _show_notes(r: PeriodResult, stub: Optional[StubData] = None) -> None:
    notes = []
    if (r.week1.weekend_hours + r.week2.weekend_hours) > 0:
        notes.append(
            "ℹ️ Weekend hours use **scheduled** ICS start times. "
            "Actual clock-in rounding (~0.5h) → small delta is expected."
        )
    for n in notes:
        st.info(n)


TENURE_OPTIONS = ["<5", "5-10", "11-20", "21+"]
TENURE_LABELS  = [
    "Less than 5 years  (9.231h/period)",
    "5–10 years         (10.769h/period)",
    "11–20 years        (11.385h/period)",
    "21+ years          (12.000h/period)",
]


def _show_auth_page() -> None:
    st.title("💵 APP Pay Reconciliation")
    st.caption("Advanced Practice Provider Pay Audit Tool")
    st.divider()

    st.info(
        "⚠️ **Legal Disclaimer:** This tool is an independent resource and is not affiliated "
        "with, endorsed by, or connected to any employer or payroll system. All pay estimates "
        "are generated from your personally entered schedule data and pay parameters. Results "
        "are provided for informational purposes only. You are solely responsible for "
        "verifying all information against your official pay stubs and employer records "
        "before taking any action."
    )
    st.divider()

    tab_login, tab_register = st.tabs(["Login", "Create Account"])



    with tab_login:
        with st.form("login_form"):
            last_name = st.text_input("Last name")
            password  = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)
        if submitted:
            user = auth.login(last_name.strip(), password)
            if user:
                st.session_state["user"] = user
                st.rerun()
            else:
                st.error("Invalid last name or password.")

    with tab_register:
        with st.form("register_form"):
            last_name   = st.text_input("Last name")
            password    = st.text_input("Password", type="password")
            confirm_pw  = st.text_input("Confirm password", type="password")
            base_rate   = st.number_input(
                "Base hourly rate ($)", min_value=50.0, max_value=300.0,
                value=94.37, step=0.01,
            )
            tenure_idx  = st.selectbox(
                "Years of service (determines PTO accrual)",
                range(len(TENURE_LABELS)),
                format_func=lambda i: TENURE_LABELS[i],
            )
            pto_balance = st.number_input(
                "Current PTO balance (hours) — from your most recent pay stub",
                min_value=0.0, max_value=500.0, value=0.0, step=0.5,
            )
            ics_url = st.text_input(
                "ShiftAdmin ICS URL",
                type="password",
                help="From ShiftAdmin → My Schedule → Subscribe to Calendar. "
                     "Contains your personal auth token — stored encrypted.",
            )
            disclaimer = st.checkbox(
                "I understand this tool is not affiliated with or endorsed by my employer. "
                "I am responsible for verifying all information against my official pay stubs "
                "and records before taking any action."
            )
            submitted = st.form_submit_button("Create account", use_container_width=True)

        if submitted:
            tenure_bracket = TENURE_OPTIONS[tenure_idx]
            if not disclaimer:
                st.error("You must accept the disclaimer to create an account.")
            elif password != confirm_pw:
                st.error("Passwords don't match.")
            elif not all([last_name.strip(), password, ics_url.strip()]):
                st.error("Last name, password, and ShiftAdmin URL are required.")
            else:
                ok, err = auth.register(
                    last_name.strip(), password, base_rate,
                    tenure_bracket, pto_balance, ics_url.strip(),
                )
                if ok:
                    user = auth.login(last_name.strip(), password)
                    st.session_state["user"] = user
                    st.rerun()
                else:
                    st.error(err)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def _build_sidebar(cfg: PayConfig, user: dict) -> None:
    with st.sidebar:
        st.title("💵 APP Pay Recon")
        st.caption(f"Logged in as **{user['last_name']}**")
        st.divider()
        st.subheader("Rates")
        st.caption(
            f"Base: **${cfg.base_rate}/hr**  \n"
            f"OT (per diem): **${cfg.perdiem_rate}/hr**  \n"
            f"Admin: **{cfg.admin_hours_per_week:.0f}h/wk** (unconditional)  \n"
            f"OT threshold: **{cfg.ot_threshold:.0f}h** biweekly  \n\n"
            f"Eve diff: **+${cfg.evening_rate}/hr** (15:00–23:00, ≥4h)  \n"
            f"Night diff: **+${cfg.night_rate}/hr** (23:00–07:00, ≥4h)  \n"
            f"Wknd diff: **+${cfg.weekend_rate}/hr** (Fri 23:00–Sun 23:00, ≥4h)  \n"
            f"Holiday: **+50% base** on holidays  \n"
        )
        st.divider()
        if st.button("🔄 Refresh schedule", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        if st.button("🚪 Logout", use_container_width=True):
            del st.session_state["user"]
            st.cache_data.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# Year Audit — multi-stub upload + two sub-tabs
# ---------------------------------------------------------------------------

def _load_audit_stubs(uploaded_files) -> list[StubData]:
    """Parse all uploaded PDFs into sorted list of StubData."""
    stubs: list[StubData] = []
    for f in uploaded_files:
        try:
            stubs.extend(parse_stub_pdf(f))
        except Exception as exc:
            st.warning(f"Could not parse {f.name}: {exc}")
    stubs.sort(key=lambda s: (s.advice_date or date.min))
    return stubs


def _show_pto_audit_tab(stubs: list[StubData], cfg: PayConfig) -> None:
    """PTO Audit: was PTO deducted correctly based on actual hours worked?"""
    st.markdown(
        "For each pay period, shows hours actually worked vs PTO charged "
        "vs what the **36h rule** would predict."
    )

    col_ob, _ = st.columns([1, 2])
    with col_ob:
        opening_bal = st.number_input(
            "PTO balance before first stub (h)",
            value=0.0, step=0.01, format="%.2f",
            key="pto_audit_opening",
            help="Leave 0 if you don't know — the balance-match column will show discrepancies.",
        )

    pto_rows = audit_pto(stubs, cfg, opening_balance=opening_bal)

    # Summary table
    summary_rows = []
    total_overcharge = 0.0
    total_undercharge = 0.0
    for row in pto_rows:
        net_delta = sum(w["diff"] for w in row.weeks)
        total_overcharge  += max(0.0, net_delta)
        total_undercharge += max(0.0, -net_delta)
        bal_str = (
            f"{row.stub_balance:.2f}h ✅"
            if row.balance_matches else
            f"{row.stub_balance:.2f}h ⚠️ calc={row.running_balance:.2f}h"
        )
        summary_rows.append({
            "Period":        row.period_label,
            "Advice Date":   str(row.advice_date) if row.advice_date else "—",
            "PTO Used":      f"{row.stub_pto_used:.2f}h",
            "Stub Balance":  bal_str,
            "Net PTO Δ":     f"{net_delta:+.2f}h",
        })

    def _pto_delta_css(val: str) -> str:
        try:
            v = float(val.replace("h", "").replace("+", ""))
            if abs(v) <= 0.5: return "color: #2ecc71; font-weight:600"
            if abs(v) <= 2.0: return "color: #f39c12; font-weight:600"
            return "color: #e74c3c; font-weight:600"
        except ValueError:
            return ""

    st.dataframe(
        pd.DataFrame(summary_rows).style.map(_pto_delta_css, subset=["Net PTO Δ"]),
        use_container_width=True, hide_index=True,
    )

    if total_overcharge or total_undercharge:
        c1, c2 = st.columns(2)
        c1.metric("Total PTO over-charged", f"+{total_overcharge:.2f}h",
                  help="Hours charged as PTO beyond the 36h rule")
        c2.metric("Total PTO under-charged", f"−{total_undercharge:.2f}h",
                  help="PTO shortfall relative to 36h rule (shouldn't happen)")

    # Per-period expandable detail
    st.divider()
    st.markdown("**Per-period week detail**")
    for row in pto_rows:
        with st.expander(f"{row.period_label}  (advice {row.advice_date})"):
            if not row.weeks:
                st.caption("No week data parsed from stub.")
                continue
            week_df = pd.DataFrame([
                {
                    "Week Begin":     str(w["week_begin"]),
                    "Week End":       str(w["week_end"]),
                    "Worked (h)":     w["actual_worked"],
                    "PTO Charged (h)": w["pto_charged"],
                    "Expected PTO (h)": w["expected_pto"],
                    "Δ (charged−expected)": f"{w['diff']:+.2f}h",
                }
                for w in row.weeks
            ])
            st.dataframe(week_df, use_container_width=True, hide_index=True)

    st.caption(
        "Rule: PTO = max(0, 36h − worked_hours) per week. "
        "Δ > 0 = more PTO charged than expected. "
        "Balance-match compares stub balance against accrual model (±0.5h tolerance)."
    )


def _show_diff_audit_tab(
    stubs: list[StubData],
    results: list[PeriodResult],
    cfg: PayConfig,
) -> None:
    """Differential Audit: engine estimate vs stub, period by period."""
    st.markdown(
        "For each period, compares the **scheduled-hours engine estimate** against "
        "what the stub shows was actually paid — both gross and by differential category. "
        "**Negative Δ = underpaid.** Evening and weekend differentials are shown separately "
        "so you can see exactly where money was missed."
    )
    st.caption(
        "Engine hours come from the ICS schedule and historical PDF calendar. "
        "Small gross deltas (±$10) often reflect clock-in rounding; "
        "zero-differential lines in a stub when the engine expects a non-zero amount "
        "are the red flag."
    )

    summary_rows = []
    total_gross_delta = 0.0
    total_eve_delta   = 0.0
    total_wknd_delta  = 0.0

    for stub in stubs:
        r = _match_stub(results, stub)
        if r is None:
            period_str = (
                f"{stub.period_start} → {stub.period_end}"
                if stub.period_start else str(stub.advice_date)
            )
            summary_rows.append({
                "Period":      period_str,
                "Advice Date": str(stub.advice_date or "—"),
                "Engine Est":  "—",
                "Stub Gross":  f"${stub.total_gross:,.2f}",
                "Gross Δ":     "—",
                "Eve Δ":       "—",
                "Wknd Δ":      "—",
                "OT":          "?",
            })
            continue

        est        = r.total_estimated_gross()
        stub_gross = stub.recurring_gross or stub.computed_gross
        gross_d    = stub_gross - est
        total_gross_delta += gross_d

        # Engine differential amounts (schedule-based)
        eng_eve  = round(r.evening_pay()  + r.ot_evening_pay(),  2)
        eng_wknd = round(r.weekend_pay()  + r.ot_weekend_pay(),  2)
        # Stub differential amounts (actually paid)
        stub_eve  = stub.evening_pay   # amount_by_cat("eve", "ot_eve")
        stub_wknd = stub.weekend_pay   # amount_by_cat("wknd", "ot_wknd")

        eve_d  = round(stub_eve  - eng_eve,  2)
        wknd_d = round(stub_wknd - eng_wknd, 2)
        total_eve_delta  += eve_d
        total_wknd_delta += wknd_d

        summary_rows.append({
            "Period":      r.period.label,
            "Advice Date": str(stub.advice_date or "—"),
            "Engine Est":  f"${est:,.2f}",
            "Stub Gross":  f"${stub_gross:,.2f}",
            "Gross Δ":     f"${gross_d:+,.2f}",
            "Eve Δ":       f"${eve_d:+,.2f}" if (eng_eve or stub_eve) else "—",
            "Wknd Δ":      f"${wknd_d:+,.2f}" if (eng_wknd or stub_wknd) else "—",
            "OT":          "✅" if r.perdiem_hours > 0 else "",
        })

    summary_df = pd.DataFrame(summary_rows)
    delta_cols = [c for c in ["Gross Δ", "Eve Δ", "Wknd Δ"] if c in summary_df.columns]
    st.dataframe(
        summary_df.style.map(_delta_css, subset=delta_cols),
        use_container_width=True, hide_index=True,
    )

    # Summary metrics
    total_diff_delta = round(total_eve_delta + total_wknd_delta, 2)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Gross Δ (all periods)", f"${total_gross_delta:+,.2f}",
        help="Stub gross minus engine estimate. Includes base, differentials, and OT.",
    )
    m2.metric(
        "Evening Diff Δ", f"${total_eve_delta:+,.2f}",
        help="Stub eve+OT-eve paid vs engine estimate (negative = underpaid).",
    )
    m3.metric(
        "Weekend Diff Δ", f"${total_wknd_delta:+,.2f}",
        help="Stub wknd+OT-wknd paid vs engine estimate (negative = underpaid).",
    )
    m4.metric(
        "Total Differential Δ", f"${total_diff_delta:+,.2f}",
        help="Eve Δ + Wknd Δ: combined differential underpayment across all periods.",
    )

    # Per-period line-by-line drill-down
    st.divider()
    st.markdown("**Period-by-period detail**")
    for stub in stubs:
        r = _match_stub(results, stub)
        if r is None:
            continue
        stub_gross = stub.recurring_gross or stub.computed_gross
        with st.expander(f"{r.period.label}  —  Gross Δ ${stub_gross - r.total_estimated_gross():+,.2f}"):
            comp_df = _comparison_df(r, stub, cfg)
            st.dataframe(
                comp_df.style.map(_delta_css, subset=["Δ"]),
                use_container_width=True, hide_index=True,
            )
            # Parsed earnings lines
            if stub.earnings:
                with st.expander("Raw parsed stub lines"):
                    st.dataframe(pd.DataFrame([
                        {"Desc": e.description,
                         "WkBegin": str(e.week_begin or "—"),
                         "Hrs": e.hours, "Rate": e.rate,
                         "Current": e.current_amt, "Cat": e.category}
                        for e in stub.earnings
                    ]), use_container_width=True, hide_index=True)



def _show_other_earnings(stubs: list[StubData]) -> None:
    """Display non-reconciled earnings lines (corrections, CME stipends, bonuses)."""
    other_lines = [
        {
            "Advice Date": s.advice_date.strftime("%b %d, %Y") if s.advice_date else "—",
            "Description": e.description,
            "Hours": f"{e.hours:.2f}" if e.hours else "—",
            "Amount": f"${e.current_amt:,.2f}",
            "YTD": f"${e.ytd_amt:,.2f}",
        }
        for s in stubs
        for e in s.earnings
        if e.category == "other" and e.current_amt != 0.0
    ]
    if not other_lines:
        st.caption("No other earnings on uploaded stubs.")
        return
    total = sum(
        e.current_amt
        for s in stubs for e in s.earnings
        if e.category == "other"
    )
    st.metric("Total Other Earnings", f"${total:,.2f}")
    st.dataframe(
        pd.DataFrame(other_lines),
        hide_index=True,
        use_container_width=True,
    )


def _show_pto_update_gate(
    stubs: list[StubData],
    cfg: PayConfig,
    user: dict,
) -> None:
    """
    After stub upload: run PTO audit on the most recent stub.
    If clean, offer to update stored PTO balance. If discrepant, block and flag.
    """
    if not stubs:
        return

    latest_stub = max(stubs, key=lambda s: s.advice_date or date.min)
    if latest_stub.pto_balance <= 0:
        return

    pto_rows = audit_pto([latest_stub], cfg)
    discrepant_weeks = [
        w for r in pto_rows for w in r.weeks
        if abs(w["diff"]) > 0.5
    ]

    st.divider()
    st.subheader("📋 PTO Balance Update")
    st.caption(
        f"Most recent stub: **{latest_stub.advice_date}**  |  "
        f"Reported balance: **{latest_stub.pto_balance:.2f}h**  |  "
        f"Stored balance: **{user['pto_balance']:.2f}h**"
    )

    if not discrepant_weeks:
        st.success("PTO audit clean — no discrepancies in the most recent stub.")
        if abs(latest_stub.pto_balance - float(user["pto_balance"])) > 0.1:
            if st.button(
                f"Update stored balance to {latest_stub.pto_balance:.2f}h",
                use_container_width=True,
            ):
                ok, err = auth.update_settings(
                    user["id"], {"pto_balance": latest_stub.pto_balance}
                )
                if ok:
                    st.session_state["user"]["pto_balance"] = latest_stub.pto_balance
                    st.success(
                        f"PTO balance updated to {latest_stub.pto_balance:.2f}h."
                    )
                else:
                    st.error(f"Update failed: {err}")
        else:
            st.caption("Stored balance already matches stub — no update needed.")
    else:
        st.warning(
            f"PTO discrepancy detected in {len(discrepant_weeks)} week(s) — "
            "balance not updated automatically. Review the PTO Audit tab."
        )
        if st.checkbox("I've reviewed the discrepancy and want to override anyway"):
            if st.button(
                f"Force update to {latest_stub.pto_balance:.2f}h",
                type="primary",
                use_container_width=True,
            ):
                ok, err = auth.update_settings(
                    user["id"], {"pto_balance": latest_stub.pto_balance}
                )
                if ok:
                    st.session_state["user"]["pto_balance"] = latest_stub.pto_balance
                    st.success(
                        f"PTO balance updated to {latest_stub.pto_balance:.2f}h."
                    )
                else:
                    st.error(f"Update failed: {err}")


def _show_year_audit(
    stubs_by_period: dict[str, "StubData"],
    results: list[PeriodResult],
    cfg: PayConfig,
    user: dict,
) -> None:
    stubs = list(stubs_by_period.values())

    if not stubs:
        st.info("No stubs uploaded yet. Upload a stub in the Schedule tab to begin the audit.")
        return

    first_date = min((s.period_start for s in stubs if s.period_start), default=None)
    last_date  = max((s.period_end   for s in stubs if s.period_end),   default=None)
    st.success(
        f"**{len(stubs)} stub(s)** loaded  ·  {first_date} → {last_date}"
    )

    t_pto, t_diff = st.tabs(["📊 PTO Audit", "💰 Differential Audit"])

    with t_pto:
        _show_pto_audit_tab(stubs, cfg)

    with t_diff:
        _show_diff_audit_tab(stubs, results, cfg)

    st.divider()
    st.subheader("💰 Other Earnings")
    st.caption(
        "Pay corrections, CME stipends, bonuses, and other non-reconciled lines. "
        "Displayed for tracking only — no engine comparison."
    )
    _show_other_earnings(stubs)
    _show_pto_update_gate(stubs, cfg, user)


# ---------------------------------------------------------------------------
# Settings (stub — implemented in Task 8)
# ---------------------------------------------------------------------------

def _show_settings(user: dict) -> None:
    st.subheader("Account Settings")
    st.caption(f"Account: **{user['last_name']}**")

    with st.form("settings_form"):
        base_rate = st.number_input(
            "Base hourly rate ($)",
            min_value=50.0, max_value=300.0,
            value=float(user["base_rate"]), step=0.01,
        )
        tenure_idx = st.selectbox(
            "Years of service",
            range(len(TENURE_OPTIONS)),
            index=TENURE_OPTIONS.index(user["tenure_bracket"]),
            format_func=lambda i: TENURE_LABELS[i],
        )
        pto_balance = st.number_input(
            "PTO balance (hours)",
            min_value=0.0, max_value=500.0,
            value=float(user["pto_balance"]), step=0.5,
        )
        ics_url = st.text_input(
            "ShiftAdmin ICS URL (leave blank to keep current)",
            type="password",
        )
        current_baseline = user.get("baseline_date")
        baseline_date = st.date_input(
            "First pay stub date",
            value=date.fromisoformat(current_baseline) if current_baseline else None,
            help="Start date of your first uploaded pay stub. Periods before this date are hidden.",
        )
        st.divider()
        st.subheader("Change Password")
        current_pw = st.text_input("Current password", type="password",
                                   key="settings_current_pw")
        new_pw     = st.text_input("New password", type="password",
                                   key="settings_new_pw")
        confirm_pw = st.text_input("Confirm new password", type="password",
                                   key="settings_confirm_pw")

        save = st.form_submit_button("Save changes", use_container_width=True)

    if save:
        updates: dict = {
            "base_rate":      base_rate,
            "tenure_bracket": TENURE_OPTIONS[tenure_idx],
            "pto_balance":    pto_balance,
            "baseline_date":  baseline_date.isoformat() if baseline_date else None,
        }
        if ics_url.strip():
            updates["ics_url"] = ics_url.strip()
        if new_pw:
            if not current_pw:
                st.error("Enter your current password to set a new one.")
                return
            if new_pw != confirm_pw:
                st.error("New passwords don't match.")
                return
            if not auth.verify_current_password(user["id"], current_pw):
                st.error("Current password is incorrect.")
                return
            updates["password"] = new_pw

        ok, err = auth.update_settings(user["id"], updates)
        if ok:
            for k, v in updates.items():
                if k not in ("password",):
                    st.session_state["user"][k] = v
            if "ics_url" in updates:
                st.session_state["user"]["ics_url"] = updates["ics_url"]
            st.success("Settings saved.")
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(f"Save failed: {err}")


# ---------------------------------------------------------------------------
# Onboarding (first-run stub upload)
# ---------------------------------------------------------------------------

def _show_stub_upload(user: dict, results: list[PeriodResult]) -> None:
    """Reusable upload widget — parses stub, saves to Supabase, reruns."""
    uploaded = st.file_uploader("Upload PDF stub", type=["pdf"], key="new_stub_upload")
    if uploaded:
        try:
            parsed = parse_stub_pdf(uploaded)
            stub = parsed[0] if parsed else None
        except Exception as exc:
            st.error(f"Couldn't read stub: {exc}")
            stub = None

        if stub:
            matched = _match_stub(results, stub)
            if matched:
                st.success(f"Detected period: **{matched.period.label}**")
                if st.button("Save stub", key="save_new_stub"):
                    ok, err = _save_stub_to_db(user["id"], stub, matched.period.start.isoformat())
                    if ok:
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"Save failed: {err}")
            else:
                st.warning("Couldn't detect period. Pick the start date:")
                manual = st.date_input("Pay period start date", key="new_stub_manual_date")
                if st.button("Save with this date", key="save_new_stub_manual"):
                    ok, err = _save_stub_to_db(user["id"], stub, manual.isoformat())
                    if ok:
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"Save failed: {err}")


def _show_onboarding(user: dict, results: list[PeriodResult]) -> None:
    st.title("💵 APP Pay Reconciliation")
    st.info(
        "Upload your most recent pay stub to get started. "
        "Only periods with an uploaded stub will be shown — no projections."
    )

    uploaded = st.file_uploader("Upload pay stub (PDF)", type=["pdf"])
    if uploaded:
        try:
            parsed = parse_stub_pdf(uploaded)
            stub = parsed[0] if parsed else None
        except Exception as exc:
            st.error(f"Couldn't read stub: {exc}")
            stub = None

        if stub:
            matched = _match_stub(results, stub)
            if matched:
                st.success(f"Detected period: **{matched.period.label}**")
                if st.button("Save and continue"):
                    ok, err = _save_stub_to_db(user["id"], stub, matched.period.start.isoformat())
                    if ok:
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"Save failed: {err}")
            else:
                st.warning("Couldn't detect period from stub. Pick the period start date:")
                manual = st.date_input("Pay period start date", key="onboard_manual")
                if st.button("Save with this date"):
                    ok, err = _save_stub_to_db(user["id"], stub, manual.isoformat())
                    if ok:
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error(f"Save failed: {err}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "user" not in st.session_state:
        _show_auth_page()
        return

    user        = st.session_state["user"]
    accrual     = auth.accrual_rate(user["tenure_bracket"])
    cfg         = _load_cfg(float(user["base_rate"]), accrual)
    results_all = _load_results(user["ics_url"], float(user["base_rate"]), accrual)
    stubs       = _load_stubs_cached(user["id"])

    if not stubs:
        _build_sidebar(cfg, user)
        _show_onboarding(user, results_all)
        return

    # Only show periods that have an uploaded stub
    results = [r for r in results_all if r.period.start.isoformat() in stubs]

    _build_sidebar(cfg, user)

    st.title("💵 APP Pay Reconciliation")

    tab_sched, tab_audit, tab_settings = st.tabs(
        ["📋 Schedule", "🔍 Year Audit", "⚙️ Settings"]
    )

    with tab_sched:
        _show_ytd_panel(stubs)
        st.divider()

        with st.expander("📤 Upload a new pay stub"):
            _show_stub_upload(user, results_all)

        st.subheader("Pay Periods")
        df = _build_summary_df(results, stubs, cfg)
        st.dataframe(df, use_container_width=True, hide_index=True)

        period_labels = [r.period.label for r in results]
        selected_label = st.radio(
            "Period:",
            options=period_labels,
            index=len(period_labels) - 1,
            horizontal=True,
            key="period_picker",
        )
        idx  = period_labels.index(selected_label)
        r    = results[idx]
        stub = stubs.get(r.period.start.isoformat())
        st.divider()
        _show_detail(r, stub, cfg, results_all, user)

    with tab_audit:
        _show_year_audit(stubs, results_all, cfg, user)

    with tab_settings:
        _show_settings(user)


if __name__ == "__main__":
    main()
