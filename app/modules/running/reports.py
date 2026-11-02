"""Read-side queries for running pages: period summaries, HR at pace, load, best efforts."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np

from app.core.charts import Series, day_epoch
from app.modules.running.analysis.efforts import DISTANCES_M, LABELS
from app.modules.running.analysis.load import fitness_curves, hrr, trimp
from app.modules.running.analysis.steady import MIN_SPEED
from app.modules.running.config import AthleteSettings
from app.modules.running.fmt import M_PER_UNIT

HR_PACE_MIN_S = {"week": 180, "month": 300}  # steady seconds a band needs to plot a point
HR_PACE_BANDS = 4
PROFILE_MIN_S = 300  # a band needs 5 steady minutes in the range to be listed

BUCKET_SQL = {
    "week": "date(a.start_local, 'weekday 0', '-6 days')",
    "month": "strftime('%Y-%m-01', a.start_local)",
    "year": "strftime('%Y-01-01', a.start_local)",
}


@dataclass(frozen=True)
class Range:
    key: str
    label: str
    days: int | None  # None = all time
    chart: str  # bucket for distance and efficiency charts
    table: str  # bucket for the periods table
    hr: str  # bucket for HR at pace


RANGES = {
    r.key: r
    for r in [
        Range("all", "All time", None, "month", "year", "month"),
        Range("1y", "1 year", 365, "week", "month", "month"),
        Range("6m", "6 months", 182, "week", "week", "month"),
        Range("3m", "3 months", 91, "week", "week", "week"),
        Range("1m", "1 month", 30, "week", "week", "week"),
    ]
}
DEFAULT_RANGE = "6m"


def get_range(key: str | None) -> Range:
    return RANGES.get(key or "", RANGES[DEFAULT_RANGE])


def range_start(conn: sqlite3.Connection, r: Range, today: date) -> date:
    """First day of the range; for all time, the day of the first run."""
    if r.days is not None:
        return today - timedelta(days=r.days)
    first = conn.execute("SELECT min(date(start_local)) FROM running_activities WHERE is_run")
    value = first.fetchone()[0]
    return date.fromisoformat(value) if value else today


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def floor(d: date, bucket: str) -> date:
    if bucket == "week":
        return week_start(d)
    return d.replace(day=1) if bucket == "month" else date(d.year, 1, 1)


def buckets(start: date, end: date, bucket: str) -> list[date]:
    """Bucket start dates from the one containing `start` through the one containing `end`."""
    d, out = floor(start, bucket), []
    while d <= end:
        out.append(d)
        if bucket == "week":
            d += timedelta(weeks=1)
        elif bucket == "month":
            d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        else:
            d = date(d.year + 1, 1, 1)
    return out


def local_epoch(start_local: str) -> int:
    """Wall-clock time as if UTC, so charts show local dates like the period buckets."""
    return int(datetime.fromisoformat(start_local).replace(tzinfo=UTC).timestamp())


EMPTY_PERIOD = {
    "runs": 0, "distance_m": 0.0, "moving_s": 0, "longest_m": 0.0, "trimp": 0.0, "ef": None,
    "cadence": None,
    "z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0,
}  # fmt: skip


def periods(conn: sqlite3.Connection, start: date, bucket: str, today: date) -> list[dict]:
    """One summary row per week/month/year (oldest first), including empty ones."""
    keys = buckets(start, today, bucket)
    rows = conn.execute(
        f"SELECT {BUCKET_SQL[bucket]} AS period, count(*) AS runs,"
        " sum(a.distance_m) AS distance_m, sum(a.moving_s) AS moving_s,"
        " max(a.distance_m) AS longest_m, coalesce(sum(m.trimp), 0) AS trimp,"
        " sum(m.ef * a.moving_s) / sum(CASE WHEN m.ef IS NOT NULL THEN a.moving_s END) AS ef,"
        " sum(m.cadence_spm * a.moving_s)"
        "   / sum(CASE WHEN m.cadence_spm IS NOT NULL THEN a.moving_s END) AS cadence,"
        " coalesce(sum(m.z1_s), 0) AS z1, coalesce(sum(m.z2_s), 0) AS z2,"
        " coalesce(sum(m.z3_s), 0) AS z3, coalesce(sum(m.z4_s), 0) AS z4,"
        " coalesce(sum(m.z5_s), 0) AS z5"
        " FROM running_activities a LEFT JOIN running_metrics m ON m.activity_id = a.id"
        " WHERE a.is_run AND a.start_local >= ? GROUP BY period",
        (keys[0].isoformat(),),
    )
    by_period = {r["period"]: dict(r) for r in rows}
    out = []
    for d in keys:
        row = {**EMPTY_PERIOD, **by_period.get(d.isoformat(), {}), "start": d}
        row["zones"] = [row[f"z{z}"] for z in range(1, 6)]
        out.append(row)
    return out


def totals(conn: sqlite3.Connection, start: date) -> dict:
    row = conn.execute(
        "SELECT count(*) AS runs, coalesce(sum(a.distance_m), 0) AS distance_m,"
        " coalesce(sum(a.moving_s), 0) AS moving_s, coalesce(max(a.distance_m), 0) AS longest_m,"
        " coalesce(sum(m.z1_s), 0) AS z1, coalesce(sum(m.z2_s), 0) AS z2,"
        " coalesce(sum(m.z3_s), 0) AS z3, coalesce(sum(m.z4_s), 0) AS z4,"
        " coalesce(sum(m.z5_s), 0) AS z5"
        " FROM running_activities a LEFT JOIN running_metrics m ON m.activity_id = a.id"
        " WHERE a.is_run AND a.start_local >= ?",
        (start.isoformat(),),
    ).fetchone()
    return {**dict(row), "intensity": intensity([row[f"z{z}"] for z in range(1, 6)])}


def intensity(zones: list[float]) -> tuple[float, float, float] | None:
    """Share of HR time (%) that is low (Z1-2), moderate (Z3), high (Z4-5): Seiler's 3 zones.

    Approximate: the 3-zone model uses ventilatory thresholds; we map from %max HR zones.
    """
    total = sum(zones)
    if total < 60:
        return None
    low, moderate, high = zones[0] + zones[1], zones[2], zones[3] + zones[4]
    return tuple(round(100 * x / total, 1) for x in (low, moderate, high))


SPIKE_PCT = 10  # Frandsen et al. 2025: a run >10% longer than the 30-day longest raises injury risk


def run_spikes(conn: sqlite3.Connection, start: date) -> list[sqlite3.Row]:
    """Runs since `start` with the longest run in the 30 days before each, and % over it."""
    return conn.execute(
        "WITH r AS (SELECT id, source_id, name, start_local, distance_m,"
        "  max(distance_m) OVER (ORDER BY julianday(start_local)"
        "    RANGE BETWEEN 30 PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW) AS prior_max"
        "  FROM running_activities WHERE is_run)"
        " SELECT *, 100.0 * (distance_m / prior_max - 1) AS pct FROM r"
        " WHERE start_local >= ? ORDER BY start_local",
        (start.isoformat(),),
    ).fetchall()


def spike_pcts(conn: sqlite3.Connection, start: date) -> dict[int, float]:
    """{activity id: % over the prior 30-day longest} for spiking runs since `start`."""
    return {r["id"]: r["pct"] for r in run_spikes(conn, start) if (r["pct"] or 0) > SPIKE_PCT}


def calendar(conn: sqlite3.Connection, start: date, today: date) -> tuple[list[dict], list[float]]:
    """Daily run distance as one grid per year (newest first), with quartile level 0-4.

    Returns (years, level thresholds in m). Each year has `weeks`: lists of 7 cells
    (Mon..Sun), a cell being None outside the range or {"day", "m", "level"}.
    """
    by_day = dict(
        conn.execute(
            "SELECT date(start_local), sum(distance_m) FROM running_activities"
            " WHERE is_run AND start_local >= ? GROUP BY 1",
            (start.isoformat(),),
        ).fetchall()
    )
    values = [v for v in by_day.values() if v > 0]
    cuts = np.percentile(values, [25, 50, 75]).tolist() if values else []
    years = []
    for year in range(today.year, start.year - 1, -1):
        first, last = max(start, date(year, 1, 1)), min(today, date(year, 12, 31))
        weeks, d = [], week_start(first)
        while d <= last:
            week = []
            for i in range(7):
                day = d + timedelta(days=i)
                m = by_day.get(day.isoformat(), 0.0)
                level = 0 if not m else 1 + sum(m > c for c in cuts)
                week.append({"day": day, "m": m, "level": level} if first <= day <= last else None)
            weeks.append(week)
            d += timedelta(weeks=1)
        years.append({"year": year, "weeks": weeks})
    return years, cuts


def per_run(conn: sqlite3.Connection, since: date | None = None) -> list[sqlite3.Row]:
    """Runs with summary avg HR (and stream EF and cadence when available), oldest first."""
    return conn.execute(
        "SELECT a.id, a.start_local, a.distance_m, a.moving_s, a.avg_hr, m.ef, m.cadence_spm"
        " FROM running_activities a LEFT JOIN running_metrics m ON m.activity_id = a.id"
        " WHERE a.is_run AND a.distance_m > 0 AND a.start_local >= ? ORDER BY a.start_local",
        ((since or date.min).isoformat(),),
    ).fetchall()


def _band_sql(units: str, width_s: int) -> tuple[str, tuple]:
    """SQL for a pace band floor in the display unit, plus its parameters."""
    return "CAST(b.pace_s * ? / ? AS INTEGER) * ?", (M_PER_UNIT[units] / 1000, width_s, width_s)


def hr_at_pace(
    conn: sqlite3.Connection,
    units: str,
    start: date,
    bucket: str,
    today: date,
    width_s: int = 15,
    trim: bool = False,
) -> tuple[list[int], list[tuple[int, Series]], list[date]]:
    """Avg steady-state HR per period for the most-run pace bands in the range.

    The x axis spans the range, or starts at the first data when `trim` (all time).
    Returns (period epochs, [(band start s/unit, series)], period dates).
    """
    band, params = _band_sql(units, width_s)
    rows = conn.execute(
        f"SELECT {BUCKET_SQL[bucket]} AS period, {band} AS band,"
        " sum(b.seconds) AS secs, sum(b.avg_hr * b.seconds) / sum(b.seconds) AS hr"
        " FROM running_pace_bins b JOIN running_activities a ON a.id = b.activity_id"
        " WHERE a.is_run AND a.start_local >= ? GROUP BY period, band",
        (*params, floor(start, bucket).isoformat()),
    ).fetchall()
    if not rows:
        return [], [], []
    totals_by_band: dict[int, float] = {}
    for r in rows:
        totals_by_band[r["band"]] = totals_by_band.get(r["band"], 0) + r["secs"]
    top = sorted(totals_by_band, key=totals_by_band.get, reverse=True)[:HR_PACE_BANDS]
    first_data = date.fromisoformat(min(r["period"] for r in rows))
    keys = buckets(first_data if trim else start, today, bucket)
    min_s = HR_PACE_MIN_S[bucket]
    cells = {(r["period"], r["band"]): r for r in rows if r["secs"] >= min_s}
    series = [
        (b, Series(str(b), [_hr(cells.get((d.isoformat(), b))) for d in keys])) for b in sorted(top)
    ]
    return [day_epoch(d) for d in keys], series, keys


def _hr(row) -> float | None:
    return round(row["hr"], 1) if row else None


def pace_profile(conn: sqlite3.Connection, units: str, start: date, width_s: int) -> list:
    """Steady minutes, avg HR and avg cadence per pace band over the range, fastest first."""
    band, params = _band_sql(units, width_s)
    return conn.execute(
        f"SELECT {band} AS band, sum(b.seconds) AS secs,"
        " sum(b.avg_hr * b.seconds) / sum(b.seconds) AS hr,"
        " sum(b.avg_cadence * b.seconds)"
        "   / sum(CASE WHEN b.avg_cadence IS NOT NULL THEN b.seconds END) AS cadence"
        " FROM running_pace_bins b JOIN running_activities a ON a.id = b.activity_id"
        " WHERE a.is_run AND a.start_local >= ? GROUP BY band HAVING secs >= ? ORDER BY band",
        (*params, start.isoformat(), PROFILE_MIN_S),
    ).fetchall()


def daily_load(
    conn: sqlite3.Connection, athlete: AthleteSettings, today: date
) -> tuple[date | None, np.ndarray]:
    """Daily TRIMP from the first run with HR through `today` (local days).

    Load is the stream TRIMP when available, else an estimate from summary avg HR.
    Also read by the recovery module.
    """
    rows = conn.execute(
        "SELECT date(a.start_local) AS day, m.trimp, a.moving_s, a.avg_hr"
        " FROM running_activities a LEFT JOIN running_metrics m ON m.activity_id = a.id"
        " WHERE a.is_run AND (m.trimp IS NOT NULL OR a.avg_hr > 0) ORDER BY day"
    ).fetchall()
    if not rows:
        return None, np.zeros(0)
    first = date.fromisoformat(rows[0]["day"])
    load = np.zeros(max((today - first).days + 1, 1))
    for r in rows:
        i = (date.fromisoformat(r["day"]) - first).days
        if i < len(load):
            load[i] += (
                r["trimp"]
                if r["trimp"] is not None
                else trimp(r["moving_s"] / 60, hrr(r["avg_hr"], athlete.resting_hr, athlete.max_hr))
            )
    return first, load


def fitness(
    conn: sqlite3.Connection, athlete: AthleteSettings, start: date, today: date
) -> tuple[list[date], np.ndarray, np.ndarray, np.ndarray]:
    """Daily CTL/ATL/TSB from the first run with HR, returned from `start` on."""
    first, load = daily_load(conn, athlete, today)
    if first is None:
        empty = np.zeros(0)
        return [], empty, empty, empty
    ctl, atl, tsb = fitness_curves(load)
    skip = min(max((start - first).days, 0), len(load) - 1)
    days = [first + timedelta(days=i) for i in range(skip, len(load))]
    return days, ctl[skip:], atl[skip:], tsb[skip:]


MONOTONY_HIGH = 2.0  # Foster 1998: weekly monotony above this went with illness at high load


def _monotony(week: np.ndarray) -> float | None:
    """Foster's monotony: mean daily load / its SD, rest days included."""
    sd = float(np.std(week))
    return float(np.mean(week)) / sd if sd > 0 else None


