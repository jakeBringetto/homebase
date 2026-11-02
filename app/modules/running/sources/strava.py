"""Strava API source: OAuth tokens, rate-limited GETs, payload parsing."""

import logging
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx
import numpy as np
from pydantic import BaseModel

from app.core.jobs import RetryLater
from app.core.ratelimit import RateLimiter, Window, next_reset
from app.modules.running import storage
from app.modules.running.config import StravaSettings
from app.modules.running.sources.base import Activity, RunningSource, Streams

log = logging.getLogger(__name__)

API = "https://www.strava.com/api/v3"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
SCOPE = "activity:read_all"
STREAM_KEYS = "time,distance,heartrate,velocity_smooth,cadence,altitude,grade_smooth,moving"
PER_PAGE = 200
TOKEN_KEY = "strava_token"

# Read limits as reported by headers (overall is 200/2000); observe() keeps them current.
LIMITER = RateLimiter("strava", [Window(900, 100), Window(86400, 1000)], reserve=5)


class NotConnected(Exception):
    """No Strava token stored yet."""


class StravaToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: float
    athlete_id: int | None = None


class _SummaryActivity(BaseModel):
    """Fields we use from Strava's SummaryActivity; extras ignored."""

    id: int
    name: str = ""
    sport_type: str = ""
    type: str = ""
    start_date: datetime
    start_date_local: datetime
    moving_time: int = 0
    elapsed_time: int = 0
    distance: float = 0.0
    total_elevation_gain: float = 0.0
    average_heartrate: float | None = None
    max_heartrate: float | None = None
    manual: bool = False

    def to_activity(self) -> Activity:
        return Activity(
            source="strava",
            source_id=str(self.id),
            sport=self.sport_type or self.type,
            name=self.name,
            start_time=int(self.start_date.timestamp()),
            start_local=self.start_date_local.replace(tzinfo=None).isoformat(timespec="seconds"),
            moving_s=self.moving_time,
            elapsed_s=self.elapsed_time,
            distance_m=self.distance,
            elev_gain_m=self.total_elevation_gain,
            avg_hr=self.average_heartrate,
            max_hr=self.max_heartrate,
            is_manual=self.manual,
        )


def make_http() -> httpx.Client:
    return httpx.Client(timeout=30)


def authorize_url(settings: StravaSettings, redirect_uri: str, state: str) -> str:
    query = {
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def _token_request(http: httpx.Client, settings: StravaSettings, **grant) -> dict:
    r = http.post(
        TOKEN_URL,
        data={"client_id": settings.client_id, "client_secret": settings.client_secret, **grant},
    )
    r.raise_for_status()
    return r.json()


def exchange_code(
    conn: sqlite3.Connection, http: httpx.Client, settings: StravaSettings, code: str
) -> StravaToken:
    """Trade an OAuth code for tokens and store them."""
    data = _token_request(http, settings, code=code, grant_type="authorization_code")
    token = StravaToken(**data, athlete_id=(data.get("athlete") or {}).get("id"))
    storage.kv_set(conn, TOKEN_KEY, token)
    conn.commit()
    return token


def parse_usage(headers: httpx.Headers) -> dict[int, tuple[int, int]]:
    """`{window_s: (limit, used)}` from rate-limit headers, taking the tighter of overall/read."""
    out: dict[int, tuple[int, int]] = {}
    for prefix in ("X-RateLimit", "X-ReadRateLimit"):
        limit, usage = headers.get(f"{prefix}-Limit"), headers.get(f"{prefix}-Usage")
        if not (limit and usage):
            continue
        try:
            limits = [int(x) for x in limit.split(",")]
            used_ = [int(x) for x in usage.split(",")]
        except ValueError:
            log.warning("unparseable %s headers: %r %r", prefix, limit, usage)
            continue
        for window, lim, used in zip((900, 86400), limits, used_, strict=False):
            if window not in out or lim - used < out[window][0] - out[window][1]:
                out[window] = (lim, used)
    return out


class StravaSource(RunningSource):
    name = "strava"

    def __init__(self, conn: sqlite3.Connection, settings: StravaSettings, http: httpx.Client):
        self.conn = conn
        self.settings = settings
        self.http = http

    def _access_token(self, force_refresh: bool = False) -> str:
        token = storage.kv_get(self.conn, TOKEN_KEY, StravaToken)
        if token is None:
            raise NotConnected("connect Strava on the Running > Sync page")
        if force_refresh or token.expires_at < time.time() + 300:
            data = _token_request(
                self.http, self.settings, grant_type="refresh_token",
                refresh_token=token.refresh_token,
            )  # fmt: skip
            token = StravaToken(**data, athlete_id=token.athlete_id)
            storage.kv_set(self.conn, TOKEN_KEY, token)
            self.conn.commit()
        return token.access_token

    def _get(self, path: str, params: dict) -> httpx.Response:
        """Rate-limited GET with one token refresh on 401; raises RetryLater on 429."""
        for attempt in range(2):
            token = self._access_token(force_refresh=attempt > 0)
            LIMITER.acquire(self.conn)
            r = self.http.get(
                f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if usage := parse_usage(r.headers):
                LIMITER.observe(self.conn, usage)
            elif r.status_code != 429:
                LIMITER.release(self.conn)  # no usage headers: Strava didn't count it (streams)
            if r.status_code == 429:
                now = time.time()
                raise RetryLater(next_reset(900, now) - now + 5, "strava returned 429")
            if r.status_code != 401:
                return r
        r.raise_for_status()
        return r

    def activities(
        self, *, after: int | None = None, before: int | None = None, page: int = 1
    ) -> list[Activity]:
        params = {"per_page": PER_PAGE, "page": page}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        r = self._get("/athlete/activities", params)
        r.raise_for_status()
        return [_SummaryActivity.model_validate(a).to_activity() for a in r.json()]

    def streams(self, source_id: str) -> Streams | None:
        r = self._get(
            f"/activities/{int(source_id)}/streams", {"keys": STREAM_KEYS, "key_by_type": "true"}
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return parse_streams(r.json())


def parse_streams(payload: dict) -> Streams | None:
    """Keep numeric channels matching the time channel's length; None if there's no time."""
    if not isinstance(payload, dict) or "time" not in payload:
        return None
    out: Streams = {}
    n = len(payload["time"].get("data", []))
    for key, stream in payload.items():
        data = stream.get("data") if isinstance(stream, dict) else None
        if not data or len(data) != n:
            continue
        try:
            arr = np.asarray(data, dtype=np.float32)
        except (TypeError, ValueError):
            arr = None
        if arr is None or arr.ndim != 1:
            log.debug("dropping stream %r: not a numeric series", key)
            continue
        out[key] = arr
    return out if n >= 2 else None
