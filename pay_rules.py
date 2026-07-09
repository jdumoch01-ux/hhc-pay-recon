"""Pay rule definitions and per-shift differential calculations."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from pay_periods import HHC_HOLIDAYS

CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass(frozen=True)
class PayConfig:
    base_rate: float
    perdiem_rate: float
    admin_hours_per_week: float
    ot_threshold: float
    # Differential minimums
    min_qualifying_hours: float   # hours in window required per shift to qualify
    # Evening (15:00–23:00)
    evening_rate: float
    evening_start: time
    evening_end: time
    # Night (23:00–07:00)
    night_rate: float
    night_start: time
    night_end: time
    # Weekend (Fri 23:00 → Sun 23:00)
    weekend_rate: float
    weekend_start_day: int        # 0=Mon … 4=Fri … 6=Sun
    weekend_start_time: time
    weekend_end_day: int
    weekend_end_time: time
    # Holiday
    holiday_pct: float
    # OT differentials (confirmed from Apr 5-18 stub)
    ot_evening_rate: float   # applied to per-diem hours in evening window
    ot_weekend_rate: float   # applied to per-diem hours in weekend window
    # PTO
    pto_standard_weekly_hours: float
    pto_accrual_per_period: float

    @classmethod
    def load(
        cls,
        base_rate_override: float | None = None,
        accrual_override: float | None = None,
    ) -> "PayConfig":
        with CONFIG_PATH.open("rb") as f:
            cfg = tomllib.load(f)
        p = cfg["pay"]
        d = cfg["differentials"]
        q = cfg["pto"]
        return cls(
            base_rate=base_rate_override if base_rate_override is not None else p.get("base_rate", 0.0),
            perdiem_rate=p["perdiem_rate"],
            admin_hours_per_week=p["admin_hours_per_week"],
            ot_threshold=p["ot_threshold"],
            min_qualifying_hours=d["min_qualifying_hours"],
            evening_rate=d["evening_rate"],
            evening_start=time.fromisoformat(d["evening_start"]),
            evening_end=time.fromisoformat(d["evening_end"]),
            night_rate=d["night_rate"],
            night_start=time.fromisoformat(d["night_start"]),
            night_end=time.fromisoformat(d["night_end"]),
            weekend_rate=d["weekend_rate"],
            weekend_start_day=d["weekend_start_day"],
            weekend_start_time=time.fromisoformat(d["weekend_start_time"]),
            weekend_end_day=d["weekend_end_day"],
            weekend_end_time=time.fromisoformat(d["weekend_end_time"]),
            holiday_pct=d["holiday_pct"],
            ot_evening_rate=d["ot_evening_rate"],
            ot_weekend_rate=d["ot_weekend_rate"],
            pto_standard_weekly_hours=q["standard_weekly_hours"],
            pto_accrual_per_period=accrual_override if accrual_override is not None else q["accrual_per_period"],
        )


@dataclass
class ShiftEarnings:
    """Differential hour counts for a single shift (after minimums applied)."""
    actual_hours: float = 0.0
    evening_hours: float = 0.0
    night_hours: float = 0.0
    weekend_hours: float = 0.0
    holiday_hours: float = 0.0


def _in_time_window(t: time, win_start: time, win_end: time) -> bool:
    """True if t falls in [win_start, win_end). Handles overnight windows."""
    if win_start < win_end:
        return win_start <= t < win_end
    # Overnight (e.g. 23:00 → 07:00)
    return t >= win_start or t < win_end


def _in_weekend_window(dt: datetime, cfg: PayConfig) -> bool:
    """True if dt is within the configured Fri 23:00 → Sun 23:00 window."""
    day = dt.weekday()   # 0=Mon … 6=Sun
    t = dt.time()
    start_day, start_time = cfg.weekend_start_day, cfg.weekend_start_time
    end_day, end_time = cfg.weekend_end_day, cfg.weekend_end_time

    # Build the set of fully-included days (days between start and end, exclusive)
    if start_day < end_day:
        in_window_days = set(range(start_day, end_day + 1))
    else:  # wraps across week boundary
        in_window_days = set(range(start_day, 7)) | set(range(0, end_day + 1))

    if day not in in_window_days:
        return False
    # On the first day, must be at or after start_time
    if day == start_day and t < start_time:
        return False
    # On the last day, must be before end_time
    if day == end_day and t >= end_time:
        return False
    return True


def _raw_hours_per_bucket(
    start: datetime, end: datetime, cfg: PayConfig
) -> tuple[float, float, float, float]:
    """
    Return (evening_h, night_h, weekend_h, holiday_h) for a shift,
    BEFORE applying the 4-hour minimum rule.
    Iterates in 1-hour steps; handles DST and overnight windows correctly.
    """
    evening_h = night_h = weekend_h = holiday_h = 0.0
    cursor = start
    while cursor < end:
        next_tick = min(cursor + timedelta(hours=1), end)
        frac = (next_tick - cursor).total_seconds() / 3600.0
        mid = cursor + (next_tick - cursor) / 2

        if _in_time_window(mid.time(), cfg.evening_start, cfg.evening_end):
            evening_h += frac
        if _in_time_window(mid.time(), cfg.night_start, cfg.night_end):
            night_h += frac
        if _in_weekend_window(mid, cfg):
            weekend_h += frac
        if mid.date() in HHC_HOLIDAYS:
            holiday_h += frac

        cursor = next_tick

    return round(evening_h, 4), round(night_h, 4), round(weekend_h, 4), round(holiday_h, 4)


def compute_shift_earnings(shift, cfg: PayConfig) -> ShiftEarnings:
    """
    Compute differential hour buckets for one Shift, applying the
    4-hour minimum qualifying rule to each differential independently.
    """
    ev, ni, wk, ho = _raw_hours_per_bucket(shift.start, shift.end, cfg)
    min_h = cfg.min_qualifying_hours
    return ShiftEarnings(
        actual_hours=shift.hours,
        evening_hours=ev if ev >= min_h else 0.0,
        night_hours=ni   if ni >= min_h else 0.0,
        weekend_hours=wk if wk >= min_h else 0.0,
        holiday_hours=ho if ho >= min_h else 0.0,
    )
