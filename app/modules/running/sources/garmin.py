"""Garmin activity search: per-run power and running dynamics for watch-recorded runs.

Strava stays the activity source; these rows only add form metrics (joined on start time).
Auth, rate limits and the HTTP client are shared: `app/core/garmin.py`.
"""

from dataclasses import dataclass
from datetime import date

from app.core.garmin import GarminClient

SEARCH_PATH = "/activitylist-service/activities/search/activities"
PAGE = 99  # the documented default `limit`


@dataclass(frozen=True)
class GarminRun:
    garmin_id: str
    start_time: int
    distance_m: float | None
    avg_power_w: float | None
    gct_ms: float | None
    vert_osc_cm: float | None
    vert_ratio: float | None
    stride_cm: float | None
    cadence_spm: float | None


def fetch_runs(client: GarminClient, start: date, end: date) -> list[GarminRun]:
    """Runs started between two local days (inclusive), paged 99 per call."""
    runs, offset = [], 0
    while True:
        params = {"startDate": start.isoformat(), "endDate": end.isoformat(),
                  "activityType": "running", "start": offset, "limit": PAGE}  # fmt: skip
        page = client.get(SEARCH_PATH, params) or []
        runs += [r for item in page if (r := parse_run(item))]
        if not isinstance(page, list) or len(page) < PAGE:
            return runs
        offset += PAGE


def _num(v) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def parse_run(item: dict) -> GarminRun | None:
    """None unless it's a running activity with an id and start time."""
    if not isinstance(item, dict):
        return None
    kind = item.get("activityType")
    is_run = isinstance(kind, dict) and "running" in str(kind.get("typeKey", ""))
    begin, aid = _num(item.get("beginTimestamp")), item.get("activityId")
    if not (is_run and begin and aid):
        return None
    return GarminRun(
        garmin_id=str(aid),
        start_time=int(begin // 1000),
        distance_m=_num(item.get("distance")),
        avg_power_w=_num(item.get("avgPower")),
        gct_ms=_num(item.get("avgGroundContactTime")),
        vert_osc_cm=_num(item.get("avgVerticalOscillation")),
        vert_ratio=_num(item.get("avgVerticalRatio")),
        stride_cm=_num(item.get("avgStrideLength")),
        cadence_spm=_num(item.get("averageRunningCadenceInStepsPerMinute")),
    )
