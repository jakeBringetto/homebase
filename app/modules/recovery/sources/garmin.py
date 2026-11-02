"""Garmin wellness endpoints (daily summaries, HRV, sleep) and their payload parsing.

Auth, rate limits and the HTTP client are shared: `app/core/garmin.py`.
"""

import json
from datetime import UTC, date, datetime

from app.core.garmin import GarminClient
from app.modules.recovery.sources.base import Daily, Hrv, RecoverySource, Sleep

# Range caps per call. HRV's isn't documented; 366 days worked in the spike.
DAILY_DAYS, HRV_DAYS, SLEEP_DAYS = 365, 365, 31


class GarminSource(RecoverySource):
    name = "garmin"

    def __init__(self, client: GarminClient):
        self._get = client.get

    def daily(self, start: date, end: date) -> list[Daily]:
        body = self._get(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        items = (body or {}).get("data") or []
        return [d for item in items if (d := parse_daily(item))]

    def hrv(self, start: date, end: date) -> list[Hrv]:
        body = self._get(f"/hrv-service/hrv/daily/{start.isoformat()}/{end.isoformat()}")
        items = (body or {}).get("hrvSummaries") or []
        return [h for item in items if (h := parse_hrv(item))]

    def sleep(self, start: date, end: date) -> list[Sleep]:
        body = self._get(
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": start.isoformat(), "endDate": end.isoformat()},
        )
        return [s for item in body or [] if (s := parse_sleep(item))]


def _num(v) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _int(v) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _obj(item: dict, key: str) -> dict:
    v = item.get(key)
    return v if isinstance(v, dict) else {}


def _epoch(v) -> int | None:
    """Garmin GMT timestamp (ms epoch, or an ISO string without offset) as unix seconds."""
    if isinstance(v, int | float) and not isinstance(v, bool):
        return int(v // 1000)
    if isinstance(v, str):
        try:
            return int(datetime.fromisoformat(v).replace(tzinfo=UTC).timestamp())
        except ValueError:
            return None
    return None


def _offset(local, gmt) -> int | None:
    """Local minus UTC in seconds, from a `*TimestampLocal` / `*TimestampGMT` pair (ms epochs)."""
    if (lo := _num(local)) is None or (g := _num(gmt)) is None:
        return None
    s = round((lo - g) / 1000 / 900) * 900  # time zones are whole quarter hours
    return s if abs(s) <= 14 * 3600 else None


def _day(item: dict) -> str | None:
    d = item.get("calendarDate")
    try:
        return date.fromisoformat(d).isoformat() if isinstance(d, str) else None
    except ValueError:
        return None


def parse_daily(item: dict) -> Daily | None:
    if not isinstance(item, dict) or not (day := _day(item)):
        return None
    hr, stress, bb = _obj(item, "heartRate"), _obj(item, "stress"), _obj(item, "bodyBattery")
    stress_avg = _num(stress.get("avgLevel"))
    no_stress = stress_avg is None or stress_avg < 0  # -1 means no data
    return Daily(
        day=day,
        resting_hr=_num(hr.get("restingValue")),
        stress_avg=None if no_stress else stress_avg,
        stress_max=None if no_stress else _num(stress.get("maxLevel")),
        bb_min=_num(bb.get("minValue")),
        bb_max=_num(bb.get("maxValue")),
        bb_charged=_num(bb.get("chargedValue")),
        bb_drained=_num(bb.get("drainedValue")),
        resp_avg=_num(_obj(item, "respiration").get("avgValue")),
    )


def parse_hrv(item: dict) -> Hrv | None:
    if not isinstance(item, dict) or not (day := _day(item)):
        return None
    base = _obj(item, "baseline")
    status = item.get("status")
    return Hrv(
        day=day,
        last_night_avg=_num(item.get("lastNightAvg")),
        last_night_5min_high=_num(item.get("lastNight5MinHigh")),
        weekly_avg=_num(item.get("weeklyAvg")),
        status=status if isinstance(status, str) else None,
        balanced_low=_num(base.get("balancedLow")),
        balanced_upper=_num(base.get("balancedUpper")),
        low_upper=_num(base.get("lowUpper")),
    )


def parse_sleep(item: dict) -> Sleep | None:
    """None for Garmin's all-null placeholder rows on nights without sleep."""
    if not isinstance(item, dict) or not (day := _day(item)):
        return None
    if not (sleep_s := _int(item.get("sleepTimeSeconds"))):
        return None
    scores = _obj(item, "sleepScores")
    return Sleep(
        day=day,
        start_utc=_epoch(item.get("sleepStartTimestampGMT")),
        end_utc=_epoch(item.get("sleepEndTimestampGMT")),
        sleep_s=sleep_s,
        deep_s=_int(item.get("deepSleepSeconds")),
        light_s=_int(item.get("lightSleepSeconds")),
        rem_s=_int(item.get("remSleepSeconds")),
        awake_s=_int(item.get("awakeSleepSeconds")),
        awake_count=_int(item.get("awakeCount")),
        avg_hr=_num(item.get("avgHeartRate")),
        avg_stress=_num(item.get("avgSleepStress")),
        resp_avg=_num(item.get("averageRespirationValue")),
        score=_int(_obj(scores, "overall").get("value")),
        scores_json=json.dumps(scores) if scores else None,
        utc_offset_s=_offset(
            item.get("sleepStartTimestampLocal"), item.get("sleepStartTimestampGMT")
        ),
    )
