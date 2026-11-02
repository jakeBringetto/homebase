"""Training and sleep, both directions: a few pairs chosen in advance, each with Spearman's rho,
a 95% CI and n.

Not a heatmap of every metric at every lag: with ~100 nights a grid like that shows several
"significant" cells by chance. HRV is taken against its own 7-night baseline, which removes slow
trends that would otherwise make unrelated series look correlated.
"""

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from app.modules.recovery import reports
from app.modules.running import reports as running_reports
from app.modules.running import storage as running_storage

BASELINE_NIGHTS, BASELINE_MIN = 7, 4
MIN_N = 10  # below this no rho is shown


@dataclass(frozen=True)
class Pair:
    key: str
    x_label: str
    y_label: str
    lag: str  # when y is measured relative to x


PAIRS = [
    Pair("load_hrv1", "Training load (TRIMP)", "HRV vs baseline (%)", "next morning"),
    Pair("load_hrv2", "Training load (TRIMP)", "HRV vs baseline (%)", "2 mornings later"),
    Pair("load_hrv3", "Training load (TRIMP)", "HRV vs baseline (%)", "3 mornings later"),
    Pair("load_score", "Training load (TRIMP)", "Sleep score", "that night"),
    Pair("load_deep", "Training load (TRIMP)", "Deep sleep (min)", "that night"),
    Pair("score_run", "Sleep score", "Run HR vs expected (bpm)", "that day's run"),
    Pair("hrv_run", "HRV vs baseline (%)", "Run HR vs expected (bpm)", "that day's run"),
]
CHARTS = {"load_hrv1", "load_score", "score_run"}


def _ranks(x: np.ndarray) -> np.ndarray:
    """Ranks starting at 1, ties averaged."""
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(1, len(x) + 1)
    for v in np.unique(x):
        tied = x == v
        if tied.sum() > 1:
            ranks[tied] = ranks[tied].mean()
    return ranks


def spearman(xs: list[float], ys: list[float]) -> dict:
    """rho with a 95% CI (Fisher z, Bonett-Wright SE); rho None below MIN_N or with no spread."""
    n = len(xs)
    out = {"n": n, "rho": None, "lo": None, "hi": None}
    if n < MIN_N:
        return out
    rx, ry = _ranks(np.asarray(xs, float)), _ranks(np.asarray(ys, float))
    if np.ptp(rx) == 0 or np.ptp(ry) == 0:
        return out
    rho = float(np.corrcoef(rx, ry)[0, 1])
    z, se = math.atanh(max(min(rho, 0.9999), -0.9999)), math.sqrt((1 + rho**2 / 2) / (n - 3))
    return {"n": n, "rho": rho, "lo": math.tanh(z - 1.96 * se), "hi": math.tanh(z + 1.96 * se)}


def hrv_vs_baseline(conn: sqlite3.Connection, start: date) -> dict[date, float]:
    """Each night's HRV as % off the mean of up to 7 nights before it (needs 4)."""
    ns = [n for n in reports.nights(conn, start - timedelta(days=BASELINE_NIGHTS)) if n.hrv]
    out = {}
    for n in ns:
        prior = [m.hrv for m in ns if 0 < (n.day - m.day).days <= BASELINE_NIGHTS]
        if n.day >= start and len(prior) >= BASELINE_MIN:
            out[n.day] = 100 * (n.hrv / float(np.mean(prior)) - 1)
    return out


def _run_delta(conn: sqlite3.Connection, start: date) -> dict[date, float]:
    by_day: dict[date, list[float]] = {}
    for r in running_reports.hr_vs_expected(conn, start):
        by_day.setdefault(r["day"], []).append(r["delta"])
    return {d: float(np.mean(v)) for d, v in by_day.items()}


def points(conn: sqlite3.Connection, start: date, today: date) -> dict[str, list[tuple]]:
    """(x, y) per pair, one point per day that has both."""
    athlete = running_storage.athlete_settings(conn)
    first, load_arr = running_reports.daily_load(conn, athlete, today)

    def load(d: date) -> float | None:
        if first is None or d < first or (d - first).days >= len(load_arr):
            return None
        return float(load_arr[(d - first).days])

    hrv = hrv_vs_baseline(conn, start)
    sleep = {
        date.fromisoformat(r["day"]): r
        for r in conn.execute(
            "SELECT day, score, deep_s FROM recovery_sleep WHERE day >= ?", (start.isoformat(),)
        )
    }
    runs = _run_delta(conn, start)

    def after(ys: dict, days: int, value=lambda v: v) -> list[tuple]:
        """Training load on D against y on wake-up day D + days."""
        pts = []
        for d, v in sorted(ys.items()):
            x, y = load(d - timedelta(days=days)), value(v)
            if x is not None and y is not None and d - timedelta(days=days) >= start:
                pts.append((x, y))
        return pts

    def same_day(xs: dict, value=lambda v: v) -> list[tuple]:
        return [(value(xs[d]), y) for d, y in sorted(runs.items())
                if d in xs and value(xs[d]) is not None]  # fmt: skip

    return {
        "load_hrv1": after(hrv, 1),
        "load_hrv2": after(hrv, 2),
        "load_hrv3": after(hrv, 3),
        "load_score": after(sleep, 1, lambda r: r["score"]),
        "load_deep": after(sleep, 1, lambda r: r["deep_s"] / 60 if r["deep_s"] else None),
        "score_run": same_day(sleep, lambda r: r["score"]),
        "hrv_run": same_day(hrv),
    }


def table(pts: dict[str, list[tuple]]) -> list[dict]:
    rows = []
    for p in PAIRS:
        s = spearman([x for x, _ in pts[p.key]], [y for _, y in pts[p.key]])
        clear = s["rho"] is not None and (s["lo"] > 0 or s["hi"] < 0)
        rows.append({"pair": p, **s, "clear": clear})
    return rows
