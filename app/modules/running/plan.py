"""Weekly training plan: planned sessions, matched to activities on the same local day."""

import sqlite3
import time
from datetime import date, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, Field

Kind = Literal["easy", "long", "tempo", "intervals", "recovery", "race", "cross", "rest"]
KINDS = get_args(Kind)
RUN_KINDS = KINDS[:6]  # matched to runs; "cross" matches non-run activities; "rest" matches none


class Session(BaseModel):
    day: date
    kind: Kind
    distance_m: float | None = Field(default=None, gt=0, le=200_000)
    duration_s: int | None = Field(default=None, gt=0, le=86_400)
    notes: str = Field(default="", max_length=200)


def save(conn: sqlite3.Connection, s: Session, session_id: int | None = None) -> None:
    """Insert a session, or replace the one with `session_id`."""
    values = (s.day.isoformat(), s.kind, s.distance_m, s.duration_s, s.notes.strip())
    if session_id is None:
        conn.execute(
            "INSERT INTO running_plan (day, kind, distance_m, duration_s, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (*values, time.time()),
        )
    else:
        conn.execute(
            "UPDATE running_plan SET day = ?, kind = ?, distance_m = ?, duration_s = ?, notes = ?"
            " WHERE id = ?",
            (*values, session_id),
        )


def get(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM running_plan WHERE id = ?", (session_id,)).fetchone()


def delete(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("DELETE FROM running_plan WHERE id = ?", (session_id,))


def copy_week(conn: sqlite3.Connection, src: date, dst: date) -> int:
    """Copy the sessions of week `src` into week `dst` (both Mondays); returns count."""
    shift = f"{(dst - src).days} days"
    cur = conn.execute(
        "INSERT INTO running_plan (day, kind, distance_m, duration_s, notes, created_at)"
        " SELECT date(day, ?), kind, distance_m, duration_s, notes, ? FROM running_plan"
        " WHERE day >= ? AND day < ? ORDER BY day, id",
        (shift, time.time(), src.isoformat(), (src + timedelta(days=7)).isoformat()),
    )
    return cur.rowcount


def week(conn: sqlite3.Connection, monday: date, today: date) -> dict:
    """Days of the week with (session, activity, status) items, plus planned vs done totals."""
    end = (monday + timedelta(days=7)).isoformat()
    sessions = conn.execute(
        "SELECT * FROM running_plan WHERE day >= ? AND day < ? ORDER BY day, id",
        (monday.isoformat(), end),
    ).fetchall()
    acts = conn.execute(
        "SELECT id, source_id, name, sport, start_local, date(start_local) AS day, distance_m,"
        " moving_s, avg_hr, is_run FROM running_activities"
        " WHERE start_local >= ? AND start_local < ? ORDER BY start_local",
        (monday.isoformat(), end),
    ).fetchall()
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        key = d.isoformat()
        items = _match(
            [s for s in sessions if s["day"] == key], [a for a in acts if a["day"] == key]
        )
        days.append({"day": d, "items": [(s, a, _status(s, a, d, today)) for s, a in items]})
    planned = [s for s in sessions if s["kind"] != "rest"]
    matched = sum(st == "done" for d in days for _, _, st in d["items"])
    return {
        "days": days,
        "sessions": len(sessions),
        "planned": len(planned),
        "done": matched,
        "planned_m": sum(s["distance_m"] or 0 for s in planned if s["kind"] in RUN_KINDS),
        "run_m": sum(a["distance_m"] for a in acts if a["is_run"]),
    }


def _match(sessions: list, acts: list) -> list[tuple]:
    """Pair sessions with the day's activities in time order; leftovers are unplanned."""
    pools = {True: [a for a in acts if a["is_run"]], False: [a for a in acts if not a["is_run"]]}
    items = []
    for s in sessions:
        pool = None if s["kind"] == "rest" else pools[s["kind"] in RUN_KINDS]
        items.append((s, pool.pop(0) if pool else None))
    leftovers = sorted(pools[True] + pools[False], key=lambda a: a["start_local"])
    return items + [(None, a) for a in leftovers]


def _status(s, a, d: date, today: date) -> str:
    if s is None:
        return "unplanned"
    if s["kind"] == "rest":
        return "rest"
    if a is not None:
        return "done"
    return "missed" if d < today else "today" if d == today else "planned"
