from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.running import Running, plan, storage
from app.modules.running.config import AthleteSettings
from app.modules.running.sources.base import Activity
from tests.conftest import PASSWORD

MON = date(2026, 10, 26)


def add_activity(conn, i: int, start_local: str, sport: str = "Run", meters: float = 8000) -> None:
    moving = round(meters * 0.3)  # 5:00/km
    a = Activity("strava", str(i), sport, f"Act {i}", 0, start_local, moving, moving + 100, meters,
                 0, 140.0, 160.0, False)  # fmt: skip
    storage.upsert_activity(conn, a, fetch_streams=False)


def add(conn, day: date, kind: str, km: float | None = None) -> None:
    plan.save(conn, plan.Session(day=day, kind=kind, distance_m=km and km * 1000))


def statuses(wk: dict) -> list[tuple]:
    return [
        (d["day"].weekday(), s["kind"] if s else None, a["name"] if a else None, st)
        for d in wk["days"]
        for s, a, st in d["items"]
    ]


def test_week_matches_same_day_and_totals(running_conn):
    c = running_conn
    add(c, MON, "easy", 8)
    add(c, MON, "intervals", 5)  # second session on Monday: no second run, so missed
    add(c, MON + timedelta(days=1), "rest")
    add(c, MON + timedelta(days=2), "cross")
    add(c, MON + timedelta(days=3), "long", 20)  # today, not run yet
    add(c, MON + timedelta(days=5), "tempo", 10)  # future
    add_activity(c, 1, "2026-10-26T07:00:00")
    add_activity(c, 2, "2026-10-27T07:00:00")  # ran on a rest day
    add_activity(c, 3, "2026-10-28T18:00:00", sport="Ride", meters=30000)
    add_activity(c, 4, "2026-10-28T07:00:00")  # unplanned run on the cross day
    add_activity(c, 5, "2026-11-02T07:00:00")  # next week

    wk = plan.week(c, MON, today=MON + timedelta(days=3))
    assert statuses(wk) == [
        (0, "easy", "Act 1", "done"),
        (0, "intervals", None, "missed"),
        (1, "rest", None, "rest"),
        (1, None, "Act 2", "unplanned"),
        (2, "cross", "Act 3", "done"),
        (2, None, "Act 4", "unplanned"),
        (3, "long", None, "today"),
        (5, "tempo", None, "planned"),
    ]
    assert (wk["sessions"], wk["planned"], wk["done"]) == (6, 5, 2)
    assert wk["planned_m"] == 43_000 and wk["run_m"] == 24_000  # rides excluded


def test_copy_week_shifts_days(running_conn):
    add(running_conn, MON, "easy", 8)
    add(running_conn, MON + timedelta(days=6), "long", 18)
    assert plan.copy_week(running_conn, MON, MON + timedelta(weeks=1)) == 2
    days = [r[0] for r in running_conn.execute("SELECT day FROM running_plan ORDER BY day")]
    assert days == ["2026-10-26", "2026-11-01", "2026-11-02", "2026-11-08"]


@pytest.mark.parametrize(
    "bad", [{"kind": "swim"}, {"distance_m": -1}, {"duration_s": 0}, {"notes": "x" * 201}]
)
def test_session_validation(bad):
    with pytest.raises(ValueError):
        plan.Session(**{"day": MON, "kind": "easy", **bad})


def test_phase_for_week_and_targets(running_conn):
    c = running_conn
    plan.save_phase(c, plan.Phase(name="Base", start_day=MON, end_day=MON + timedelta(days=20)))
    plan.save_phase(c, plan.Phase(name="Build", start_day="2026-11-13", end_day="2026-12-13"))
    assert [(p["name"], p["weeks"]) for p in plan.phases(c)] == [("Base", 3), ("Build", 5)]
    p = plan.phase_for(c, MON + timedelta(weeks=2))
    assert (p["name"], p["week_no"]) == ("Base", 3)  # overlaps Build's first week; first wins
    assert plan.phase_for(c, MON + timedelta(weeks=3))["week_no"] == 2
    assert plan.phase_for(c, MON - timedelta(weeks=1)) is None

    plan.set_target(c, plan.Target(week=MON + timedelta(days=3), distance_m=30_000, quality=2))
    t = plan.get_target(c, MON)  # stored under the Monday
    assert (t["distance_m"], t["long_m"], t["quality"]) == (30_000, None, 2)
    plan.set_target(c, plan.Target(week=MON, long_m=10_000))
    assert plan.get_target(c, MON)["distance_m"] is None  # replaced, not merged
    plan.set_target(c, plan.Target(week=MON))
    assert plan.get_target(c, MON) is None


