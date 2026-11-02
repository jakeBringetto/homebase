import time
from datetime import date, timedelta

import pytest

from app.core import garmin as client
from app.core import kv
from app.core.jobs import RetryLater
from app.core.ratelimit import RateLimiter, Window
from app.modules.recovery import sync
from app.modules.recovery.sources import garmin
from tests.recovery.conftest import KEY, make_jwt, set_key

TODAY = date.today()
OPEN = RateLimiter("garmin-test", [Window(86400, 10_000)])


@pytest.fixture(autouse=True)
def open_limiter(monkeypatch):
    monkeypatch.setattr(client, "LIMITER", OPEN)


def source(conn) -> garmin.GarminSource:
    return garmin.GarminSource(client.GarminClient(conn, client.get_settings(), client.make_http()))


def token_calls(fake) -> int:
    return [p for p, _ in fake.calls].count(client.TOKEN_PATH)


def test_parsers_handle_garmin_quirks():
    assert garmin.parse_sleep({"calendarDate": None, "sleepTimeSeconds": None}) is None
    s = garmin.parse_sleep({
        "calendarDate": "2026-10-28", "sleepTimeSeconds": 26280,
        "sleepStartTimestampGMT": 1761600000000, "sleepEndTimestampGMT": "2026-10-28T06:00:00",
        "sleepScores": {"overall": {"value": 83}},
    })  # fmt: skip
    assert s.start_utc == 1761600000 and s.end_utc == 1793167200 and s.score == 83
    d = garmin.parse_daily({
        "calendarDate": "2026-10-31", "heartRate": {"restingValue": 70},
        "stress": {"avgLevel": -1, "maxLevel": -1}, "bodyBattery": None,
    })  # fmt: skip
    assert d.resting_hr == 70 and d.stress_avg is None and d.stress_max is None and d.bb_min is None
    h = garmin.parse_hrv({"calendarDate": "2026-10-30", "lastNightAvg": None, "weeklyAvg": 71,
                          "baseline": {"balancedLow": 72, "balancedUpper": 107}})  # fmt: skip
    assert h.last_night_avg is None and (h.balanced_low, h.balanced_upper) == (72, 107)
    assert garmin.parse_hrv({"calendarDate": "not a date"}) is None


def test_jwt_exp():
    assert client.jwt_exp(make_jwt(1_800_000_000)) == 1_800_000_000
    assert client.jwt_exp("not-a-jwt") is None


def test_backfill_then_trailing_refresh(recovery_conn, garmin_fake):
    c = recovery_conn
    sync.run_sync(c, {})
    # 366 days: daily and HRV in 2 calls each (365-day cap), sleep in 12 (31-day cap).
    paths = garmin_fake.data_calls()
    assert len(paths) == 16 and paths.count("/sleep-service/sleep/dailySleepSummariesByDate") == 12
    assert token_calls(garmin_fake) == 1
    n = c.execute("SELECT count(*) FROM recovery_sleep").fetchone()[0]
    assert n == 366 and c.execute("SELECT count(*) FROM recovery_hrv").fetchone()[0] == 366
    st = sync.sync_state(c)
    assert st.through == {k: TODAY.isoformat() for k in ("daily", "hrv", "sleep")}

    garmin_fake.calls.clear()
    sync.run_sync(c, {})  # last night is stored and we synced just now: nothing to do
    assert garmin_fake.calls == []

    sync.run_sync(c, {"force": True})  # token cached; 1 call per kind for the trailing window
    assert len(garmin_fake.calls) == 3
    sleep_q = garmin_fake.calls[2][1]
    assert sleep_q["startDate"] == (TODAY - timedelta(days=7)).isoformat()


def test_backfill_resumes_after_rate_limit(recovery_conn, garmin_fake, monkeypatch):
    monkeypatch.setattr(client, "LIMITER", RateLimiter("garmin-tight", [Window(86400, 6)]))
    with pytest.raises(RetryLater):
        sync.run_sync(recovery_conn, {})
    done = len(garmin_fake.data_calls())  # token + 5 data calls fit the budget
    assert done == 5
    monkeypatch.setattr(client, "LIMITER", OPEN)
    sync.run_sync(recovery_conn, {})
    # No chunk fetched twice; the 2 extra calls are the finished kinds' trailing windows.
    assert len(garmin_fake.data_calls()) == 18


def test_401_reexchanges_once(recovery_conn, garmin_fake):
    garmin_fake.status_override["/usersummary-service/v2/dailysummaries"] = [401]
    assert len(source(recovery_conn).daily(TODAY, TODAY)) == 1
    assert token_calls(garmin_fake) == 2


def test_rejected_key_stops_until_key_changes(recovery_conn, garmin_fake, monkeypatch):
    garmin_fake.status_override[client.TOKEN_PATH] = [401]
    sync.run_sync(recovery_conn, {})  # logs and returns: no failed job, no retries
    auth = kv.get(recovery_conn, client.KV_TABLE, client.AUTH_KEY, client.GarminAuth)
    assert "rejected" in auth.invalid
    garmin_fake.calls.clear()
    sync.run_sync(recovery_conn, {"force": True})
    assert garmin_fake.calls == []
    set_key(monkeypatch, "GCDC_new")  # a new key gets a fresh auth state
    assert client.auth_state(recovery_conn, client.get_settings()).invalid is None


def test_429_defers(recovery_conn, garmin_fake):
    garmin_fake.status_override[client.TOKEN_PATH] = [429]
    with pytest.raises(RetryLater):
        sync.run_sync(recovery_conn, {})


def test_cached_token_skips_exchange(recovery_conn, garmin_fake):
    auth = client.GarminAuth(key_id=client.key_id(KEY), jwt="cached", exp=time.time() + 3600)
    kv.put(recovery_conn, client.KV_TABLE, client.AUTH_KEY, auth)
    source(recovery_conn).hrv(TODAY, TODAY)
    assert [p for p, _ in garmin_fake.calls] == [f"/hrv-service/hrv/daily/{TODAY}/{TODAY}"]
