"""Job handlers: activity sync (new + backfill), per-run streams, metric recompute, Garmin form."""

import logging
import sqlite3
import time
from datetime import date, timedelta

from pydantic import BaseModel

from app.core import garmin as core_garmin
from app.core.jobs import RetryLater, enqueue
from app.modules.running import config, storage
from app.modules.running.analysis.metrics import ANALYSIS_VERSION, analyze
from app.modules.running.sources import garmin, strava
from app.modules.running.sources.base import Activity

log = logging.getLogger(__name__)

STATE_KEY = "sync_state"
# Bump when stored summaries need a field they lack; the next sync re-pages the whole
# activity list once (a few calls). 1: gear_id.
LIST_VERSION = 1


class SyncState(BaseModel):
    newest_start: int | None = None
    oldest_start: int | None = None
    backfill_done: bool = False
    last_sync: float | None = None
    analysis_version: int = 0
    list_version: int = 0


def sync_state(conn: sqlite3.Connection) -> SyncState:
    return storage.kv_get(conn, STATE_KEY, SyncState) or SyncState()


def _source(conn: sqlite3.Connection, http) -> strava.StravaSource | None:
    settings = config.get_strava_settings()
    token = storage.kv_get(conn, strava.TOKEN_KEY, strava.StravaToken)
    if not settings.configured or token is None:
        return None
    return strava.StravaSource(conn, settings, http)


def _store(
    conn: sqlite3.Connection, acts: list[Activity], st: SyncState, fetch_streams: bool
) -> None:
    for a in acts:
        storage.upsert_activity(conn, a, fetch_streams)
        st.newest_start = max(st.newest_start or a.start_time, a.start_time)
        st.oldest_start = min(st.oldest_start or a.start_time, a.start_time)
    storage.kv_set(conn, STATE_KEY, st)


def run_sync(conn: sqlite3.Connection, payload: dict) -> None:
    """Fetch activities newer than the newest stored, continue the history backfill, then
    fetch details once for any new gear (1 call each).

    Only new activities get their streams fetched; backfilled history is summaries only,
    which keeps the one-time cost to a few list calls. Progress is saved per page, so a
    rate-limit deferral resumes where it stopped.
    """
    st = sync_state(conn)
    if st.analysis_version != ANALYSIS_VERSION:
        enqueue(conn, "running.recompute", dedupe_key="running.recompute")
        st.analysis_version = ANALYSIS_VERSION
        storage.kv_set(conn, STATE_KEY, st)
        conn.commit()
    if st.list_version != LIST_VERSION:
        st.backfill_done, st.oldest_start, st.list_version = False, None, LIST_VERSION
        storage.kv_set(conn, STATE_KEY, st)
        conn.commit()
    with strava.make_http() as http:
        src = _source(conn, http)
        if src is None:
            log.info("strava not connected; skipping sync")
            return
        if st.newest_start is not None:
            after, page = st.newest_start, 1
            while len(acts := src.activities(after=after, page=page)) == strava.PER_PAGE:
                _store(conn, acts, st, fetch_streams=True)
                page += 1
            _store(conn, acts, st, fetch_streams=True)
        while not st.backfill_done:
            acts = src.activities(before=st.oldest_start)
            st.backfill_done = len(acts) < strava.PER_PAGE
            _store(conn, acts, st, fetch_streams=False)
        for gear_id in storage.unknown_gear(conn):
            storage.save_gear(conn, gear_id, src.gear(gear_id))
        st.last_sync = time.time()
        storage.kv_set(conn, STATE_KEY, st)


GARMIN_KEY = "garmin_state"
GARMIN_HISTORY_DAYS = 365  # Garmin returned a year in the spike
GARMIN_TRAIL_DAYS = 3  # re-fetch recent days: the watch can sync a run late


class GarminState(BaseModel):
    through: str | None = None  # last local day fetched (YYYY-MM-DD)
    synced_at: float | None = None


def garmin_state(conn: sqlite3.Connection) -> GarminState:
    return storage.kv_get(conn, GARMIN_KEY, GarminState) or GarminState()


def run_garmin(conn: sqlite3.Connection, payload: dict) -> None:
    """Fetch Garmin form metrics for runs since the last fetch (a year the first time)."""
    settings = core_garmin.get_settings()
    if not settings.configured:
        return
    today = date.today()
    st = garmin_state(conn)
    start = today - timedelta(days=GARMIN_HISTORY_DAYS)
    if st.through:
        start = max(start, date.fromisoformat(st.through) - timedelta(days=GARMIN_TRAIL_DAYS))
    with core_garmin.make_http() as http:
        try:
            runs = garmin.fetch_runs(core_garmin.GarminClient(conn, settings, http), start, today)
        except core_garmin.KeyInvalid as e:
            log.warning("garmin form sync stopped: %s", e)
            return
    storage.upsert_garmin(conn, runs)
    st.through, st.synced_at = today.isoformat(), time.time()
    storage.kv_set(conn, GARMIN_KEY, st)


def history_without_streams(conn: sqlite3.Connection) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM running_activities"
            " WHERE is_run AND NOT is_manual AND streams_status = 'none' ORDER BY start_local DESC"
        )
    ]


def queue_history_streams(conn: sqlite3.Connection) -> int:
    """Queue streams for runs synced as summaries only, behind new-run jobs (caller commits).

    One-time, user-triggered: Strava doesn't count streams requests against its limits.
    """
    ids = history_without_streams(conn)
    for aid in ids:
        storage.queue_streams(conn, aid, priority=-1)
    return len(ids)


def fetch_streams(conn: sqlite3.Connection, payload: dict) -> None:
    """Fetch, store, and analyze one run's streams (no-op if already stored)."""
    aid = int(payload["activity_id"])
    row = conn.execute(
        "SELECT source_id, streams_status FROM running_activities WHERE id = ?", (aid,)
    ).fetchone()
    if row is None or row["streams_status"] == "stored":
        return
    with strava.make_http() as http:
        src = _source(conn, http)
        if src is None:
            raise RetryLater(3600, "strava not connected")
        streams = src.streams(row["source_id"])
    if streams is None:
        storage.set_streams_status(conn, aid, "unavailable")
        return
    storage.save_streams(conn, aid, streams)
    storage.save_analysis(conn, aid, analyze(streams, storage.athlete_settings(conn)))


def recompute(conn: sqlite3.Connection, payload: dict) -> None:
    """Re-run analysis for every stored stream (no API calls), e.g. after zones change."""
    athlete = storage.athlete_settings(conn)
    ids = [r[0] for r in conn.execute("SELECT activity_id FROM running_streams")]
    for i, aid in enumerate(ids, 1):
        if streams := storage.load_streams(conn, aid):
            storage.save_analysis(conn, aid, analyze(streams, athlete))
        if i % 50 == 0:
            conn.commit()
    log.info("recomputed metrics for %d activities", len(ids))
