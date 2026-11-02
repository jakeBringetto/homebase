"""Training load: Banister TRIMP and fitness/fatigue/form (CTL/ATL/TSB) curves."""

import numpy as np

CTL_DAYS = 42
ATL_DAYS = 7


def trimp(minutes, hrr):
    """Banister TRIMP for time at heart-rate reserve fraction `hrr` (scalars or arrays)."""
    return minutes * hrr * 0.64 * np.exp(1.92 * hrr)


def hrr(hr, resting: float, max_hr: float):
    return np.clip((hr - resting) / (max_hr - resting), 0, 1)


def fitness_curves(daily_load: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """CTL and ATL as exponential averages of daily load; TSB is yesterday's CTL - ATL."""
    ctl = np.zeros(len(daily_load))
    atl = np.zeros(len(daily_load))
    c = a = 0.0
    for i, load in enumerate(daily_load):
        c += (load - c) / CTL_DAYS
        a += (load - a) / ATL_DAYS
        ctl[i], atl[i] = c, a
    tsb = np.concatenate(([0.0], ctl[:-1] - atl[:-1]))
    return ctl, atl, tsb
