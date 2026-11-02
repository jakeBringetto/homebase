from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import garmin as client
from app.core.charts import day_epoch
from app.core.ratelimit import RateLimiter, Window
from app.main import create_app
from app.modules.recovery import Recovery, sleep_timing, storage, sync
from app.modules.recovery.sleep_timing import Night
from app.modules.recovery.sources import garmin
from app.modules.recovery.sources.base import Sleep
from app.modules.running import Running
from tests.conftest import PASSWORD

MON = date(2026, 10, 5)  # a Monday
PDT = -7 * 3600
SLEEP_PATH = "/sleep-service/sleep/dailySleepSummariesByDate"


def night(day: date, bed_h: float, hours: float = 8, offset=PDT, score=80) -> Night:
    """A night going to bed `bed_h` local hours from the wake day's midnight (-1 = 23:00)."""
    start = int(day_epoch(day) + bed_h * 3600 - (offset or 0))
    return Night(day, start, start + int(hours * 3600), int(hours * 3600), score, offset)


def test_parse_sleep_offset_from_local_and_gmt_timestamps():
    base = {"calendarDate": "2026-10-28", "sleepTimeSeconds": 26000,
            "sleepStartTimestampGMT": 1_800_000_000_000}  # fmt: skip
    local = {**base, "sleepStartTimestampLocal": 1_800_000_000_000 - 7 * 3600 * 1000 + 999}
    assert garmin.parse_sleep(local).utc_offset_s == PDT  # rounded to a quarter hour
    assert garmin.parse_sleep(base).utc_offset_s is None
    absurd = {**base, "sleepStartTimestampLocal": 1_900_000_000_000}
    assert garmin.parse_sleep(absurd).utc_offset_s is None


def test_clock_times():
    n = night(MON, -1.5)
    assert sleep_timing.clock(n.bed_h) == "22:30" and sleep_timing.clock(n.wake_h) == "06:30"
    assert sleep_timing.clock(n.mid_h) == "02:30" and sleep_timing.clock(None) == "–"
    assert night(MON, -1.5, offset=None).bed_h is None


def test_pair_sri():
    a, tue = night(MON, -1), MON + timedelta(days=1)
    assert sleep_timing.pair_sri(a, night(tue, -1)) == 100
    # 1 h later both ends: 2 h of 24 in a different state
    assert sleep_timing.pair_sri(a, night(tue, 0)) == pytest.approx(100 - 200 / 12)
    # 12 h asleep, then 12 h later: every minute reversed
    assert sleep_timing.pair_sri(night(MON, -6, hours=12), night(tue, 6, hours=12)) == -100
    assert sleep_timing.shift_min(a, night(tue, 0.5)) == 90


def test_summary_weekdays_vs_weekends():
    # Two weeks: weekday mornings bed 23:00; Sat and Sun mornings bed 01:00, 2 h later.
    ns = [night(d, 1 if d.weekday() >= 5 else -1)
          for d in (MON + timedelta(days=i) for i in range(14))]  # fmt: skip
    s = sleep_timing.summary(ns)
    assert (s["nights"], s["pairs"]) == (14, 13)
    assert s["work"]["nights"] == 10 and s["free"]["nights"] == 4
    assert s["jetlag_min"] == pytest.approx(120)
    assert sleep_timing.clock(s["bed_h"]) == "23:00" and s["bed_sd_min"] > 0
    # 3 of the 13 pairs move 2 h: into both weekends and out of the first
    assert s["shift_min"] == pytest.approx(3 * 120 / 13)
    few = sleep_timing.summary(ns[:5])
    assert few["sri"] is None and few["bed_h"] is None and few["jetlag_min"] is None


def test_next_morning_buckets_by_shift(recovery_conn):
    ns = [night(MON, -1), night(MON + timedelta(days=1), -1, score=90),
          night(MON + timedelta(days=2), 1, score=60)]  # fmt: skip
    buckets = {b["key"]: b for b in sleep_timing.next_morning(recovery_conn, ns, MON)}
    assert buckets["steady"]["nights"] == 1 and buckets["steady"]["score"] == 90
    assert buckets["big"]["nights"] == 1 and buckets["big"]["score"] == 60
    assert buckets["some"]["nights"] == 0 and buckets["some"]["hrv"] is None


def test_sync_refetches_sleep_once_for_local_times(recovery_conn, garmin_fake, monkeypatch):
    monkeypatch.setattr(client, "LIMITER", RateLimiter("garmin-test", [Window(86400, 10_000)]))
    c, today = recovery_conn, date.today()
    sync.run_sync(c, {})
    q = "SELECT count(*) FROM recovery_sleep WHERE utc_offset_s = ?"
    assert c.execute(q, (PDT,)).fetchone()[0] == 366

    st = sync.sync_state(c)  # as stored before SLEEP_VERSION existed
    st.sleep_version = 0
    storage.kv_set(c, sync.STATE_KEY, st)
    garmin_fake.calls.clear()
    sync.run_sync(c, {})
    sleep_q = [q for p, q in garmin_fake.calls if p == SLEEP_PATH]
    assert len(sleep_q) == 12
    assert sleep_q[0]["startDate"] == (today - timedelta(days=365)).isoformat()
    other = [p for p, _ in garmin_fake.calls if p not in (SLEEP_PATH, client.TOKEN_PATH)]
    assert len(other) == 2  # daily and HRV only re-fetch their trailing days

    garmin_fake.calls.clear()
    sync.run_sync(c, {})
    assert garmin_fake.calls == []


def test_sleep_page_renders(recovery_conn, settings):
    rows = []
    for i in range(10):
        n = night(date.today() - timedelta(days=i), -1)
        rows.append(Sleep(n.day.isoformat(), n.start, n.end, n.sleep_s, *[None] * 8, 80, None, PDT))
    storage.upsert(recovery_conn, rows)
    recovery_conn.commit()
    with TestClient(create_app(settings, [Running(), Recovery()])) as tc:
        tc.post("/login", data={"password": PASSWORD})
        page = tc.get("/recovery/sleep?range=1m").text
        assert "Usual bedtime" in page and "23:00" in page and "Social jetlag" in page
        timing = tc.get("/recovery/charts/sleep-timing.json?range=1m").json()
        assert timing["data"][1][0] == -1 and timing["opts"]["axes"][1]["format"] == "clock"
        assert tc.get("/recovery/charts/sri.json?range=1m").json()["data"][1] == [100] * 9
