"""Job handler: Garmin sync. The first run backfills a year; later runs re-fetch recent days."""

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import date, timedelta

from pydantic import BaseModel

from app.modules.recovery import config, storage
from app.modules.recovery.sources import garmin

log = logging.getLogger(__name__)

STATE_KEY = "sync_state"
# Garmin may keep only a rolling year (unconfirmed), so fetch it all once and keep it.
HISTORY_DAYS = 365
FRESH_S = 86400


class SyncState(BaseModel):
    through: dict[str, str] = {}  # kind -> last local day fetched (YYYY-MM-DD)
    last_full_sync: float | None = None


def sync_state(conn: sqlite3.Connection) -> SyncState:
    return storage.kv_get(conn, STATE_KEY, SyncState) or SyncState()


def _kinds(src: garmin.GarminSource) -> list[tuple[str, Callable, int, int]]:
    """(kind, fetch, max days per call, trailing days to re-fetch)."""
    return [
        ("daily", src.daily, garmin.DAILY_DAYS, 3),
        ("hrv", src.hrv, garmin.HRV_DAYS, 7),
        ("sleep", src.sleep, garmin.SLEEP_DAYS, 7),
    ]


def is_fresh(conn: sqlite3.Connection, st: SyncState, today: date) -> bool:
    """Synced within a day and last night's HRV and sleep are already in."""
    if st.last_full_sync is None or time.time() - st.last_full_sync > FRESH_S:
        return False
    day = today.isoformat()
    hrv = conn.execute(
        "SELECT 1 FROM recovery_hrv WHERE day = ? AND last_night_avg IS NOT NULL", (day,)
    ).fetchone()
    sleep = conn.execute("SELECT 1 FROM recovery_sleep WHERE day = ?", (day,)).fetchone()
    return bool(hrv and sleep)


def run_sync(conn: sqlite3.Connection, payload: dict) -> None:
    """Fetch each kind from where it left off (minus a trailing window) through today.

    Progress is saved after every call, so a rate-limit deferral resumes where it stopped.
    """
    settings = config.get_garmin_settings()
    if not settings.configured:
        log.info("GARMIN_API_KEY not set; skipping sync")
        return
    today = date.today()
    st = sync_state(conn)
    if not payload.get("force") and is_fresh(conn, st, today):
        return
    with garmin.make_http() as http:
        src = garmin.GarminSource(conn, settings, http)
        try:
            for kind, fetch, cap, trail in _kinds(src):
                start = today - timedelta(days=HISTORY_DAYS)
                if done := st.through.get(kind):
                    start = max(start, min(date.fromisoformat(done), today - timedelta(trail)))
                while start <= today:
                    end = min(start + timedelta(days=cap - 1), today)
                    storage.upsert(conn, fetch(start, end))
                    st.through[kind] = end.isoformat()
                    storage.kv_set(conn, STATE_KEY, st)
                    conn.commit()
                    start = end + timedelta(days=1)
        except garmin.KeyInvalid as e:
            log.warning("garmin sync stopped: %s", e)
            return
    st.last_full_sync = time.time()
    storage.kv_set(conn, STATE_KEY, st)