def weekly_strain(
    conn: sqlite3.Connection, athlete: AthleteSettings, start: date, today: date
) -> tuple[list[dict], float | None]:
    """Per complete Mon-Sun week: load, monotony and strain (load x monotony), oldest first.

    Also returns monotony over the last 7 days.
    """
    first, load = daily_load(conn, athlete, today)
    if first is None:
        return [], None
    out = []
    for w in buckets(max(start, first), today - timedelta(days=6), "week"):
        i = (w - first).days
        if i < 0:
            continue
        week = load[i : i + 7]
        mono = _monotony(week)
        total = float(week.sum())
        out.append({"start": w, "load": total, "monotony": mono,
                    "strain": total * mono if mono is not None else None})  # fmt: skip
    return out, _monotony(load[-7:]) if len(load) >= 7 else None


RIEGEL_EXP = 1.06
ESTIMATE_DAYS = 90
# Efforts from a mile up; Riegel's formula overpredicts endurance from shorter ones.
ESTIMATE_FROM_M = (1609.344, 5000.0, 10000.0, 21097.5)
PREDICT_M = (5000.0, 10000.0, 21097.5)


def riegel(seconds: float, from_m: float, to_m: float) -> float:
    """Riegel (1981) equivalent time: t2 = t1 * (d2 / d1) ** 1.06."""
    return seconds * (to_m / from_m) ** RIEGEL_EXP