def test_week_long_run_and_quality_done(running_conn):
    c = running_conn
    add(c, MON, "intervals", 8)
    add(c, MON + timedelta(days=1), "tempo", 8)  # not run: doesn't count
    add_activity(c, 1, "2026-10-26T07:00:00")
    add_activity(c, 2, "2026-10-28T07:00:00", meters=14_000)  # unplanned
    wk = plan.week(c, MON, today=MON + timedelta(days=6))
    assert (wk["long_m"], wk["quality_done"]) == (14_000, 1)


@pytest.mark.parametrize(
    "bad", [{"name": ""}, {"end_day": date(2026, 10, 25)}, {"notes": "x" * 301}]
)
def test_phase_validation(bad):
    with pytest.raises(ValueError):
        plan.Phase(**{"name": "Base", "start_day": MON, "end_day": MON, **bad})


@pytest.fixture
def client(settings, running_conn):
    with TestClient(create_app(settings, [Running()])) as c:
        c.post("/login", data={"password": PASSWORD})
        yield c


def test_plan_pages(client, running_conn):
    storage.kv_set(running_conn, "athlete", AthleteSettings(units="mi"))
    running_conn.commit()
    assert client.get("/running/plan").status_code == 200
    assert client.get("/running/plan?week=garbage").status_code == 200

    form = {"week": "2026-10-28", "day": "2026-10-29", "kind": "long", "distance": "10"}
    r = client.post("/running/plan", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/running/plan?week=2026-10-26"
    row = running_conn.execute("SELECT * FROM running_plan").fetchone()
    assert row["distance_m"] == pytest.approx(16093.44)

    page = client.get(f"/running/plan?edit={row['id']}").text
    assert 'value="10.0"' in page and "Edit session" in page
    assert "No runs in 30 days" in page and ">over " not in page

    add_activity(running_conn, 1, "2026-10-27T07:00:00", meters=8 * 1609.344)
    running_conn.commit()
    page = client.get("/running/plan?week=2026-10-26").text
    assert ">over 8.8 mi<" in page  # 10 mi planned on the 29th; 8 × 1.1 floors to 8.8, not 8.7
    edit = {**form, "session_id": str(row["id"]), "minutes": "95", "distance": ""}
    client.post("/running/plan", data=edit)
    row = running_conn.execute("SELECT * FROM running_plan").fetchone()
    assert row["distance_m"] is None and row["duration_s"] == 5700

    r = client.post("/running/plan", data={**form, "distance": "-3"})
    assert r.status_code == 400 and "greater than 0" in r.text

    for _ in range(2):  # second copy is a no-op: target week no longer empty
        client.post("/running/plan/copy", data={"week": "2026-11-02"})
    assert running_conn.execute("SELECT count(*) FROM running_plan").fetchone()[0] == 2

    client.post(f"/running/plan/{row['id']}/delete", data={"week": "2026-10-26"})
    assert running_conn.execute("SELECT count(*) FROM running_plan").fetchone()[0] == 1


def test_target_and_phase_pages(client, running_conn):
    week = {"week": "2026-10-28"}
    r = client.post("/running/plan/target", data={**week, "distance": "30", "long": "12",
                                                  "quality": "2", "notes": "light week"},
                    follow_redirects=False)  # fmt: skip
    assert r.headers["location"] == "/running/plan?week=2026-10-26"
    assert plan.get_target(running_conn, MON)["long_m"] == 12_000
    r = client.post("/running/plan/target", data={**week, "quality": "-1"})
    assert r.status_code == 400 and 'value="-1"' in r.text

    phase = {**week, "name": "Base", "start_day": "2026-10-26", "end_day": "2026-11-15"}
    client.post("/running/plan/phase", data=phase)
    page = client.get("/running/plan?week=2026-11-09").text
    assert "<b>Base</b> · week 3 of 3" in page
    page = client.get("/running/plan?week=2026-10-26").text
    assert "This week: light week" in page and "0.0 km / 12.0 km" in page
    pid = plan.phases(running_conn)[0]["id"]
    assert 'value="2026-11-15"' in client.get(f"/running/plan?edit_phase={pid}").text

    r = client.post("/running/plan/phase", data={**phase, "end_day": "2026-10-01"})
    assert r.status_code == 400 and "on or after start" in r.text
    client.post("/running/plan/phase", data={**phase, "phase_id": str(pid), "name": "Rebuild"})
    assert [p["name"] for p in plan.phases(running_conn)] == ["Rebuild"]
    client.post(f"/running/plan/phase/{pid}/delete", data=week)
    assert plan.phases(running_conn) == []
