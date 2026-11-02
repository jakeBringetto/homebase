"""Steady-state detection: which minutes of a run had stable pace on flat ground.

HR only reflects pace once it has settled, so a minute counts only if it and the minute
before it were both stable at a similar pace, after a warm-up.
"""

import numpy as np

SEGMENT_S = 60
WARMUP_S = 600
MAX_SPEED_CV = 0.08  # within-minute speed variation
MAX_PACE_CHANGE = 0.05  # vs previous minute
MAX_GRADE_PCT = 2.0
MIN_SPEED = 1.5  # m/s (~11:00/km); slower is walking
BAND_S = 5  # pace band width, s/km


def pace_bins(
    t: np.ndarray,
    w: np.ndarray,
    speed: np.ndarray,
    hr: np.ndarray,
    grade: np.ndarray | None,
    cadence_spm: np.ndarray | None = None,
) -> dict[int, tuple[float, float, float | None]]:
    """Steady minutes binned by pace: {band floor s/km: (seconds, avg HR, avg cadence)}.

    `w` is each sample's weight in seconds (0 when not moving or HR missing).
    """
    seg = ((t - t[0]) // SEGMENT_S).astype(np.int64)
    n = int(seg[-1]) + 1
    secs = np.bincount(seg, w, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.bincount(seg, w * speed, n) / secs
        v2 = np.bincount(seg, w * speed**2, n) / secs
        cv = np.sqrt(np.maximum(v2 - v**2, 0)) / v
        seg_hr = np.bincount(seg, w * hr, n) / secs
        g = np.bincount(seg, w * np.abs(grade), n) / secs if grade is not None else np.zeros(n)
        cad = np.bincount(seg, w * cadence_spm, n) / secs if cadence_spm is not None else None
        stable = (
            (secs >= 0.9 * SEGMENT_S)
            & (cv <= MAX_SPEED_CV)
            & (v >= MIN_SPEED)
            & (g <= MAX_GRADE_PCT)
            & (np.arange(n) * SEGMENT_S >= WARMUP_S)
        )
        settled = np.zeros(n, dtype=bool)
        settled[1:] = stable[:-1] & (np.abs(v[1:] / v[:-1] - 1) <= MAX_PACE_CHANGE)
    steady = stable & settled
    pace = np.round(1000 / v[steady], 2)  # avoid float32 noise flipping a band edge
    bands = (pace // BAND_S * BAND_S).astype(np.int64)
    out: dict[int, tuple[float, float, float | None]] = {}
    for band in np.unique(bands):
        m = bands == band
        s = secs[steady][m]
        avg_cad = float((cad[steady][m] * s).sum() / s.sum()) if cad is not None else None
        out[int(band)] = (float(s.sum()), float((seg_hr[steady][m] * s).sum() / s.sum()), avg_cad)
    return out