def _efforts(conn: sqlite3.Connection, since: date) -> list[dict]:
    rows = conn.execute(
        "SELECT date(a.start_local) AS day, a.source_id, b.distance_m, b.seconds"
        " FROM running_best b JOIN running_activities a ON a.id = b.activity_id"
        f" WHERE a.is_run AND b.distance_m IN ({','.join('?' * len(ESTIMATE_FROM_M))})"
        " AND a.start_local >= ? ORDER BY day",
        (*ESTIMATE_FROM_M, since.isoformat()),
    ).fetchall()
    return [
        {**dict(r), "day": date.fromisoformat(r["day"]),
         "equiv_5k": riegel(r["seconds"], r["distance_m"], 5000.0)}
        for r in rows
    ]  # fmt: skip


def _best_equiv(efforts: list[dict], end: date) -> dict | None:
    """The effort with the fastest 5 km equivalent in the ESTIMATE_DAYS ending at `end`."""
    window = [e for e in efforts if 0 <= (end - e["day"]).days < ESTIMATE_DAYS]
    return min(window, key=lambda e: e["equiv_5k"], default=None)


def race_estimates(
    conn: sqlite3.Connection, start: date, bucket: str, today: date
) -> tuple[list[date], list[float | None]]:
    """Estimated 5 km time per period, from the best effort in the 90 days to its end."""
    efforts = _efforts(conn, start - timedelta(days=ESTIMATE_DAYS))
    keys = buckets(start, today, bucket)
    ends = [k - timedelta(days=1) for k in keys[1:]] + [today]
    best = [_best_equiv(efforts, end) for end in ends]
    return keys, [round(b["equiv_5k"]) if b else None for b in best]


