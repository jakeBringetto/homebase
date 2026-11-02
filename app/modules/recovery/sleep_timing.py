"""Sleep timing and regularity: bedtime, wake time and midpoint on the local clock, an approximate
Sleep Regularity Index (SRI), weekday vs weekend, and timing shifts against the next morning.

Garmin gives one main sleep window per night, so the SRI here treats everything inside it as
asleep and everything outside as awake. The original SRI (Phillips et al. 2017; Windred et al.
2024) is computed minute by minute from actigraphy, counting naps and waking in the night, so
this version reads higher. Compare it with your own history only.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

import numpy as np

from app.core.charts import day_epoch
from app.modules.recovery import reports

DAY_S = 86400
MIN_NIGHTS = 7  # for an SRI, spreads and averages
MIN_GROUP = 2  # nights per weekday/weekend group
FREE_DAYS = {5, 6}  # waking on Saturday or Sunday
SHIFT_BUCKETS = [
    ("steady", "Under 30 min", 0), ("some", "30–60 min", 30), ("big", "Over 60 min", 60),
]  # fmt: skip


@dataclass(frozen=True)
class Night:
    day: date  # wake-up day
    start: int  # unix seconds
    end: int
    sleep_s: int
    score: int | None
    utc_offset_s: int | None

    def _clock(self, ts: int) -> float | None:
        """Local clock hours from the wake day's midnight: 22:30 the night before is -1.5."""
        if self.utc_offset_s is None:
            return None
        return (ts + self.utc_offset_s - day_epoch(self.day)) / 3600

    @property
    def bed_h(self) -> float | None:
        return self._clock(self.start)

    @property
    def wake_h(self) -> float | None:
        return self._clock(self.end)

    @property
    def mid_h(self) -> float | None:
        return self._clock((self.start + self.end) // 2)


def clock(h: float | None) -> str:
    """Hours from midnight (may be negative) as a 24 h clock time."""
    if h is None:
        return "–"
    m = round(h * 60) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def nights(conn: sqlite3.Connection, start: date) -> list[Night]:
    rows = conn.execute(
        "SELECT day, start_utc, end_utc, sleep_s, score, utc_offset_s FROM recovery_sleep"
        " WHERE day >= ? AND start_utc IS NOT NULL AND end_utc > start_utc ORDER BY day",
        (start.isoformat(),),
    ).fetchall()
    return [Night(date.fromisoformat(r[0]), *r[1:]) for r in rows]


def pairs(ns: list[Night]) -> list[tuple[Night, Night]]:
    """Consecutive nights (the second woke the day after the first)."""
    return [(a, b) for a, b in zip(ns, ns[1:], strict=False) if (b.day - a.day).days == 1]


def pair_sri(a: Night, b: Night) -> float:
    """SRI for two consecutive nights: 100 = asleep and awake at the same times both days,
    0 = no better than chance, -100 = exactly reversed. Uses UTC, so travel counts as a shift."""
    b0, b1 = b.start - DAY_S, b.end - DAY_S
    overlap = max(0, min(a.end, b1) - max(a.start, b0))
    mismatch = (a.end - a.start) + (b1 - b0) - 2 * overlap
    return max(-100.0, 100 - 200 * mismatch / DAY_S)


def shift_min(a: Night, b: Night) -> float:
    """How far the midpoint of sleep moved from one night to the next, in minutes."""
    return abs((b.start + b.end) / 2 - (a.start + a.end) / 2 - DAY_S) / 60


def _mean(xs: list[float]) -> float | None:
    return float(np.mean(xs)) if xs else None


def _group(ns: list[Night]) -> dict:
    return {
        "nights": len(ns),
        "bed_h": _mean([n.bed_h for n in ns]),
        "wake_h": _mean([n.wake_h for n in ns]),
        "mid_h": _mean([n.mid_h for n in ns]),
        "sleep_h": _mean([n.sleep_s / 3600 for n in ns]),
    }


def summary(ns: list[Night]) -> dict:
    """SRI, typical timing and spread, night-to-night shift, and weekday vs weekend."""
    ps = pairs(ns)
    local = [n for n in ns if n.utc_offset_s is not None]
    enough = len(local) >= MIN_NIGHTS
    work = _group([n for n in local if n.day.weekday() not in FREE_DAYS])
    free = _group([n for n in local if n.day.weekday() in FREE_DAYS])
    jetlag = None
    if work["nights"] >= MIN_GROUP and free["nights"] >= MIN_GROUP:
        jetlag = (free["mid_h"] - work["mid_h"]) * 60
    return {
        "nights": len(ns),
        "pairs": len(ps),
        "sri": _mean([pair_sri(a, b) for a, b in ps]) if len(ps) >= MIN_NIGHTS else None,
        "shift_min": _mean([shift_min(a, b) for a, b in ps]) if len(ps) >= MIN_NIGHTS else None,
        "bed_h": float(np.median([n.bed_h for n in local])) if enough else None,
        "wake_h": float(np.median([n.wake_h for n in local])) if enough else None,
        "bed_sd_min": float(np.std([n.bed_h for n in local], ddof=1) * 60) if enough else None,
        "wake_sd_min": float(np.std([n.wake_h for n in local], ddof=1) * 60) if enough else None,
        "work": work,
        "free": free,
        "jetlag_min": jetlag,
        "missing_local": len(ns) - len(local),
    }


def next_morning(conn: sqlite3.Connection, ns: list[Night], start: date) -> list[dict]:
    """That morning's HRV, resting HR and sleep score, by how much sleep timing moved."""
    hrv = {n.day: n.hrv for n in reports.nights(conn, start) if n.hrv is not None}
    rhr = {d: v for d, v, _ in reports.resting_hr(conn, start)}
    out = []
    for i, (key, label, lo) in enumerate(SHIFT_BUCKETS):
        hi = SHIFT_BUCKETS[i + 1][2] if i + 1 < len(SHIFT_BUCKETS) else float("inf")
        bs = [b for a, b in pairs(ns) if lo <= shift_min(a, b) < hi]
        out.append({
            "key": key, "label": label, "nights": len(bs),
            "hrv": _mean([hrv[b.day] for b in bs if b.day in hrv]),
            "rhr": _mean([rhr[b.day] for b in bs if b.day in rhr]),
            "score": _mean([b.score for b in bs if b.score is not None]),
        })  # fmt: skip
    return out
