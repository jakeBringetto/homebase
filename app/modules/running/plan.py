"""Training plan: phases, weekly targets, and sessions matched to same-day activities."""

import sqlite3
import time
from datetime import date, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

Kind = Literal["easy", "long", "tempo", "intervals", "recovery", "race", "cross", "rest"]
KINDS = get_args(Kind)
RUN_KINDS = KINDS[:6]  # matched to runs; "cross" matches non-run activities; "rest" matches none
QUALITY_KINDS = ("tempo", "intervals")


class Session(BaseModel):
    day: date
    kind: Kind
    distance_m: float | None = Field(default=None, gt=0, le=200_000)
    duration_s: int | None = Field(default=None, gt=0, le=86_400)
    notes: str = Field(default="", max_length=200)


class Phase(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    start_day: date
    end_day: date
    notes: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _ordered(self) -> "Phase":
        if self.end_day < self.start_day:
            raise ValueError("end must be on or after start")
        return self


class Target(BaseModel):
    week: date
    distance_m: float | None = Field(default=None, gt=0, le=1_000_000)
    long_m: float | None = Field(default=None, gt=0, le=200_000)
    quality: int | None = Field(default=None, ge=0, le=14)
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


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def save_phase(conn: sqlite3.Connection, p: Phase, phase_id: int | None = None) -> None:
    """Insert a phase, or replace the one with `phase_id`."""
    values = (p.name.strip(), p.start_day.isoformat(), p.end_day.isoformat(), p.notes.strip())
    if phase_id is None:
        conn.execute(
            "INSERT INTO running_plan_phases (name, start_day, end_day, notes) VALUES (?, ?, ?, ?)",
            values,
        )
    else:
        conn.execute(
            "UPDATE running_plan_phases SET name = ?, start_day = ?, end_day = ?, notes = ?"
            " WHERE id = ?",
            (*values, phase_id),
        )


def get_phase(conn: sqlite3.Connection, phase_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM running_plan_phases WHERE id = ?", (phase_id,)).fetchone()


def delete_phase(conn: sqlite3.Connection, phase_id: int) -> None:
    conn.execute("DELETE FROM running_plan_phases WHERE id = ?", (phase_id,))


def phases(conn: sqlite3.Connection) -> list[dict]:
    """All phases in date order, each with its Monday and length in weeks."""
    rows = conn.execute("SELECT * FROM running_plan_phases ORDER BY start_day, id").fetchall()
    out = []
    for r in rows:
        first = _monday(date.fromisoformat(r["start_day"]))
        weeks = (_monday(date.fromisoformat(r["end_day"])) - first).days // 7 + 1
        out.append({**dict(r), "monday": first, "weeks": weeks})
    return out


def phase_for(conn: sqlite3.Connection, monday: date) -> dict | None:
    """The first phase overlapping this week, with `week_no` (1-based) added."""
    sunday = monday + timedelta(days=6)
    for p in phases(conn):
        if p["start_day"] <= sunday.isoformat() and p["end_day"] >= monday.isoformat():
            return {**p, "week_no": (monday - p["monday"]).days // 7 + 1}
    return None


def set_target(conn: sqlite3.Connection, t: Target) -> None:
    """Upsert the week's target; an all-empty target deletes it."""
    week = _monday(t.week).isoformat()
    if t.distance_m is None and t.long_m is None and t.quality is None and not t.notes.strip():
        conn.execute("DELETE FROM running_plan_targets WHERE week = ?", (week,))
        return
    conn.execute(
        "INSERT INTO running_plan_targets (week, distance_m, long_m, quality, notes)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (week) DO UPDATE SET distance_m = excluded.distance_m,"
        " long_m = excluded.long_m, quality = excluded.quality, notes = excluded.notes",
        (week, t.distance_m, t.long_m, t.quality, t.notes.strip()),
    )


def get_target(conn: sqlite3.Connection, monday: date) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM running_plan_targets WHERE week = ?", (monday.isoformat(),)
    ).fetchone()


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
        "long_m": max((a["distance_m"] for a in acts if a["is_run"]), default=0),
        "quality_done": sum(
            st == "done" and s["kind"] in QUALITY_KINDS for d in days for s, _, st in d["items"]
        ),
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
