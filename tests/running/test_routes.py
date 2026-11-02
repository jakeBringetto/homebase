from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.core.jobs import Worker
from app.main import create_app
from app.modules.running import Running, storage, sync
from app.modules.running.sources import strava
from tests.conftest import PASSWORD
from tests.running.conftest import connect


@pytest.fixture
def client(settings, fake):
    with TestClient(create_app(settings, [Running()])) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


@pytest.fixture
def synced(running_conn, settings, fake):
    """Connected account with three runs synced and analyzed."""
    connect(running_conn)
    m = Running()
    w = Worker(settings.db_path, m.handlers(), m.recurring())
    w.start()
    while w.run_once():
        pass
    for (aid,) in running_conn.execute("SELECT id FROM running_activities").fetchall():
        sync.fetch_streams(running_conn, {"activity_id": aid})
    running_conn.commit()
    return running_conn


def test_pages_render_empty(client):
    paths = ["/running/", "/running/load", "/running/performance", "/running/activities",
             "/running/sync", "/running/settings"]  # fmt: skip
    for path in paths:
        assert client.get(path).status_code == 200, path
    assert "Connect Strava" in client.get("/running/sync").text
    assert "Strava not connected" in client.get("/").text


def test_pages_render_with_data(synced, client):
    assert "8.0 km" in client.get("/running/").text
    assert "Run 1" in client.get("/running/activities").text
    assert "Connected" in client.get("/running/sync").text
    hr = client.get("/running/charts/hr-pace.json").json()
    assert hr["opts"]["series"][1]["label"] == "5:00–5:15/km"
    values = [v for v in hr["data"][1] if v is not None]
    assert len(hr["data"]) == 2 and values and values[-1] == pytest.approx(150, abs=1)
    dist = client.get("/running/charts/distance.json?range=3m").json()
    assert dist["opts"]["series"][1]["paths"] == "bars" and 13 <= len(dist["data"][0]) <= 15
    monthly = client.get("/running/charts/distance.json?range=all").json()
    assert len(monthly["data"][0]) <= 2  # one bucket per month
    fit = client.get("/running/charts/fitness.json").json()
    assert len(fit["data"]) == 4 and fit["data"][1][-1] > 0
    ef = client.get("/running/charts/efficiency.json?range=1m").json()
    assert ef["opts"]["series"][2] == {"label": "Each run", "paths": "points"}
    assert sum(v is not None for v in ef["data"][2]) == 3
    scatter = client.get("/running/charts/hr-pace-runs.json?range=all").json()
    assert scatter["opts"]["scales"] == {"x": {"time": False}} and scatter["data"][0] == [300] * 3
    intensity = client.get("/running/charts/intensity.json?range=1m").json()
    assert len(intensity["data"]) == 4
    assert client.get("/running/charts/spikes.json").status_code == 200


@pytest.mark.parametrize("key", ["all", "1y", "6m", "3m", "1m", "bogus"])
def test_pages_by_range(synced, client, key):
    page = client.get(f"/running/?range={key}").text
    table = {"all": "Years", "1y": "Months"}.get(key, "Weeks")
    assert f"<h2>{table}</h2>" in page
    perf = client.get(f"/running/performance?range={key}").text
    assert "Best efforts" in perf and "25:00" in perf  # 5 km at 5:00/km
    assert "5:00–5:15/km" in perf  # pace profile band
    assert "5 km now" in perf  # race estimate from the synced 5 km efforts
    load = client.get(f"/running/load?range={key}").text
    assert "Long-run spikes" in load and "Monotony and strain" in load
    for name in ["monotony", "race-estimate"]:
        assert "data" in client.get(f"/running/charts/{name}.json?range={key}").json()


def test_cadence_chart_period_average_and_runs(synced, client):
    ids = [r[0] for r in synced.execute("SELECT activity_id FROM running_metrics ORDER BY 1")]
    sql = "UPDATE running_metrics SET cadence_spm = ? WHERE activity_id = ?"
    for aid, spm in zip(ids, (160, 170, 180), strict=True):
        synced.execute(sql, (spm, aid))
    synced.commit()
    body = client.get("/running/charts/cadence.json?range=1m").json()
    assert sorted(v for v in body["data"][2] if v is not None) == [160, 170, 180]
    assert any(v is not None and 160 <= v <= 180 for v in body["data"][1])
    assert "<h2>Cadence</h2>" in client.get("/running/performance").text


def test_race_estimate_chart_formats_times(synced, client):
    body = client.get("/running/charts/race-estimate.json?range=1m").json()
    assert body["opts"]["axes"][1]["format"] == "pace"
    assert body["data"][1][-1] == 1500  # the synced runs' 5 km in 25:00


def test_settings_validation_and_recompute(synced, client):
    bad = {"max_hr": 190, "resting_hr": 50, "units": "km", "zone_bounds": "150, 140, 160, 170"}
    r = client.post("/running/settings", data=bad)
    assert r.status_code == 400 and "4 increasing values" in r.text
    r = client.post("/running/settings", data={"max_hr": 185, "resting_hr": 55, "units": "mi"})
    assert r.status_code == 200
    assert storage.athlete_settings(synced).units == "mi"
    recomputes = synced.execute("SELECT count(*) FROM jobs WHERE kind = 'running.recompute'")
    assert recomputes.fetchone()[0] == 2  # one at first sync, one for the HR change
    assert "/mi" in client.get("/running/activities").text


def test_oauth_flow(client, running_conn):
    r = client.get("/running/strava/connect", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == [strava.SCOPE]
    assert q["redirect_uri"][0].endswith("/running/strava/callback")

    bad = client.get("/running/strava/callback?code=c&state=wrong&scope=read,activity:read_all")
    assert "state mismatch" in bad.text

    def state():
        r = client.get("/running/strava/connect", follow_redirects=False)
        return parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    r = client.get(f"/running/strava/callback?code=c&state={state()}&scope=read")
    assert "Allow activity access" in r.text

    state = state()
    r = client.get(f"/running/strava/callback?code=c&state={state}&scope=read,activity:read_all")
    assert r.status_code == 200 and "Connected (athlete 42)" in r.text
    assert storage.kv_get(running_conn, strava.TOKEN_KEY, strava.StravaToken).athlete_id == 42

    client.post("/running/strava/disconnect")
    assert storage.kv_get(running_conn, strava.TOKEN_KEY, strava.StravaToken) is None
