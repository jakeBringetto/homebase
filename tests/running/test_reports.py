from datetime import date

import numpy as np
import pytest

from app.core.charts import merge_points
from app.modules.running import reports, storage
from tests.running.test_plan import add_activity


def test_spikes_compare_to_prior_30_days(running_conn):
    c = running_conn
    add_activity(c, 1, "2026-09-01T07:00:00", meters=10000)
    add_activity(c, 2, "2026-09-10T07:00:00", meters=10500)  # +5%: fine
    add_activity(c, 3, "2026-09-20T07:00:00", meters=13000)  # +24% on 10.5 km
    add_activity(c, 4, "2026-10-25T07:00:00", meters=8000)  # 35 days after: nothing to compare
    add_activity(c, 5, "2026-10-26T07:00:00", meters=12000)  # +50% on 8 km
    add_activity(c, 6, "2026-10-26T18:00:00", sport="Ride", meters=40000)  # not a run
    rows = {r["source_id"]: r for r in reports.run_spikes(c, date(2026, 1, 1))}
    assert rows["1"]["prior_max"] is None and rows["4"]["prior_max"] is None
    assert rows["3"]["pct"] == pytest.approx(100 * (13000 / 10500 - 1))
    spikes = reports.spike_pcts(c, date(2026, 1, 1))
    ids = {r["id"]: r["source_id"] for r in rows.values()}
    assert sorted(ids[i] for i in spikes) == ["3", "5"]


def test_weekly_strain_complete_weeks_only(running_conn):
    c = running_conn
    monday = date(2026, 10, 19)
    assert monday.weekday() == 0
    for i, day in enumerate((19, 21, 23, 28)):  # Mon, Wed, Fri, then next Wed
        add_activity(c, i, f"2026-10-{day}T07:00:00")
    weeks, last7 = reports.weekly_strain(
        c, storage.athlete_settings(c), date(2026, 10, 1), date(2026, 10, 28)
    )
    assert [w["start"] for w in weeks] == [monday]  # the week of the 26th isn't over
    x = weeks[0]["load"] / 3
    week = np.array([x, 0, x, 0, x, 0, 0])
    assert weeks[0]["monotony"] == pytest.approx(week.mean() / week.std())
    assert weeks[0]["strain"] == pytest.approx(weeks[0]["load"] * weeks[0]["monotony"])
    assert last7 == pytest.approx(np.mean([x, 0, 0, 0, 0, 0, x]) / np.std([x, 0, 0, 0, 0, 0, x]))


def test_race_estimate_uses_best_recent_effort(running_conn):
    c = running_conn
    add_activity(c, 1, "2026-06-01T07:00:00")
    add_activity(c, 2, "2026-10-01T07:00:00")
    ids = dict(c.execute("SELECT source_id, id FROM running_activities").fetchall())
    c.executemany(
        "INSERT INTO running_best (activity_id, distance_m, seconds) VALUES (?, ?, ?)",
        [(ids["1"], 1609.344, 360), (ids["2"], 5000.0, 1500), (ids["2"], 800.0, 120)],
    )
    mile_5k = reports.riegel(360, 1609.344, 5000)
    assert mile_5k == pytest.approx(360 * (5000 / 1609.344) ** 1.06) and mile_5k < 1500
    now = reports.race_predictions(c, date(2026, 10, 15))
    assert now["source_label"] == "5 km"  # the June mile is over 90 days old; 800 m isn't used
    assert now["times"][0][2] == pytest.approx(1500)
    assert now["times"][1][2] == pytest.approx(1500 * 2**1.06)
    keys, secs = reports.race_estimates(c, date(2026, 6, 1), "month", date(2026, 10, 15))
    by_month = dict(zip([k.month for k in keys], secs, strict=True))
    assert by_month[6] == round(mile_5k) and by_month[9] is None and by_month[10] == 1500
    assert reports.race_predictions(c, date(2027, 6, 1)) is None


def test_intensity_shares():
    assert reports.intensity([600, 1800, 300, 200, 100]) == (80.0, 10.0, 10.0)
    assert reports.intensity([0, 0, 0, 0, 0]) is None


def test_calendar_levels_and_bounds(running_conn):
    c = running_conn
    for i, (day, m) in enumerate([("01", 2000), ("02", 5000), ("04", 8000), ("05", 16000)]):
        add_activity(c, i, f"2026-10-{day}T07:00:00", meters=m)
    years, cuts = reports.calendar(c, date(2026, 10, 1), date(2026, 10, 7))
    assert [y["year"] for y in years] == [2026] and len(cuts) == 3
    cells = [cell for week in years[0]["weeks"] for cell in week]
    assert cells[0] is None  # Mon Sep 28 is before the range
    by_day = {cell["day"].day: cell["level"] for cell in cells if cell}
    assert by_day == {1: 1, 2: 2, 3: 0, 4: 3, 5: 4, 6: 0, 7: 0}


def test_merge_points_sorts_and_pads():
    xs, (a, b) = merge_points([[(3, 30), (1, 10)], [(2, None), (1, 5)]])
    assert xs == [1, 1, 2, 3]
    assert a == [10, None, None, 30] and b == [None, 5, None, None]
