"""Persistence for recovery data: daily, HRV and sleep rows (upserted), and KV documents."""

import dataclasses
import sqlite3
import time

from pydantic import BaseModel

from app.core import kv
from app.modules.recovery.sources.base import Daily, Hrv, Sleep

TABLES = {Daily: "recovery_daily", Hrv: "recovery_hrv", Sleep: "recovery_sleep"}


def kv_get[M: BaseModel](conn: sqlite3.Connection, key: str, model: type[M]) -> M | None:
    return kv.get(conn, "recovery_kv", key, model)


def kv_set(conn: sqlite3.Connection, key: str, value: BaseModel) -> None:
    kv.put(conn, "recovery_kv", key, value)


def upsert(conn: sqlite3.Connection, rows: list[Daily] | list[Hrv] | list[Sleep]) -> None:
    """Insert or replace day rows (caller commits). Recent days change as the watch syncs."""
    if not rows:
        return
    cols = [f.name for f in dataclasses.fields(rows[0])] + ["fetched_at"]
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "day")
    sql = (
        f"INSERT INTO {TABLES[type(rows[0])]} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' * len(cols))}) ON CONFLICT (day) DO UPDATE SET {updates}"
    )
    now = time.time()
    conn.executemany(sql, [(*dataclasses.astuple(r), now) for r in rows])
