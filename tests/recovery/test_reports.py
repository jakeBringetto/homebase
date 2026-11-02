from datetime import date, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.recovery import Recovery, reports, storage
from app.modules.recovery.sources.base import Daily, Hrv, Sleep
from app.modules.running import Running
from tests.conftest import PASSWORD
from tests.running.test_plan import add_activity

TODAY = date(2026, 11, 1)


def hrv(day: date, value, low=None, high=None) -> Hrv:
    return Hrv(day.isoformat(), value, None, None, None, low, high, None)


def daily(day: date, rhr, stress=30, bb=(20, 80)) -> Daily:
    return Daily(day.isoformat(), rhr, stress, None, *bb, None, None, None)


def sleep(day: date) -> Sleep:
    return Sleep(day.isoformat(), None, None, 27000, *[None] * 8, 80, None)


def test_hrv_band_falls_back_to_our_60_day_range(recovery_conn):
    days = [TODAY - timedelta(days=i) for i in range(20, -1, -1)]
    rows = [hrv(d, 60 + 10 * (i % 2)) for i, d in enumerate(days[:-1])]
    rows.append(hrv(TODAY, 50, low=58, high=80))  # Garmin's baseline once it has one
    storage.upsert(recovery_conn, rows)
    ns = reports.nights(recovery_conn, days[16])  # 16 prior nights: eight 60s, eight 70s
    assert all(not n.garmin_band for n in ns[:-1])
    assert ns[0].low == pytest.approx(60) and ns[0].high == pytest.approx(70)  # mean 65 ± 5
    assert (ns[-1].low, ns[-1].high, ns[-1].garmin_band) == (58, 80, True)
    early = reports.nights(recovery_conn, days[0])
    assert early[5].low is None  # fewer than 14 prior nights


def test_resting_hr_skips_days_not_worn(recovery_conn):
    d1, d2, d3 = (TODAY - timedelta(days=i) for i in (2, 1, 0))
    storage.upsert(recovery_conn, [
        daily(d1, 50, stress=None, bb=(37, 37)),  # barely worn, no sleep: the fallback minimum
        daily(d2, 52, stress=None, bb=(37, 37)),  # same, but slept with the watch on
        daily(d3, 54),  # worn all day
    ])  # fmt: skip
    storage.upsert(recovery_conn, [sleep(d2)])
    rows = reports.resting_hr(recovery_conn, d1)
    assert [(d, v) for d, v, _ in rows] == [(d2, 52), (d3, 54)] and rows[-1][2] == 53


def test_next_morning_buckets_by_previous_day_load(recovery_conn):
    c = recovery_conn
    for i in (1, 3, 5, 7):  # TRIMP estimated from summary avg HR
        add_activity(c, i, f"{(TODAY - timedelta(days=i)).isoformat()}T07:00:00")
    c.execute("UPDATE running_activities SET avg_hr = 175 WHERE source_id = '7'")
    storage.upsert(c, [hrv(TODAY - timedelta(days=i), 50 + i) for i in range(7)])
    out = reports.next_morning(c, TODAY - timedelta(days=6), TODAY)
    kinds = {p["day"]: p["kind"] for p in out["points"]}
    assert kinds[TODAY - timedelta(days=6)] == "hard"  # the morning after the high-HR run
    assert kinds[TODAY] == "easy" and kinds[TODAY - timedelta(days=1)] == "rest"
    assert [b["nights"] for b in out["buckets"]] == [3, 3, 1]


def test_hrv_cv_of_log_values_over_7_nights(recovery_conn):
    vals = [60, 70, 80, 65, 75, 90, 55]
    days = [TODAY - timedelta(days=i) for i in range(9, -1, -1)]
    worn = [hrv(d, v) for d, v in zip(days[3:], vals, strict=True)]
    storage.upsert(recovery_conn, [hrv(d, None) for d in days[:3]] + worn)
    ns = {n.day: n for n in reports.nights(recovery_conn, days[0])}
    logs = np.log(vals)
    assert ns[TODAY].cv == pytest.approx(100 * logs.std(ddof=1) / logs.mean(), abs=0.01)
    assert ns[days[5]].cv is None  # only 3 nights with HRV so far
    assert ns[days[6]].cv is not None


def _run(c, i: int, day: date, pace_s_km: float, hr: float) -> None:
    add_activity(c, i, f"{day.isoformat()}T07:00:00")
    c.execute(
        "UPDATE running_activities SET moving_s = ?, avg_hr = ? WHERE source_id = ?",
        (8 * pace_s_km, hr, str(i)),
    )


def test_readiness_compares_runs_with_expected_hr(recovery_conn):
    c = recovery_conn
    for i in range(12):  # history: HR = 300 - 30 * speed (m/s), over two paces
        speed = 3.0 if i % 2 else 3.5
        _run(c, i, TODAY - timedelta(days=40 - i), 1000 / speed, 300 - 30 * speed)
    ok_day, low_day = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    _run(c, 100, ok_day, 1000 / 3.5, 300 - 105 - 2)  # 2 bpm easier than expected
    _run(c, 101, low_day, 1000 / 3.0, 300 - 90 + 6)  # 6 bpm harder (fit now includes run 100)
    storage.upsert(c, [hrv(low_day, 50, 60, 90), hrv(ok_day, 75, 60, 90)])
    storage.upsert(c, [sleep(low_day)])  # score 80
    out = reports.readiness(c, TODAY - timedelta(days=3))
    pts = {p["day"]: p for p in out["points"]}
    assert pts[ok_day]["delta"] == pytest.approx(-2, abs=0.01)
    assert pts[low_day]["delta"] == pytest.approx(6, abs=0.5)
    assert pts[low_day]["hrv_pct"] == pytest.approx(100 * (50 / 75 - 1))
    by_hrv = {b["label"]: b for b in out["by_hrv"]}
    assert by_hrv["Below normal range"]["runs"] == by_hrv["In range"]["runs"] == 1
    assert by_hrv["In range"]["delta"] == -2.0 and by_hrv["Above range"]["runs"] == 0
    assert [b["runs"] for b in out["by_sleep"]] == [0, 0, 1]
    # The history runs have no morning HRV or sleep, so only the two test runs count.
    assert len(reports.readiness(c, TODAY - timedelta(days=40))["points"]) == 2


def test_pages_render(recovery_conn, settings, garmin_fake):
    storage.upsert(recovery_conn, [hrv(date.today(), 70, 60, 90)])
    storage.upsert(recovery_conn, [daily(date.today(), 50)])
    storage.upsert(recovery_conn, [sleep(date.today())])
    recovery_conn.commit()
    with TestClient(create_app(settings, [Running(), Recovery()])) as client:
        client.post("/login", data={"password": PASSWORD})
        page = client.get("/recovery/?range=1m").text
        assert "70 ms" in page and "60–90 ms" in page
        assert "Key set." in client.get("/recovery/sync").text
        assert "Morning readiness" in page and "Night-to-night variation" in page
        for name in ["hrv", "hrv-cv", "rhr", "sleep", "response", "readiness"]:
            body = client.get(f"/recovery/charts/{name}.json?range=bogus").json()
            assert "data" in body, name
        # The worker's startup sync (fake Garmin, also 70 ms) may have filled in more nights.
        assert client.get("/recovery/charts/hrv.json").json()["data"][1][-1] == 70
        assert "HRV 70 ms (normal 60–90)" in client.get("/").text
