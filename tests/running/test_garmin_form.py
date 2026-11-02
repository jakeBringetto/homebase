from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import garmin as client
from app.core.ratelimit import RateLimiter, Window
from app.main import create_app
from app.modules.running import Running, reports, storage, sync
from app.modules.running.sources import garmin
from app.modules.running.sources.base import Activity
from tests.conftest import PASSWORD
from tests.recovery.conftest import KEY, FakeGarmin, set_key

T0 = 1_790_000_000  # 2026-09-21 UTC


@pytest.fixture
def fake(monkeypatch):
    f = FakeGarmin()
    monkeypatch.setattr(client, "make_http", lambda: httpx.Client(transport=httpx.MockTransport(f)))
    monkeypatch.setattr(client, "LIMITER", RateLimiter("garmin-test", [Window(86400, 10_000)]))
    set_key(monkeypatch, KEY)
    return f


def garmin_run(i: int, start: int, meters: float = 8000, gct: float = 250, kind="running"):
    return {
        "activityId": i, "beginTimestamp": start * 1000, "distance": meters,
        "activityType": {"typeKey": kind}, "avgPower": 400.0, "avgGroundContactTime": gct,
        "avgVerticalOscillation": 9.0, "avgVerticalRatio": 7.5, "avgStrideLength": 120.0,
        "averageRunningCadenceInStepsPerMinute": 165.0,
    }  # fmt: skip


def strava_run(conn, i: int, start: int, meters: float = 8000, moving: int = 2400) -> None:
    local = date.fromtimestamp(start).isoformat() + "T07:00:00"
    a = Activity("strava", str(i), "Run", f"Run {i}", start, local, moving, moving, meters,
                 0, 150.0, 170.0, False)  # fmt: skip
    storage.upsert_activity(conn, a, fetch_streams=False)


def test_parse_run_skips_non_runs_and_bad_rows():
    assert garmin.parse_run(garmin_run(1, T0, kind="cycling")) is None
    assert garmin.parse_run({"activityId": 2}) is None
    r = garmin.parse_run(garmin_run(3, T0, kind="trail_running"))
    assert (r.garmin_id, r.start_time, r.gct_ms) == ("3", T0, 250.0)


def test_fetch_pages_then_trails_and_matches_runs(running_conn, fake, monkeypatch):
    c = running_conn
    monkeypatch.setattr(garmin, "PAGE", 2)
    strava_run(c, 1, T0, moving=2400)
    strava_run(c, 2, T0 + 86400, moving=2000)
    strava_run(c, 3, T0 + 2 * 86400)  # no Garmin row: recorded on another device
    fake.activities = [
        garmin_run(12, T0 + 86400 + 30, gct=270),  # 30 s apart still matches
        garmin_run(11, T0, gct=240),
        garmin_run(13, T0 + 5 * 86400, meters=5000),  # no Strava run
    ]
    sync.run_garmin(c, {})
    searches = [q for p, q in fake.calls if p == garmin.SEARCH_PATH]
    assert [q["start"] for q in searches] == ["0", "2"]
    assert searches[0]["startDate"] == (date.today() - timedelta(days=365)).isoformat()
    assert sync.garmin_state(c).through == date.today().isoformat()

    runs = reports.form_runs(c)
    assert [(r["start_local"][:10], r["gct_ms"]) for r in runs] == [
        (date.fromtimestamp(T0).isoformat(), 240),
        (date.fromtimestamp(T0 + 86400).isoformat(), 270),
    ]
    (period,) = reports.form_periods(c, date(2026, 1, 1), "year")
    assert period["runs"] == 2
    assert period["gct_ms"] == pytest.approx((240 * 2400 + 270 * 2000) / 4400)  # by moving time

    fake.calls.clear()
    sync.run_garmin(c, {})  # later runs re-fetch only the trailing days
    (q,) = [q for p, q in fake.calls if p == garmin.SEARCH_PATH][:1]
    assert q["startDate"] == (date.today() - timedelta(days=sync.GARMIN_TRAIL_DAYS)).isoformat()


def test_rejected_key_returns_quietly(running_conn, fake):
    fake.status_override[client.TOKEN_PATH] = [401]
    sync.run_garmin(running_conn, {})
    assert sync.garmin_state(running_conn).through is None


def test_no_key_makes_no_calls(running_conn, fake, monkeypatch):
    set_key(monkeypatch, "")
    sync.run_garmin(running_conn, {})
    assert fake.calls == []


def test_form_page_and_charts(settings, running_conn, fake):
    strava_run(running_conn, 1, T0)
    fake.activities = [garmin_run(11, T0)]
    sync.run_garmin(running_conn, {})
    running_conn.commit()
    with TestClient(create_app(settings, [Running()])) as tc:
        tc.post("/login", data={"password": PASSWORD})
        page = tc.get("/running/form?range=all").text
        assert "1 of 1 Garmin runs matched" in page and "Ground contact time" in page
        body = tc.get("/running/charts/form/gct_ms.json?range=all").json()
        assert body["data"][0] == [300] and body["data"][1] == [250]  # 5:00/km
        assert tc.get("/running/charts/form/power.json").status_code == 404
