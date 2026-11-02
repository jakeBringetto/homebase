"""Garmin Connect Personal API: key-to-token exchange, rate-limited GETs, payload parsing.

Facts and gotchas: docs/garmin-api.md.
"""

import base64
import hashlib
import json
import logging
import sqlite3
import time
from datetime import UTC, date, datetime

import httpx
from pydantic import BaseModel

from app.core.jobs import RetryLater
from app.core.ratelimit import RateLimiter, Window
from app.modules.recovery import storage
from app.modules.recovery.config import GarminSettings
from app.modules.recovery.sources.base import Daily, Hrv, RecoverySource, Sleep

log = logging.getLogger(__name__)

API = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
AUTH_KEY = "garmin_auth"
# Range caps per call. HRV's isn't documented; 366 days worked in the spike.
DAILY_DAYS, HRV_DAYS, SLEEP_DAYS = 365, 365, 31

# Garmin publishes no limits or headers; these are self-imposed.
LIMITER = RateLimiter("garmin", [Window(60, 6), Window(900, 30), Window(86400, 300)])


class KeyInvalid(Exception):
    """The API key was rejected; a new key in .env (and a restart) is needed."""


class GarminAuth(BaseModel):
    """Cached 24 h token, plus a rejection reason tied to the key it was seen with."""

    key_id: str = ""  # sha256 prefix of the key, so a new key clears `invalid`
    jwt: str = ""
    exp: float = 0
    invalid: str | None = None


def make_http() -> httpx.Client:
    return httpx.Client(timeout=30)


def key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


def jwt_exp(token: str) -> float | None:
    """`exp` claim of a JWT (unverified: we only need to know when to re-exchange)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def auth_state(conn: sqlite3.Connection, settings: GarminSettings) -> GarminAuth:
    """Stored auth for the current key (a fresh state if the key changed)."""
    kid = key_id(settings.api_key)
    auth = storage.kv_get(conn, AUTH_KEY, GarminAuth)
    return auth if auth and auth.key_id == kid else GarminAuth(key_id=kid)


def _deferred(r: httpx.Response) -> None:
    if r.status_code in (429, 503):
        raise RetryLater(900, f"garmin returned {r.status_code}")


class GarminSource(RecoverySource):
    name = "garmin"

    def __init__(self, conn: sqlite3.Connection, settings: GarminSettings, http: httpx.Client):
        self.conn = conn
        self.settings = settings
        self.http = http

    def _reject(self, auth: GarminAuth, reason: str) -> KeyInvalid:
        auth.invalid = reason
        storage.kv_set(self.conn, AUTH_KEY, auth)
        self.conn.commit()
        return KeyInvalid(reason)

    def _token(self, force: bool = False) -> str:
        auth = auth_state(self.conn, self.settings)
        if auth.invalid:
            raise KeyInvalid(auth.invalid)
        if not force and auth.jwt and auth.exp > time.time() + 300:
            return auth.jwt
        LIMITER.acquire(self.conn)
        # No Accept: application/json, which gets a 406. The body is the bare JWT.
        r = self.http.post(
            f"{API}{TOKEN_PATH}", headers={"Authorization": f"ApiKey {self.settings.api_key}"}
        )
        _deferred(r)
        if r.status_code in (401, 403):
            raise self._reject(auth, f"key rejected ({r.status_code}); create a new one")
        r.raise_for_status()
        auth.jwt = r.text.strip()
        auth.exp = jwt_exp(auth.jwt) or time.time() + 86400
        storage.kv_set(self.conn, AUTH_KEY, auth)
        self.conn.commit()
        return auth.jwt

    def _get(self, path: str, params: dict | None = None):
        """Rate-limited GET with one re-exchange on 401; returns JSON, or None on 204."""
        for attempt in range(2):
            token = self._token(force=attempt > 0)
            LIMITER.acquire(self.conn)
            r = self.http.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
            )
            _deferred(r)
            if r.status_code == 204:
                return None
            if r.status_code == 403:
                raise self._reject(auth_state(self.conn, self.settings), "missing API scope (403)")
            if r.status_code != 401:
                r.raise_for_status()
                return r.json()
        raise self._reject(auth_state(self.conn, self.settings), "token rejected twice (401)")

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
    )