def race_predictions(conn: sqlite3.Connection, today: date) -> dict | None:
    """Predicted times now for PREDICT_M, and the effort they come from."""
    src = _best_equiv(_efforts(conn, today - timedelta(days=ESTIMATE_DAYS)), today)
    if src is None:
        return None
    times = [(LABELS[d], d, riegel(src["equiv_5k"], 5000.0, d)) for d in PREDICT_M]
    return {"source": src, "source_label": LABELS[src["distance_m"]], "times": times}


EXPECT_DAYS, EXPECT_MIN_RUNS = 90, 10


def hr_vs_expected(conn: sqlite3.Connection, start: date) -> list[dict]:
    """Runs from `start` with avg HR minus the HR expected at their pace, in bpm.

    Expected HR is a straight line (speed -> HR) fit on the EXPECT_DAYS of runs before each,
    so it follows changes in fitness. Runs with too little history are skipped.
    Read by the recovery module.
    """
    rows = conn.execute(
        "SELECT date(start_local) AS day, distance_m / moving_s AS speed, avg_hr"
        " FROM running_activities WHERE is_run AND avg_hr > 0 AND moving_s > 0"
        " AND distance_m / moving_s >= ? AND start_local >= ? ORDER BY start_local",
        (MIN_SPEED, (start - timedelta(days=EXPECT_DAYS)).isoformat()),
    ).fetchall()
    runs = [(date.fromisoformat(r["day"]), r["speed"], r["avg_hr"]) for r in rows]
    out = []
    for day, speed, hr in runs:
        if day < start:
            continue
        prior = [(s, h) for d, s, h in runs if 0 < (day - d).days <= EXPECT_DAYS]
        speeds = np.array([s for s, _ in prior])
        if len(prior) < EXPECT_MIN_RUNS or np.ptp(speeds) == 0:
            continue
        slope, icept = np.polyfit(speeds, [h for _, h in prior], 1)
        out.append({"day": day, "delta": float(hr - (slope * speed + icept))})
    return out


