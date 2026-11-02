from datetime import date, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.recovery import Recovery, lag, storage
from app.modules.recovery.sources.base import Sleep
from app.modules.running import Running
from tests.conftest import PASSWORD
from tests.recovery.test_reports import hrv
from tests.running.test_plan import add_activity

TODAY = date(2026, 11, 1)


def test_ranks_average_ties():
    assert lag._ranks(np.array([10.0, 20, 20, 5])).tolist() == [2, 3.5, 3.5, 1]


def test_spearman():
    xs = list(range(20))
    up = lag.spearman(xs, [x**3 for x in xs])  # monotonic, not linear
    assert up["rho"] == pytest.approx(1) and up["n"] == 20
    rng = np.random.default_rng(0)
    noise = lag.spearman(list(rng.normal(size=200)), list(rng.normal(size=200)))
    assert noise["lo"] < 0 < noise["hi"]  # independent: CI includes 0
    assert abs(noise["hi"] - noise["lo"]) < 0.35
    assert lag.spearman(xs[:9], xs[:9])["rho"] is None  # under MIN_N
    assert lag.spearman(xs, [1] * 20)["rho"] is None  # no spread


def test_hrv_vs_baseline(recovery_conn):
    days = [TODAY - timedelta(days=i) for i in range(8, -1, -1)]
    storage.upsert(recovery_conn, [hrv(d, 60) for d in days[:-1]] + [hrv(days[-1], 66)])
    out = lag.hrv_vs_baseline(recovery_conn, TODAY - timedelta(days=4))
    assert out[TODAY] == pytest.approx(10)  # 66 against a 60 ms baseline
    assert TODAY - timedelta(days=5) not in out  # before start


def test_points_line_up_load_with_later_mornings(recovery_conn):
    c = recovery_conn
    run_day = TODAY - timedelta(days=3)
    add_activity(c, 1, f"{run_day.isoformat()}T07:00:00")
    days = [TODAY - timedelta(days=i) for i in range(12, -1, -1)]
    storage.upsert(c, [hrv(d, 60) for d in days])
    sleep = [Sleep(d.isoformat(), None, None, 27000, 5400 if d == run_day + timedelta(days=1)
                   else 3600, *[None] * 7, 70, None) for d in days]  # fmt: skip
    storage.upsert(c, sleep)
    pts = lag.points(c, TODAY - timedelta(days=5), TODAY)
    # Load is only on run_day; deep sleep that night (waking the next day) was 90 min.
    deep = dict(pts["load_deep"])
    assert max(deep) > 0 and deep[max(deep)] == 90
    loaded = [x for x, _ in pts["load_hrv1"] if x > 0]
    assert len(loaded) == 1 and len(pts["load_hrv2"]) == len(pts["load_hrv1"]) - 1


def test_lag_page_renders(recovery_conn, settings):
    storage.upsert(recovery_conn, [hrv(date.today(), 60)])
    recovery_conn.commit()
    with TestClient(create_app(settings, [Running(), Recovery()])) as tc:
        tc.post("/login", data={"password": PASSWORD})
        page = tc.get("/recovery/lag").text
        assert "Spearman" in page and "Needs 10" in page and "Sleep score" in page
        assert tc.get("/recovery/charts/lag/load_hrv1.json?range=1m").json()["data"] == [[], []]
        assert tc.get("/recovery/charts/lag/bogus.json").status_code == 404
