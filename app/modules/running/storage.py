"""Persistence for running data: activities, streams, metrics, and small KV documents."""

import io
import sqlite3
import time

import numpy as np
from pydantic import BaseModel

from app.core import kv
from app.core.jobs import enqueue
from app.modules.running.analysis.metrics import Analysis
from app.modules.running.config import AthleteSettings
from app.modules.running.sources.base import Activity, Gear, Streams


def kv_get[M: BaseModel](conn: sqlite3.Connection, key: str, model: type[M]) -> M | None:
    return kv.get(conn, "running_kv", key, model)


def kv_set(conn: sqlite3.Connection, key: str, value: BaseModel) -> None:
    kv.put(conn, "running_kv", key, value)


def kv_delete(conn: sqlite3.Connection, key: str) -> None:
    kv.delete(conn, "running_kv", key)


def athlete_settings(conn: sqlite3.Connection) -> AthleteSettings:
    return kv_get(conn, "athlete", AthleteSettings) or AthleteSettings()


def upsert_activity(conn: sqlite3.Connection, a: Activity, fetch_streams: bool) -> None:
    """Insert or update an activity; optionally queue a one-time streams fetch for a new run."""
    row = conn.execute(
        "INSERT INTO running_activities (source, source_id, sport, name, start_time, start_local,"
        " moving_s, elapsed_s, distance_m, elev_gain_m, avg_hr, max_hr, is_run, ride_like,"
        " is_manual, gear_id, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (source, source_id) DO UPDATE SET sport = excluded.sport,"
        " name = excluded.name, moving_s = excluded.moving_s, elapsed_s = excluded.elapsed_s,"
        " gear_id = excluded.gear_id,"
        " distance_m = excluded.distance_m, elev_gain_m = excluded.elev_gain_m,"
        " avg_hr = excluded.avg_hr, max_hr = excluded.max_hr,"
        " ride_like = max(ride_like, excluded.ride_like),"  # keep a streams-based flag
        " is_run = excluded.is_run AND NOT max(ride_like, excluded.ride_like),"
        " updated_at = excluded.updated_at"
        " RETURNING id, streams_status",
        (a.source, a.source_id, a.sport, a.name, a.start_time, a.start_local, a.moving_s,
         a.elapsed_s, a.distance_m, a.elev_gain_m, a.avg_hr, a.max_hr, a.is_run, a.ride_like,
         a.is_manual, a.gear_id, time.time()),
    ).fetchone()  # fmt: skip
    if fetch_streams and a.is_run and not a.is_manual and row["streams_status"] == "none":
        queue_streams(conn, row["id"])


def unknown_gear(conn: sqlite3.Connection) -> list[str]:
    """Gear ids on runs with no `running_gear` row yet."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT gear_id FROM running_activities WHERE is_run AND gear_id IS NOT NULL"
            " AND gear_id NOT IN (SELECT id FROM running_gear) ORDER BY gear_id"
        )
    ]


def save_gear(conn: sqlite3.Connection, gear_id: str, g: Gear | None) -> None:
    """Store gear details once; a None (unavailable) row stops refetching."""
    conn.execute(
        "INSERT OR REPLACE INTO running_gear (id, name, retired, fetched_at) VALUES (?, ?, ?, ?)",
        (gear_id, g and g.name, bool(g and g.retired), time.time()),
    )


def queue_streams(conn: sqlite3.Connection, activity_id: int, priority: int = 0) -> None:
    """Queue a one-time streams fetch (caller commits)."""
    enqueue(
        conn,
        "running.streams",
        {"activity_id": activity_id},
        priority=priority,
        dedupe_key=f"running.streams:{activity_id}",
    )
    set_streams_status(conn, activity_id, "queued")


def set_streams_status(conn: sqlite3.Connection, activity_id: int, status: str) -> None:
    conn.execute(
        "UPDATE running_activities SET streams_status = ? WHERE id = ?", (status, activity_id)
    )


def save_streams(conn: sqlite3.Connection, activity_id: int, streams: Streams) -> None:
    buf = io.BytesIO()
    np.savez_compressed(buf, **{k: v.astype(np.float32) for k, v in streams.items()})
    conn.execute(
        "INSERT OR REPLACE INTO running_streams (activity_id, data) VALUES (?, ?)",
        (activity_id, buf.getvalue()),
    )
    set_streams_status(conn, activity_id, "stored")


def load_streams(conn: sqlite3.Connection, activity_id: int) -> Streams | None:
    row = conn.execute(
        "SELECT data FROM running_streams WHERE activity_id = ?", (activity_id,)
    ).fetchone()
    if row is None:
        return None
    with np.load(io.BytesIO(row[0])) as npz:
        return {k: npz[k] for k in npz.files}


def save_analysis(conn: sqlite3.Connection, activity_id: int, a: Analysis) -> None:
    """Replace an activity's metrics, pace bins, and best efforts; flag ride-like runs."""
    conn.execute(
        "INSERT OR REPLACE INTO running_metrics (activity_id, z1_s, z2_s, z3_s, z4_s, z5_s, ef,"
        " decoupling_pct, trimp, steady_s, gap_s_km, cadence_spm)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (activity_id, *a.zone_s, a.ef, a.decoupling_pct, a.trimp, a.steady_s, a.gap_s_km,
         a.cadence_spm),
    )  # fmt: skip
    conn.execute("DELETE FROM running_pace_bins WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        "INSERT INTO running_pace_bins (activity_id, pace_s, seconds, avg_hr, avg_cadence)"
        " VALUES (?, ?, ?, ?, ?)",
        [(activity_id, band, *values) for band, values in a.pace_bins.items()],
    )
    conn.execute("DELETE FROM running_best WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        "INSERT INTO running_best (activity_id, distance_m, seconds) VALUES (?, ?, ?)",
        [(activity_id, d, secs) for d, secs in a.best.items()],
    )
    if a.ride_like:
        conn.execute(
            "UPDATE running_activities SET ride_like = 1, is_run = 0 WHERE id = ?", (activity_id,)
        )
