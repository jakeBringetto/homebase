"""Per-activity metrics computed once from streams: zones, efficiency, drift, load, pace bins,
best efforts, grade-adjusted pace, cadence."""

from dataclasses import dataclass, field

import numpy as np

from app.modules.running.analysis.efforts import best_efforts
from app.modules.running.analysis.load import hrr, trimp
from app.modules.running.analysis.steady import MIN_SPEED, WARMUP_S, pace_bins
from app.modules.running.config import AthleteSettings
from app.modules.running.sources.base import RIDE_MIN_M, RIDE_SPEED, Streams

ANALYSIS_VERSION = 4  # bump when analysis output changes; sync then recomputes all metrics
MAX_GAP_S = 10  # longer gaps between samples are pauses, not running
DECOUPLING_MIN_S = 45 * 60
MAX_GRADE = 0.45  # Minetti's polynomial is fit on -45%..+45%


@dataclass
class Analysis:
    zone_s: list[float] = field(default_factory=lambda: [0.0] * 5)
    ef: float | None = None
    decoupling_pct: float | None = None
    trimp: float | None = None
    steady_s: float = 0.0
    pace_bins: dict[int, tuple[float, float, float | None]] = field(default_factory=dict)
    best: dict[float, float] = field(default_factory=dict)
    gap_s_km: float | None = None
    cadence_spm: float | None = None
    ride_like: bool = False


def _speed(s: Streams, dt: np.ndarray) -> np.ndarray | None:
    if "velocity_smooth" in s:
        return s["velocity_smooth"].astype(np.float64)
    if "distance" in s:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.nan_to_num(np.diff(s["distance"], prepend=s["distance"][0]) / dt)
    return None


def _ef(speed: np.ndarray, hr: np.ndarray, w: np.ndarray) -> float | None:
    """Average speed (m/min) per bpm over weighted samples."""
    total = w.sum()
    if total < 60:
        return None
    return float((w * speed).sum() / total * 60 / ((w * hr).sum() / total))


def grade_factor(grade_pct: np.ndarray) -> np.ndarray:
    """Energy cost of running at a grade relative to flat (Minetti et al. 2002)."""
    i = np.clip(grade_pct / 100, -MAX_GRADE, MAX_GRADE)
    cost = 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6
    return cost / 3.6


def _gap(speed: np.ndarray, grade: np.ndarray, wm: np.ndarray) -> float | None:
    """Moving time per flat-equivalent km."""
    flat_m = (wm * speed * grade_factor(grade)).sum()
    return round(float(wm.sum() / flat_m * 1000), 1) if flat_m > 0 and wm.sum() >= 60 else None


def _effort_clock(t: np.ndarray, distance: np.ndarray, moving: np.ndarray) -> np.ndarray:
    """Cumulative moving time for best efforts.

    Stops and pause gaps don't count, but any distance covered in one is timed at walking
    speed (MIN_SPEED), so a GPS jump or dropout can't make an effort faster than it was.
    """
    raw = np.diff(t, prepend=t[0])
    step = np.maximum(np.diff(distance, prepend=distance[0]), 0)
    normal = moving & (raw <= MAX_GAP_S)
    return np.cumsum(np.where(normal, raw, np.minimum(raw, step / MIN_SPEED)))


def analyze(s: Streams, athlete: AthleteSettings) -> Analysis:
    """Compute all metrics; channels that are missing just leave their metrics empty."""
    t = s.get("time")
    if t is None or len(t) < 2:
        return Analysis()
    t = t.astype(np.float64)
    dt = np.clip(np.diff(t, prepend=t[0]), 0, MAX_GAP_S)
    moving = s["moving"] > 0.5 if "moving" in s else np.ones(len(t), dtype=bool)
    wm = dt * moving
    out = Analysis()

    if "distance" in s:
        out.best = best_efforts(_effort_clock(t, s["distance"].astype(np.float64), moving),
                                s["distance"].astype(np.float64))  # fmt: skip
        out.ride_like = out.best.get(RIDE_MIN_M, np.inf) < RIDE_MIN_M / RIDE_SPEED
    speed = _speed(s, dt)
    grade = s["grade_smooth"].astype(np.float64) if "grade_smooth" in s else None
    if speed is not None and grade is not None:
        out.gap_s_km = _gap(speed, grade, wm)
    cadence = s["cadence"].astype(np.float64) * 2 if "cadence" in s else None  # Strava: per foot
    if cadence is not None and (wc := wm * (cadence > 0)).sum() >= 60:
        out.cadence_spm = round(float((wc * cadence).sum() / wc.sum()), 1)

    hr = s.get("heartrate")
    if hr is None:
        return out
    hr = hr.astype(np.float64)
    w = wm * (hr > 0)
    zone_idx = np.searchsorted(athlete.zones(), hr, side="right")
    out.zone_s = np.bincount(zone_idx, w, 5).round(1).tolist()
    out.trimp = round(float(trimp(w / 60, hrr(hr, athlete.resting_hr, athlete.max_hr)).sum()), 1)
    if speed is None:
        return out
    out.ef = _ef(speed, hr, w)

    elapsed = np.cumsum(w)
    if elapsed[-1] >= DECOUPLING_MIN_S:
        mid = WARMUP_S + (elapsed[-1] - WARMUP_S) / 2
        first, second = (elapsed >= WARMUP_S) & (elapsed < mid), elapsed >= mid
        ef1, ef2 = _ef(speed, hr, w * first), _ef(speed, hr, w * second)
        if ef1 and ef2:
            out.decoupling_pct = round((ef1 - ef2) / ef1 * 100, 2)

    out.pace_bins = pace_bins(t, w, speed, hr, grade, cadence)
    out.steady_s = sum(b[0] for b in out.pace_bins.values())
    return out
