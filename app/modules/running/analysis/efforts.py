"""Best efforts: fastest elapsed time over standard distances within one activity."""

import numpy as np

DISTANCES_M = (400.0, 800.0, 1000.0, 1609.344, 5000.0, 10000.0, 21097.5, 42195.0)
LABELS = {400.0: "400 m", 800.0: "800 m", 1000.0: "1 km", 1609.344: "1 mi", 5000.0: "5 km",
          10000.0: "10 km", 21097.5: "Half marathon", 42195.0: "Marathon"}  # fmt: skip


def best_efforts(clock: np.ndarray, distance: np.ndarray) -> dict[float, float]:
    """{distance m: fastest seconds} for each standard distance the activity covers.

    `clock` is cumulative moving time (`metrics._effort_clock`), so stops (stretching,
    lights, a run left recording) don't count against an effort.
    """
    keep = np.concatenate(([True], np.diff(clock) > 0))  # drop stopped samples
    t, distance = clock[keep], distance[keep]
    if len(t) < 2:
        return {}
    grid = np.arange(t[0], t[-1] + 1)  # resample to 1 s; Strava thins streams
    d = np.maximum.accumulate(np.interp(grid, t, distance))  # GPS can step backwards
    idx = np.arange(len(d))
    out = {}
    for target in DISTANCES_M:
        if d[-1] - d[0] < target:
            break
        end = np.searchsorted(d, d + target)  # first second at or past start + target
        ok = end < len(d)
        out[target] = float((end[ok] - idx[ok]).min())
    return out
