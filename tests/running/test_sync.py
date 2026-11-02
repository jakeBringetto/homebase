import time
from datetime import date

import pytest

from app.core.jobs import Worker
from app.modules.running import Running, reports, storage, sync
from app.modules.running.config import AthleteSettings
from app.modules.running.sources import strava
from app.modules.running.sources.base import Activity
from tests.running.conftest import connect, summary


def statuses(conn):
    return dict(conn.execute("SELECT source_id, streams_status FROM running_activities").fetchall())


def first_id(conn) -> int:
    return conn.execute("SELECT id FROM running_activities WHERE source_id = '1'").fetchone()[0]


def test_sync_skips_when_not_connected(running_conn, fake):
    sync.run_sync(running_conn, {})
    assert fake.calls == []


def test_backfill_then_incremental(running_conn, fake, monkeypatch):
    monkeypatch.setattr(strava, "PER_PAGE", 2)
    old = int(time.time()) - 3 * 365 * 86400
    fake.activities += [
        summary(9, old),
        summary(10, old - 1, sport="Ride"),
        summary(11, old - 2, manual=True),
    ]
    connect(running_conn)

    sync.run_sync(running_conn, {})
    st = sync.sync_state(running_conn)
    assert st.backfill_done and st.last_sync
    s = statuses(running_conn)
    assert len(s) == 6
    assert set(s.values()) == {"none"}  # history: summaries only, no stream fetches
    assert fake.calls == ["/api/v3/athlete/activities"] * 4

    fake.calls.clear()
    fake.activities.insert(0, summary(20, int(time.time())))
    fake.activities.insert(0, summary(21, int(time.time()), manual=True))
    sync.run_sync(running_conn, {})
    assert fake.calls == ["/api/v3/athlete/activities"] * 2  # full page of 2, then empty
    s = statuses(running_conn)
    assert s["20"] == "queued" and s["21"] == "none"  # new GPS run only
    jobs = running_conn.execute("SELECT count(*) FROM jobs WHERE kind = 'running.streams'")
    assert jobs.fetchone()[0] == 1


def test_history_streams_queued_once_behind_new_runs(running_conn, fake):
    old = int(time.time()) - 400 * 86400
    fake.activities += [summary(9, old), summary(10, old - 1, manual=True)]
    connect(running_conn)
    sync.run_sync(running_conn, {})
    assert sync.queue_history_streams(running_conn) == 4  # 3 recent + 1 old GPS run
    assert sync.queue_history_streams(running_conn) == 0
    jobs = running_conn.execute(
        "SELECT DISTINCT priority FROM jobs WHERE kind = 'running.streams'"
    ).fetchall()
    assert [r[0] for r in jobs] == [-1]


def test_ride_logged_as_run_is_excluded(running_conn, fake):
    ride = summary(30, int(time.time()) - 3600)
    ride["moving_time"] = 1000  # 8 km in 1000 s: ~3:21/mi
    fake.activities.insert(0, ride)
    connect(running_conn)
    sync.run_sync(running_conn, {})
    row = running_conn.execute(
        "SELECT is_run, ride_like, streams_status FROM running_activities WHERE source_id = '30'"
    ).fetchone()
    assert tuple(row) == (0, 1, "none")


def test_relist_adds_gear_and_fetches_each_shoe_once(running_conn, fake):
    connect(running_conn)
    sync.run_sync(running_conn, {})
    st = sync.sync_state(running_conn)
    st.list_version = 0  # stored before gear_id existed
    storage.kv_set(running_conn, sync.STATE_KEY, st)
    for a, gear in zip(fake.activities, ["g1", "g1", "g2"], strict=True):
        a["gear_id"] = gear
    fake.gear["g1"] = {"id": "g1", "name": "Trainer", "retired": False}  # g2: 404
    fake.calls.clear()

    sync.run_sync(running_conn, {})
    assert fake.calls.count("/api/v3/athlete/activities") == 2  # new runs, then one re-page
    assert sorted(c for c in fake.calls if "/gear/" in c) == ["/api/v3/gear/g1", "/api/v3/gear/g2"]
    gear = dict(running_conn.execute("SELECT id, name FROM running_gear").fetchall())
    assert gear == {"g1": "Trainer", "g2": None}

    fake.calls.clear()
    sync.run_sync(running_conn, {})
    assert not [c for c in fake.calls if "/gear/" in c]
    shoes = {s["gear_id"]: s for s in reports.shoes(running_conn, date.today())}
    assert shoes["g1"]["runs"] == 2 and shoes["g1"]["distance_m"] == 16000
    assert shoes["g1"]["active"]