def best_efforts(conn: sqlite3.Connection, start: date) -> list[dict]:
    """Fastest time per standard distance in the range and all time."""
    q = (
        "SELECT b.seconds, a.start_local, a.source_id FROM running_best b"
        " JOIN running_activities a ON a.id = b.activity_id"
        " WHERE a.is_run AND b.distance_m = ? AND a.start_local >= ? ORDER BY b.seconds LIMIT 1"
    )
    out = []
    for d in DISTANCES_M:
        best_all = conn.execute(q, (d, "")).fetchone()
        if best_all is None:
            continue
        best = conn.execute(q, (d, start.isoformat())).fetchone()
        out.append({"label": LABELS[d], "distance_m": d, "range": best, "all": best_all})
    return out


def activities(conn: sqlite3.Connection, limit: int, offset: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, m.ef, m.decoupling_pct, m.trimp, m.steady_s, m.gap_s_km, m.cadence_spm"
        " FROM running_activities a LEFT JOIN running_metrics m ON m.activity_id = a.id"
        " ORDER BY a.start_local DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM running_activities WHERE is_run ORDER BY start_local DESC LIMIT 1"
    ).fetchone()


def sync_counts(conn: sqlite3.Connection) -> dict:
    """Activity counts by streams status plus running job counts by status."""
    streams = dict(
        conn.execute(
            "SELECT streams_status, count(*) FROM running_activities GROUP BY streams_status"
        ).fetchall()
    )
    jobs = dict(
        conn.execute(
            "SELECT status, count(*) FROM jobs WHERE kind LIKE 'running.%' GROUP BY status"
        ).fetchall()
    )
    total = conn.execute("SELECT count(*) FROM running_activities").fetchone()[0]
    return {"total": total, "streams": streams, "jobs": jobs}


def failed_jobs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, last_error, updated_at FROM jobs"
        " WHERE kind LIKE 'running.%' AND status = 'failed' ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
