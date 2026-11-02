"""Garmin Connect Personal API client shared by modules: key-to-token exchange, rate-limited GETs.

One key, one cached token and one limiter for every module (auth lives in `core_kv`).
Facts and gotchas: docs/garmin-api.md.
"""

import base64
import hashlib
import json
import sqlite3
import time
from functools import lru_cache

import httpx
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core import kv
from app.core.jobs import RetryLater
from app.core.ratelimit import RateLimiter, Window

API = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
AUTH_KEY = "garmin_auth"
KV_TABLE = "core_kv"

# Garmin publishes no limits or headers; these are self-imposed.
LIMITER = RateLimiter("garmin", [Window(60, 6), Window(900, 30), Window(86400, 300)])


class GarminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GARMIN_", env_file=".env", extra="ignore")

    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@lru_cache
def get_settings() -> GarminSettings:
    return GarminSettings()


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
    auth = kv.get(conn, KV_TABLE, AUTH_KEY, GarminAuth)
    return auth if auth and auth.key_id == kid else GarminAuth(key_id=kid)


def _deferred(r: httpx.Response) -> None:
    if r.status_code in (429, 503):
        raise RetryLater(900, f"garmin returned {r.status_code}")


class GarminClient:
    def __init__(self, conn: sqlite3.Connection, settings: GarminSettings, http: httpx.Client):
        self.conn = conn
        self.settings = settings
        self.http = http

    def _reject(self, auth: GarminAuth, reason: str) -> KeyInvalid:
        auth.invalid = reason
        kv.put(self.conn, KV_TABLE, AUTH_KEY, auth)
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
        kv.put(self.conn, KV_TABLE, AUTH_KEY, auth)
        self.conn.commit()
        return auth.jwt

    def get(self, path: str, params: dict | None = None):
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