@pytest.mark.parametrize(
    ("miles", "pace_s_mi", "ride"),
    [
        (6.8, 281, True),  # 4:41/mi for 6.8 mi: a bike ride
        (2.4, 299, True),
        (1.0, 288, False),  # a fast mile is still a run
        (2.0, 324, False),  # track session
        (0.4, 230, False),  # under 800 m
        (1.0, 230, True),  # faster than 4:00/mi over 800 m
    ],
)
def test_ride_rules(miles, pace_s_mi, ride):
    m = miles * 1609.344
    a = Activity("strava", "1", "Run", "r", 0, "2026-01-01T07:00:00", round(miles * pace_s_mi),
                 0, m, 0, None, None, False)  # fmt: skip
    assert a.ride_like is ride and a.is_run is not ride


def test_streams_job_stores_and_analyzes_once(running_conn, fake):
    connect(running_conn)
    sync.run_sync(running_conn, {})
    aid = first_id(running_conn)
    sync.fetch_streams(running_conn, {"activity_id": aid})
    sync.fetch_streams(running_conn, {"activity_id": aid})
    assert sum(c.endswith("/streams") for c in fake.calls) == 1
    assert storage.load_streams(running_conn, aid)["time"].shape == (2400,)
    m = running_conn.execute("SELECT * FROM running_metrics WHERE activity_id = ?", (aid,))
    m = m.fetchone()
    assert m["trimp"] > 0 and m["steady_s"] > 0


def test_missing_streams_marked_unavailable(running_conn, fake):
    connect(running_conn)
    sync.run_sync(running_conn, {})
    aid = first_id(running_conn)
    fake.status_override["/api/v3/activities/1/streams"] = 404
    sync.fetch_streams(running_conn, {"activity_id": aid})
    assert statuses(running_conn)["1"] == "unavailable"


def test_recompute_uses_new_zones(running_conn, fake):
    connect(running_conn)
    sync.run_sync(running_conn, {})
    aid = first_id(running_conn)
    sync.fetch_streams(running_conn, {"activity_id": aid})
    athlete = AthleteSettings(max_hr=200, zone_bounds=[160, 170, 180, 190])
    storage.kv_set(running_conn, "athlete", athlete)
    calls = len(fake.calls)
    sync.recompute(running_conn, {})
    z1 = running_conn.execute("SELECT z1_s FROM running_metrics WHERE activity_id = ?", (aid,))
    z1 = z1.fetchone()[0]
    assert z1 > 2300  # all of a 150 bpm run is now Z1
    assert len(fake.calls) == calls


def test_full_pipeline_through_worker(running_conn, fake, settings):
    connect(running_conn)
    m = Running()
    w = Worker(settings.db_path, m.handlers(), m.recurring())
    w.start()
    while w.run_once():
        pass
    assert set(statuses(running_conn).values()) == {"none"}  # backfill: summaries only

    fake.activities.insert(0, summary(20, int(time.time())))
    running_conn.execute("UPDATE jobs SET run_after = 0 WHERE kind = 'running.sync'")
    running_conn.commit()
    while w.run_once():
        pass
    assert statuses(running_conn)["20"] == "stored"
    pending = running_conn.execute("SELECT kind FROM jobs WHERE status = 'pending'").fetchall()
    assert sorted(r[0] for r in pending) == ["running.garmin", "running.sync"]  # next daily runs
