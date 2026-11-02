"""Read-side queries for recovery pages: HRV against its normal range, RHR, next-day response,
and morning readiness against that day's run."""

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from app.modules.running import reports as running_reports
from app.modules.running import storage as running_storage

RANGES = {
    "1m": ("1 month", 30), "3m": ("3 months", 91), "6m": ("6 months", 182), "1y": ("1 year", 365),
}  # fmt: skip
DEFAULT_RANGE = "3m"
# Our fallback normal range (before Garmin's ~19-night baseline, or after gaps).
BAND_DAYS, BAND_MIN_NIGHTS = 60, 14
HARD_PCTL = 75  # daily TRIMP at or above this percentile of training days counts as hard
CV_MIN_NIGHTS = 4  # of the last 7, for a weekly HRV CV
SLEEP_BUCKETS = [("poor", "Under 60", 0), ("fair", "60–79", 60), ("good", "80 and up", 80)]

# RHR falls back to the day's minimum when not worn. Trust it when the night has sleep,
# or the day shows real wear (stress measured, Body Battery moved).
RHR_VALID_SQL = (
    "(EXISTS (SELECT 1 FROM recovery_sleep s WHERE s.day = d.day)"
    " OR (d.stress_avg IS NOT NULL AND d.bb_min != d.bb_max))"
)


@dataclass(frozen=True)
class Night:
    day: date
    hrv: float | None
    weekly: float | None
    low: float | None
    high: float | None
    garmin_band: bool  # False = our fallback band
    cv: float | None  # 7-day CV of ln HRV, %


def _cv(values: list[float]) -> float | None:
    """CV (%) of ln HRV over a week, as in Plews et al.; needs CV_MIN_NIGHTS of the 7."""
    if len(values) < CV_MIN_NIGHTS:
        return None
    logs = np.log(values)
    return round(float(100 * np.std(logs, ddof=1) / np.mean(logs)), 2)


def range_start(key: str | None, today: date) -> tuple[str, date]:
    key = key if key in RANGES else DEFAULT_RANGE
    return key, today - timedelta(days=RANGES[key][1] - 1)


def nights(conn: sqlite3.Connection, start: date) -> list[Night]:
    """HRV per night from `start`, with Garmin's normal range or our 60-day mean ± 1 SD."""
    rows = conn.execute(
        "SELECT day, last_night_avg, weekly_avg, balanced_low, balanced_upper FROM recovery_hrv"
        " WHERE day >= ? ORDER BY day",
        ((start - timedelta(days=BAND_DAYS)).isoformat(),),
    ).fetchall()
    history: list[tuple[date, float]] = []
    out = []
    for r in rows:
        day = date.fromisoformat(r["day"])
        low, high, garmin = r["balanced_low"], r["balanced_upper"], True
        if low is None or high is None:
            prior = [v for d, v in history if (day - d).days <= BAND_DAYS]
            garmin = False
            if len(prior) >= BAND_MIN_NIGHTS:
                mean, sd = float(np.mean(prior)), float(np.std(prior))
                low, high = mean - sd, mean + sd
        if r["last_night_avg"] is not None:
            history.append((day, r["last_night_avg"]))
        if day >= start:
            week = [v for d, v in history if (day - d).days < 7]
            out.append(
                Night(day, r["last_night_avg"], r["weekly_avg"], low, high, garmin, _cv(week))
            )
    return out


def resting_hr(conn: sqlite3.Connection, start: date) -> list[tuple[date, float, float]]:
    """(day, RHR, 7-day average of valid days) for days with a trustworthy RHR."""
    rows = conn.execute(
        f"SELECT d.day, d.resting_hr FROM recovery_daily d WHERE d.resting_hr > 0"
        f" AND d.day >= ? AND {RHR_VALID_SQL} ORDER BY d.day",
        ((start - timedelta(days=6)).isoformat(),),
    ).fetchall()
    pts = [(date.fromisoformat(r["day"]), r["resting_hr"]) for r in rows]
    out = []
    for day, rhr in pts:
        week = [v for d, v in pts if 0 <= (day - d).days < 7]
        if day >= start:
            out.append((day, rhr, round(sum(week) / len(week), 1)))
    return out


