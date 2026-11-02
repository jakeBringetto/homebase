import time

import httpx
import pytest

from app.core.jobs import RetryLater
from app.modules.running import config, storage
from app.modules.running.sources import strava
from tests.running.conftest import connect

SETTINGS = config.StravaSettings(_env_file=None, client_id="1", client_secret="s")


def source(conn, fake):
    return strava.StravaSource(conn, SETTINGS, httpx.Client(transport=httpx.MockTransport(fake)))


def test_parse_usage_takes_tighter_window():
    h = httpx.Headers({
        "X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "10,100",
        "X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": "5,990",
    })  # fmt: skip
    assert strava.parse_usage(h) == {900: (100, 5), 86400: (1000, 990)}
    bad = httpx.Headers({"X-RateLimit-Limit": "x", "X-RateLimit-Usage": "1"})
    assert strava.parse_usage(bad) == {}


def test_parse_streams_drops_bad_channels():
    s = strava.parse_streams({
        "time": {"data": [0, 1, 2]}, "heartrate": {"data": [100, 101, 102]},
        "cadence": {"data": [80, 81]}, "latlng": {"data": [[1, 2], [1, 2], [1, 2]]},
    })  # fmt: skip
    assert set(s) == {"time", "heartrate"}
    assert strava.parse_streams({"heartrate": {"data": [1, 2]}}) is None


def test_activities_parsed_and_usage_observed(running_conn, fake):
    connect(running_conn)
    acts = source(running_conn, fake).activities()
    assert [a.source_id for a in acts] == ["1", "2", "3"]
    assert acts[0].is_run and acts[0].start_local.count(":") == 2
    used = {r["window_s"]: r["used"] for r in strava.LIMITER.status(running_conn)}
    assert used == {900: 1, 86400: 10}


def test_uncounted_streams_dont_use_budget(running_conn, fake):
    connect(running_conn)
    src = source(running_conn, fake)
    src.activities()
    for _ in range(3):
        assert src.streams("1") is not None
    used = {r["window_s"]: r["used"] for r in strava.LIMITER.status(running_conn)}
    assert used == {900: 1, 86400: 10}


def test_expired_token_refreshes(running_conn, fake):
    storage.kv_set(running_conn, strava.TOKEN_KEY, strava.StravaToken(
        access_token="old", refresh_token="r", expires_at=int(time.time()) - 10, athlete_id=7,
    ))  # fmt: skip
    source(running_conn, fake).activities()
    assert fake.calls[0] == "/oauth/token"
    token = storage.kv_get(running_conn, strava.TOKEN_KEY, strava.StravaToken)
    assert (token.access_token, token.athlete_id) == ("acc", 7)


def test_401_refreshes_once_then_retries(running_conn, fake):
    connect(running_conn)
    fake.status_override["/api/v3/athlete/activities"] = 401
    assert len(source(running_conn, fake).activities()) == 3
    list_path = "/api/v3/athlete/activities"
    assert fake.calls == [list_path, "/oauth/token", list_path]


def test_429_defers(running_conn, fake):
    connect(running_conn)
    fake.status_override["/api/v3/athlete/activities"] = 429
    with pytest.raises(RetryLater) as e:
        source(running_conn, fake).activities()
    assert 0 < e.value.delay_s <= 905


def test_not_connected(running_conn, fake):
    with pytest.raises(strava.NotConnected):
        source(running_conn, fake).activities()
