import numpy as np
import pytest

from app.modules.running.analysis.load import fitness_curves
from app.modules.running.analysis.metrics import analyze
from app.modules.running.config import AthleteSettings
from tests.running.conftest import make_streams

ATHLETE = AthleteSettings(max_hr=200, resting_hr=50)  # zones at 120/140/160/180


def test_steady_run_bins_at_its_pace():
    s = make_streams(minutes=40)
    s["cadence"] = np.full_like(s["time"], 85)  # Strava reports one foot
    a = analyze(s, ATHLETE)
    assert list(a.pace_bins) == [300]  # 5:00/km band
    secs, hr, cadence = a.pace_bins[300]
    assert secs == pytest.approx((40 - 11) * 60, abs=60)  # after warm-up + settling minute
    assert hr == pytest.approx(150, abs=0.5)
    assert cadence == pytest.approx(170) and a.cadence_spm == pytest.approx(170)
    assert a.steady_s == secs
    assert a.decoupling_pct is None  # under 45 min
    assert a.gap_s_km == pytest.approx(300, abs=1)  # flat: GAP equals pace
    assert not a.ride_like


def test_best_efforts_from_distance():
    a = analyze(make_streams(minutes=40), ATHLETE)  # 8 km at 5:00/km
    assert a.best[1000.0] == pytest.approx(300, abs=1)
    assert a.best[5000.0] == pytest.approx(1500, abs=1)
    assert 10000.0 not in a.best
    s = make_streams(minutes=40)
    s["velocity_smooth"][600:900] = 5.0  # 300 s surge covers 1500 m
    s["distance"] = np.cumsum(s["velocity_smooth"])
    assert analyze(s, ATHLETE).best[1000.0] == pytest.approx(200, abs=1)


def test_best_efforts_skip_stops():
    s = make_streams(minutes=40)
    # Stops at the 4 km mark, which every 5 km window of the run covers.
    s["moving"][1200:1380] = 0  # 3 min standing still, still recording
    s["velocity_smooth"][1200:1380] = 0
    s["distance"] = np.cumsum(s["velocity_smooth"])
    paused = make_streams(minutes=40)
    paused["time"] = paused["time"] + np.where(paused["time"] >= 1200, 600, 0)  # auto-pause gap
    for streams in (s, paused):
        assert analyze(streams, ATHLETE).best[5000.0] == pytest.approx(1500, abs=2)


def test_best_efforts_keep_real_time_through_gps_dropouts():
    s = make_streams(minutes=40)
    keep = (s["time"] < 1200) | (s["time"] >= 1320)  # 2 min with no samples, still running
    s = {k: v[keep] for k, v in s.items()}
    # The 400 m jump after the gap keeps its real 2 min, not the 10 s pause cap.
    assert analyze(s, ATHLETE).best[5000.0] == pytest.approx(1500, abs=2)


def test_ride_logged_as_run_detected():
    s = make_streams(minutes=10, speed=8.0)  # ~3:21/mi
    del s["heartrate"]
    a = analyze(s, ATHLETE)
    assert a.ride_like and a.best  # efforts still computed without HR
    s = make_streams(minutes=10, speed=6.0)  # ~4:28/mi: a fast run
    assert not analyze(s, ATHLETE).ride_like


def test_uphill_gap_is_faster_than_pace():
    s = make_streams(minutes=40)
    s["grade_smooth"][:] = 5
    assert analyze(s, ATHLETE).gap_s_km < 280
    s["grade_smooth"][:] = -3
    assert analyze(s, ATHLETE).gap_s_km > 300


def test_fitness_curves():
    ctl, atl, tsb = fitness_curves(np.full(400, 50.0))
    assert ctl[-1] == pytest.approx(50, abs=0.5) and atl[-1] == pytest.approx(50)
    assert tsb[-1] == pytest.approx(0, abs=0.5)
    load = np.zeros(100)
    load[90] = 300
    ctl, atl, tsb = fitness_curves(load)
    assert atl[90] > ctl[90] > 0 and tsb[91] < 0 and tsb[90] == 0  # form lags a day


def test_zones_and_trimp():
    a = analyze(make_streams(minutes=40, hr_end=150), ATHLETE)
    assert sum(a.zone_s) == pytest.approx(40 * 60 - 1, abs=1)
    assert a.zone_s.index(max(a.zone_s)) == 2  # 150 bpm is Z3
    assert a.trimp > 0
    assert a.ef == pytest.approx(200 / 148, rel=0.05)  # 200 m/min over ~148 bpm average


def test_decoupling_positive_when_hr_drifts():
    flat = analyze(make_streams(minutes=60), ATHLETE)
    drift = analyze(make_streams(minutes=60, drift=20), ATHLETE)
    assert flat.decoupling_pct == pytest.approx(0, abs=0.5)
    assert drift.decoupling_pct > 2


def test_unsteady_and_hilly_minutes_excluded():
    s = make_streams(minutes=40)
    s["velocity_smooth"] = s["velocity_smooth"] * (1 + 0.3 * np.sin(np.arange(len(s["time"]))))
    assert analyze(s, ATHLETE).pace_bins == {}
    s = make_streams(minutes=40)
    s["grade_smooth"][:] = 5
    assert analyze(s, ATHLETE).pace_bins == {}


def test_missing_channels():
    s = make_streams()
    del s["heartrate"]
    assert analyze(s, ATHLETE).trimp is None
    s = make_streams()
    del s["velocity_smooth"], s["distance"]
    a = analyze(s, ATHLETE)
    assert a.trimp and a.ef is None and a.pace_bins == {}


def test_pauses_dont_count():
    s = make_streams(minutes=40)
    s["time"] = s["time"].copy()
    s["time"][1200:] += 600  # 10-minute stop
    a = analyze(s, ATHLETE)
    assert sum(a.zone_s) == pytest.approx(40 * 60, abs=15)


def test_settings_validation():
    with pytest.raises(ValueError):
        AthleteSettings(max_hr=150, resting_hr=160)
    with pytest.raises(ValueError):
        AthleteSettings(zone_bounds=[150, 140, 160, 170])
    with pytest.raises(ValueError):
        AthleteSettings(max_hr=190, zone_bounds=[120, 140, 160, 200])
    assert AthleteSettings(max_hr=200).zones() == [120, 140, 160, 180]