def next_morning(conn: sqlite3.Connection, start: date, today: date) -> dict:
    """HRV and RHR each morning against the previous day's running load.

    Returns per-night points and a summary per load bucket (rest, easy, hard).
    """
    athlete = running_storage.athlete_settings(conn)
    first, load = running_reports.daily_load(conn, athlete, today)
    training = load[load > 0]
    hard_cut = float(np.percentile(training, HARD_PCTL)) if len(training) else None
    rhr = {d: v for d, v, _ in resting_hr(conn, start)}
    points = []
    for n in nights(conn, start):
        prev = n.day - timedelta(days=1)
        if first is None or prev < first or (n.hrv is None and n.day not in rhr):
            continue
        trimp = float(load[(prev - first).days])
        kind = "rest" if trimp == 0 else "hard" if trimp >= hard_cut else "easy"
        points.append({"day": n.day, "trimp": trimp, "kind": kind, "hrv": n.hrv,
                       "rhr": rhr.get(n.day)})  # fmt: skip
    buckets = []
    for kind, label in [("rest", "Rest day"), ("easy", "Easy day"), ("hard", "Hard day")]:
        pts = [p for p in points if p["kind"] == kind]
        hrv = [p["hrv"] for p in pts if p["hrv"] is not None]
        rhrs = [p["rhr"] for p in pts if p["rhr"] is not None]
        buckets.append({
            "label": label, "nights": len(pts),
            "hrv": round(float(np.mean(hrv)), 1) if hrv else None,
            "rhr": round(float(np.mean(rhrs)), 1) if rhrs else None,
        })  # fmt: skip
    return {"points": points, "buckets": buckets, "hard_cut": hard_cut}


def readiness(conn: sqlite3.Connection, start: date) -> dict:
    """Each run's HR vs expected against that morning's HRV (vs its normal range) and sleep."""
    hrv = {n.day: n for n in nights(conn, start) if n.hrv is not None and n.low is not None}
    scores = dict(
        conn.execute(
            "SELECT day, score FROM recovery_sleep WHERE score IS NOT NULL AND day >= ?",
            (start.isoformat(),),
        ).fetchall()
    )
    points = []
    for run in running_reports.hr_vs_expected(conn, start):
        n, score = hrv.get(run["day"]), scores.get(run["day"].isoformat())
        if n is None and score is None:
            continue
        state = hrv_pct = None
        if n:
            state = "below" if n.hrv < n.low else "above" if n.hrv > n.high else "in"
            hrv_pct = 100 * (n.hrv / ((n.low + n.high) / 2) - 1)
        sleep_kind = None
        if score is not None:
            sleep_kind = next(k for k, _, lo in reversed(SLEEP_BUCKETS) if score >= lo)
        points.append({**run, "hrv_state": state, "hrv_pct": hrv_pct, "sleep": sleep_kind})

    def summary(key: str, kind: str, label: str) -> dict:
        deltas = [p["delta"] for p in points if p[key] == kind]
        mean = round(float(np.mean(deltas)), 1) if deltas else None
        return {"label": label, "runs": len(deltas), "delta": mean}

    hrv_states = [("below", "Below normal range"), ("in", "In range"), ("above", "Above range")]
    return {
        "points": points,
        "by_hrv": [summary("hrv_state", k, lab) for k, lab in hrv_states],
        "by_sleep": [summary("sleep", k, lab) for k, lab, _ in SLEEP_BUCKETS],
    }


def sleep(conn: sqlite3.Connection, start: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT day, sleep_s, deep_s, light_s, rem_s, awake_s, score FROM recovery_sleep"
        " WHERE day >= ? ORDER BY day",
        (start.isoformat(),),
    ).fetchall()


def latest(conn: sqlite3.Connection, today: date) -> dict:
    """Most recent night's HRV (with its band), RHR and sleep, for the tile and stats."""
    recent = nights(conn, today - timedelta(days=13))
    night = next((n for n in reversed(recent) if n.hrv is not None), None)
    rhr = resting_hr(conn, today - timedelta(days=13))
    slp = conn.execute(
        "SELECT day, sleep_s, score FROM recovery_sleep ORDER BY day DESC LIMIT 1"
    ).fetchone()
    return {"night": night, "rhr": rhr[-1] if rhr else None, "sleep": slp}


def counts(conn: sqlite3.Connection) -> dict:
    return {
        t: conn.execute(f"SELECT count(*), min(day), max(day) FROM recovery_{t}").fetchone()
        for t in ("daily", "hrv", "sleep")
    }
