"""HHC Pay Reconciliation — Streamlit dashboard."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from datetime import date

from parse_stub import StubData, parse_stub, parse_stub_pdf
from pay_rules import PayConfig
from reconcile import PeriodResult, PTOProjectionRow, project_pto, reconcile
from reconcile_stubs import audit_pto, audit_differentials, total_underpayment

import auth

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------
ACTUALS_PATH = Path(__file__).parent / "actuals.json"

st.set_page_config(
    page_title="HHC Pay Reconciliation",
    page_icon="💵",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_actuals() -> dict[str, float]:
    if ACTUALS_PATH.exists():
        try:
            return json.loads(ACTUALS_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_actuals(actuals: dict[str, float]) -> None:
    ACTUALS_PATH.write_text(json.dumps(actuals, indent=2))


# ---------------------------------------------------------------------------
# Data loading (cached per session — ICS fetch only happens once)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Fetching schedule from ShiftAdmin…")
def _load_results(ics_url: str) -> list[PeriodResult]:
    from schedule import load_all_shifts
    shifts = load_all_shifts(ics_url=ics_url)
    return reconcile(shifts=shifts)


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


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _build_summary_df(
    results: list[PeriodResult],
    actuals: dict[str, float],
    cfg: PayConfig,
) -> pd.DataFrame:
    rows = []
    for r in results:
        label  = r.period.label
        est    = r.total_estimated_gross()
        actual = actuals.get(label)
        rows.append({
            "Period":    label,
            "Paydate":   r.period.paydate.strftime("%b %d, %Y"),
            "Shifts":    len(r.week1.shifts) + len(r.week2.shifts),
            "Paid Hrs":  f"{r.total_paid_hours:.1f}",
            "OT Hrs":    f"{r.perdiem_hours:.1f}" if r.perdiem_hours else "—",
            "Holidays":  ", ".join(h.strftime("%b %d") for h in r.period.holidays()) or "—",
            "Est. Gross": f"${est:,.2f}",
            "Actual":    f"${actual:,.2f}" if actual else "—",
            "Δ":         f"${actual - est:+,.2f}" if actual else "—",
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
            r.holiday_pay(), "+50% base on HHC holidays",
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
# Detail panel
# ---------------------------------------------------------------------------

def _show_detail(
    r: PeriodResult,
    actuals: dict[str, float],
    cfg: PayConfig,
    results: list[PeriodResult],
) -> None:
    label = r.period.label
    est   = r.total_estimated_gross()

    hols = r.period.holidays()
    hol_str = f"  ·  holidays: {', '.join(h.strftime('%b %d') for h in hols)}" if hols else ""
    st.subheader(f"{label}  ·  paydate {r.period.paydate}{hol_str}")

    # ---- Hours summary ----
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Actual Hours", f"{r.total_actual_hours:.1f}h")
    c2.metric("Admin Hours",  f"{r.total_admin_hours:.1f}h")
    c3.metric("Total Paid",   f"{r.total_paid_hours:.1f}h")
    c4.metric("OT / Per Diem", f"{r.perdiem_hours:.1f}h")

    # ---- Engine breakdown table ----
    st.markdown("**Engine Estimate**")
    eng_df = _engine_breakdown_df(r, cfg)
    st.dataframe(
        eng_df.style.apply(
            lambda col: ["font-weight:700" if i == len(eng_df)-1 else "" for i in range(len(col))],
            axis=0,
        ),
        use_container_width=True,
        hide_index=True,
    )

    # ---- Actual / stub section ----
    st.divider()
    st.markdown("**Compare Against Actual Paystub**")

    stub_key  = f"stub_{label}"
    gross_key = f"gross_{label}"

    col_up, col_gross = st.columns([2, 1])

    with col_up:
        uploaded = st.file_uploader(
            "Upload PDF stub for this period",
            type=["pdf"],
            key=f"up_{label}",
            label_visibility="collapsed",
            help="Drop the PDF pay stub for this period to get a line-by-line comparison.",
        )

    with col_gross:
        saved_gross = actuals.get(label, "")
        manual_gross = st.text_input(
            "Or enter actual gross",
            value=str(saved_gross) if saved_gross else "",
            placeholder="e.g. 8234.56",
            key=gross_key,
            label_visibility="visible",
        )
        if st.button("Save", key=f"save_{label}"):
            try:
                g = float(manual_gross.replace(",", "").replace("$", ""))
                actuals[label] = g
                _save_actuals(actuals)
                st.success(f"Saved ${g:,.2f}")
                st.rerun()
            except ValueError:
                st.error("Enter a plain number, e.g. 8234.56")

    # Resolve stub: uploaded file takes priority, else session state
    stub: Optional[StubData] = None
    if uploaded is not None:
        try:
            with st.spinner("Parsing PDF…"):
                stub = parse_stub(uploaded)
            st.session_state[stub_key] = stub
        except Exception as exc:
            st.error(f"PDF parse error: {exc}")
    elif stub_key in st.session_state:
        stub = st.session_state[stub_key]

    # Reconcile using manual gross if no full stub
    actual_gross = actuals.get(label)
    if stub is None and actual_gross:
        # No full stub, but we have a total — show just the gross delta
        delta = actual_gross - est
        st.divider()
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Engine Estimate", f"${est:,.2f}")
        cc2.metric("Actual Gross",    f"${actual_gross:,.2f}")
        cc3.metric("Δ (Actual − Engine)", f"${delta:+,.2f}",
                   delta_color="normal" if delta >= 0 else "inverse")
        _show_notes(r)
        return

    if stub is not None:
        # If stub has dates, verify it matches this period
        if stub.period_start is not None:
            matched = _match_stub(results, stub)
            if matched is None or matched.period.label != label:
                st.warning(
                    f"Stub dates ({stub.period_start} → {stub.period_end}) "
                    f"don't match the selected period ({r.period.start} → {r.period.end}). "
                    "Check that you uploaded the right stub."
                )

        # Use stub gross if available, else fall back to computed
        stub_gross = stub.total_gross or actual_gross or stub.computed_gross
        if stub_gross == 0 and actual_gross:
            stub.raw_gross = actual_gross
            stub_gross = actual_gross

        delta    = stub_gross - est
        accuracy = (1 - abs(delta) / stub_gross) * 100 if stub_gross else 100.0

        st.divider()
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("Engine Estimate", f"${est:,.2f}")
        cc2.metric("Stub Gross",      f"${stub_gross:,.2f}")
        cc3.metric("Δ (Stub − Engine)", f"${delta:+,.2f}",
                   delta_color="normal" if delta >= 0 else "inverse")
        cc4.metric("Accuracy", f"{accuracy:.1f}%")

        st.markdown("**Line-by-line comparison**")
        comp_df = _comparison_df(r, stub, cfg)
        st.dataframe(
            comp_df.style.map(_delta_css, subset=["Δ"]),
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("Raw extracted text / parsed lines"):
            st.text(stub.raw_text[:5000] if stub.raw_text else "(none)")
            if stub.earnings:
                st.dataframe(pd.DataFrame([
                    {"Desc": e.description, "Hrs": e.hours, "Rate": e.rate,
                     "Current": e.current_amt, "Cat": e.category}
                    for e in stub.earnings
                ]), use_container_width=True, hide_index=True)

    # ---- Week-by-week shift detail ----
    with st.expander("Week-by-week shift detail"):
        for wk in (r.week1, r.week2):
            st.markdown(
                f"**Wk{wk.week_num}** "
                f"({wk.start.strftime('%b %d')}–{wk.end.strftime('%b %d')}): "
                f"{wk.actual_hours:.1f}h actual + {wk.admin_hours:.0f}h admin = "
                f"{wk.total_hours:.1f}h  ·  "
                f"eve {wk.evening_hours:.1f}h · wknd {wk.weekend_hours:.1f}h · "
                f"hol {wk.holiday_hours:.1f}h"
            )
            if wk.shifts:
                st.dataframe(pd.DataFrame([
                    {
                        "Shift":  s.summary,
                        "Start":  s.start.strftime("%a %b %d %H:%M"),
                        "End":    s.end.strftime("%H:%M"),
                        "Hours":  round(s.hours, 2),
                    }
                    for s in wk.shifts
                ]), use_container_width=True, hide_index=True)

    _show_notes(r, stub)


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
    st.title("💵 HHC Pay Reconciliation")
    st.caption("Charlotte Hungerford Hospital — APP Pay Audit Tool")
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
            submitted = st.form_submit_button("Create account", use_container_width=True)

        if submitted:
            tenure_bracket = TENURE_OPTIONS[tenure_idx]
            if password != confirm_pw:
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
        st.title("💵 HHC Pay Recon")
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
            f"Holiday: **+50% base** on HHC holidays  \n"
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
# PTO projection (schedule-based, existing logic)
# ---------------------------------------------------------------------------

def _show_pto_projection(results: list[PeriodResult], cfg: PayConfig) -> None:
    st.subheader("🏖️ PTO Balance Projection")

    col_bal, col_pto = st.columns([1, 1])
    with col_bal:
        starting_balance = st.number_input(
            "Starting PTO balance (hours)",
            value=123.92,
            step=0.01,
            format="%.2f",
            help="PTO balance from your most recent pay stub. Last known: 123.92h on 05/21/2026.",
        )
    with col_pto:
        extra_aug_pto = st.number_input(
            "Additional PTO to take in August (hours)",
            value=24.0,
            step=1.0,
            format="%.1f",
            help="Hours of planned PTO in August beyond what the schedule already shows.",
        )

    # Infer last scheduled date
    last_sched = max(
        (s.start.date() for r in results for wk in (r.week1, r.week2) for s in wk.shifts),
        default=date.today(),
    )

    aug_paydate = date(2026, 8, 28)

    rows = project_pto(
        results,
        starting_balance=starting_balance,
        starting_after_paydate=date(2026, 5, 22),
        extra_pto={aug_paydate: extra_aug_pto} if extra_aug_pto else {},
        last_scheduled=last_sched,
        project_through=date(2026, 9, 30),
    )

    if not rows:
        st.caption("No future periods to project.")
        return

    table_rows = []
    for row in rows:
        table_rows.append({
            "Period":    row.period_label,
            "Paydate":   row.paydate.strftime("%b %d, %Y"),
            "Accrual":   f"+{row.accrual:.2f}h",
            "PTO Used":  f"−{row.pto_used:.1f}h" if row.pto_used else "—",
            "Balance":   f"{row.balance:.2f}h",
            "Schedule":  "✅" if row.schedule_complete else "⚠️ partial",
        })

    proj_df = pd.DataFrame(table_rows)

    def _bal_colour(val: str) -> str:
        try:
            v = float(val.replace("h", ""))
            if v < 40:   return "color: #e74c3c; font-weight:600"
            if v < 80:   return "color: #f39c12; font-weight:600"
            return "color: #2ecc71; font-weight:600"
        except ValueError:
            return ""

    st.dataframe(
        proj_df.style.map(_bal_colour, subset=["Balance"]),
        use_container_width=True,
        hide_index=True,
    )

    sep_rows = [r for r in rows if r.paydate >= date(2026, 9, 1)]
    if sep_rows:
        sep_bal = sep_rows[0].balance
        colour = "green" if sep_bal >= 80 else "orange" if sep_bal >= 40 else "red"
        st.markdown(
            f"**Entering September (paydate {sep_rows[0].paydate.strftime('%b %d')}):** "
            f"<span style='color:{colour}; font-weight:700'>{sep_bal:.2f}h</span> PTO remaining",
            unsafe_allow_html=True,
        )

    st.caption(
        f"PTO rule: {cfg.pto_standard_weekly_hours:.0f}h/week standard — shortfall charged as PTO. "
        f"Accrual: +{cfg.pto_accrual_per_period:.2f}h/period (verified from Feb–May 2026 stubs). "
        f"⚠️ = week has no posted shifts yet (schedule through {last_sched.strftime('%b %d')})."
    )


# ---------------------------------------------------------------------------
# Year Audit — multi-stub upload + three sub-tabs
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
        stub_gross = stub.total_gross or stub.computed_gross
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
        stub_gross = stub.total_gross or stub.computed_gross
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


def _show_stub_pto_plan_tab(
    stubs: list[StubData],
    results: list[PeriodResult],
    cfg: PayConfig,
) -> None:
    """PTO Plan: historical trajectory from stubs + forward projection."""
    if not stubs:
        return

    latest = stubs[-1]
    latest_balance = latest.pto_balance
    latest_paydate = latest.advice_date or date.min

    st.markdown(
        f"**Latest stub:** {latest.period_start} → {latest.period_end}  "
        f"·  balance **{latest_balance:.2f}h**  ·  paydate **{latest_paydate}**"
    )

    # Historical trajectory
    st.markdown("#### Historical PTO Balance (from stubs)")
    ACCRUAL = cfg.pto_accrual_per_period
    running = latest_balance - ACCRUAL + stubs[0].pto_hours_used  # back-calc opening
    hist_rows = []
    for stub in stubs:
        running += ACCRUAL
        running -= stub.pto_hours_used
        period_str = (
            f"{stub.period_start.strftime('%b %d')}–{stub.period_end.strftime('%b %d, %Y')}"
            if stub.period_start and stub.period_end else str(stub.advice_date)
        )
        match_flag = "✅" if abs(running - stub.pto_balance) < 0.5 else f"⚠️ stub={stub.pto_balance:.2f}"
        hist_rows.append({
            "Period":       period_str,
            "Advice Date":  str(stub.advice_date or "—"),
            "PTO Used":     f"−{stub.pto_hours_used:.2f}h",
            "Stub Balance": f"{stub.pto_balance:.2f}h",
            "Calc Balance": f"{running:.2f}h",
            "Match":        match_flag,
        })

    st.dataframe(pd.DataFrame(hist_rows), use_container_width=True, hide_index=True)

    # Forward projection
    st.markdown("#### Forward Projection")

    last_sched = max(
        (s.start.date() for r in results for wk in (r.week1, r.week2) for s in wk.shifts),
        default=date.today(),
    )

    col1, col2 = st.columns(2)
    with col1:
        extra_pto_h = st.number_input(
            "Additional planned PTO (hours)",
            value=24.0, step=1.0, format="%.1f",
            key="stub_plan_extra_pto",
        )
    with col2:
        extra_paydate_str = st.text_input(
            "On paydate (YYYY-MM-DD)",
            value="2026-08-28",
            key="stub_plan_paydate",
        )

    try:
        extra_pd = date.fromisoformat(extra_paydate_str)
        extra_pto_map = {extra_pd: extra_pto_h} if extra_pto_h else {}
    except ValueError:
        st.error("Enter date as YYYY-MM-DD")
        extra_pto_map = {}

    proj_rows = project_pto(
        results,
        starting_balance=latest_balance,
        starting_after_paydate=latest_paydate,
        extra_pto=extra_pto_map,
        last_scheduled=last_sched,
        project_through=date(2026, 12, 31),
    )

    if not proj_rows:
        st.caption("No future periods beyond the latest stub.")
        return

    proj_table = []
    for row in proj_rows:
        proj_table.append({
            "Period":    row.period_label,
            "Paydate":   row.paydate.strftime("%b %d, %Y"),
            "Accrual":   f"+{row.accrual:.2f}h",
            "PTO Used":  f"−{row.pto_used:.1f}h" if row.pto_used else "—",
            "Balance":   f"{row.balance:.2f}h",
            "Schedule":  "✅" if row.schedule_complete else "⚠️",
        })

    def _bal_colour(val: str) -> str:
        try:
            v = float(val.replace("h", ""))
            if v < 40:  return "color: #e74c3c; font-weight:600"
            if v < 80:  return "color: #f39c12; font-weight:600"
            return "color: #2ecc71; font-weight:600"
        except ValueError:
            return ""

    st.dataframe(
        pd.DataFrame(proj_table).style.map(_bal_colour, subset=["Balance"]),
        use_container_width=True, hide_index=True,
    )

    sep_rows = [r for r in proj_rows if r.paydate >= date(2026, 9, 1)]
    if sep_rows:
        sep_bal = sep_rows[0].balance
        colour = "green" if sep_bal >= 80 else "orange" if sep_bal >= 40 else "red"
        st.markdown(
            f"**Entering September:** "
            f"<span style='color:{colour}; font-weight:700'>{sep_bal:.2f}h</span> PTO remaining",
            unsafe_allow_html=True,
        )

    eoy_rows = [r for r in proj_rows if r.paydate >= date(2026, 12, 1)]
    if eoy_rows:
        eoy_bal = eoy_rows[-1].balance
        colour = "green" if eoy_bal >= 40 else "orange" if eoy_bal >= 0 else "red"
        st.markdown(
            f"**End of Year:** "
            f"<span style='color:{colour}; font-weight:700'>{eoy_bal:.2f}h</span> PTO balance",
            unsafe_allow_html=True,
        )

    st.caption(
        f"Accrual: +{cfg.pto_accrual_per_period:.2f}h/period. "
        "PTO used from ICS schedule (36h rule). "
        "⚠️ = schedule not yet posted for that week."
    )


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


def _show_year_audit(results: list[PeriodResult], cfg: PayConfig, user: dict) -> None:
    st.subheader("📂 Upload All Pay Stubs")
    st.caption(
        "Upload one or more PDF pay stubs (a single multi-stub PDF or individual files). "
        "The parser handles multi-page PDFs with one stub per page."
    )

    uploaded_files = st.file_uploader(
        "Pay stub PDFs",
        type=["pdf"],
        accept_multiple_files=True,
        key="audit_pdfs",
        label_visibility="collapsed",
    )

    # Parse on upload or when files change
    if uploaded_files:
        file_names = tuple(sorted(f.name for f in uploaded_files))
        if st.session_state.get("audit_stub_files") != file_names:
            with st.spinner(f"Parsing {len(uploaded_files)} file(s)…"):
                stubs = _load_audit_stubs(uploaded_files)
            st.session_state["audit_stubs"] = stubs
            st.session_state["audit_stub_files"] = file_names

    stubs: list[StubData] = st.session_state.get("audit_stubs", [])

    if not stubs:
        st.info("Upload pay stubs above to begin the audit.")
        return

    first_date = stubs[0].advice_date
    last_date  = stubs[-1].advice_date
    st.success(
        f"**{len(stubs)} stub(s)** loaded  ·  "
        f"{first_date} → {last_date}"
    )

    t_pto, t_diff, t_plan = st.tabs([
        "📊 PTO Audit",
        "💰 Differential Audit",
        "🏖️ PTO Plan",
    ])

    with t_pto:
        _show_pto_audit_tab(stubs, cfg)

    with t_diff:
        _show_diff_audit_tab(stubs, results, cfg)

    with t_plan:
        _show_stub_pto_plan_tab(stubs, results, cfg)

    if stubs:
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "user" not in st.session_state:
        _show_auth_page()
        return

    user    = st.session_state["user"]
    accrual = auth.accrual_rate(user["tenure_bracket"])
    cfg     = _load_cfg(float(user["base_rate"]), accrual)
    results = _load_results(user["ics_url"])
    actuals = _load_actuals()

    _build_sidebar(cfg, user)

    st.title("💵 HHC Pay Reconciliation")

    if not results:
        st.warning("No shifts loaded — check your ShiftAdmin URL in Settings.")
        return

    tab_sched, tab_audit, tab_settings = st.tabs(
        ["📋 Schedule", "🔍 Year Audit", "⚙️ Settings"]
    )

    with tab_sched:
        df = _build_summary_df(results, actuals, cfg)
        st.caption("Click a row to see the full engine breakdown and compare against a stub.")
        event = st.dataframe(
            df.style.map(_delta_css, subset=["Δ"]),
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
        )
        selected = event.selection.rows
        if selected:
            idx = selected[0]
            st.divider()
            _show_detail(results[idx], actuals, cfg, results)
        else:
            st.divider()
            total_est  = sum(r.total_estimated_gross() for r in results)
            total_eve  = sum(r.evening_pay() + r.ot_evening_pay() for r in results)
            total_wknd = sum(r.weekend_pay() + r.ot_weekend_pay() for r in results)
            total_hol  = sum(r.holiday_pay() for r in results)
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("Total Estimated Gross", f"${total_est:,.2f}")
            cc2.metric("Evening Differentials", f"${total_eve:,.2f}")
            cc3.metric("Weekend Differentials", f"${total_wknd:,.2f}")
            cc4.metric("Holiday Differentials", f"${total_hol:,.2f}")
            if actuals:
                paid   = sum(actuals.values())
                unpaid = [r for r in results if r.period.label not in actuals]
                st.caption(
                    f"Actual gross entered for {len(actuals)} period(s): ${paid:,.2f}. "
                    f"{len(unpaid)} period(s) estimated only."
                )
            st.divider()
            _show_pto_projection(results, cfg)

    with tab_audit:
        _show_year_audit(results, cfg, user)

    with tab_settings:
        _show_settings(user)


if __name__ == "__main__":
    main()
