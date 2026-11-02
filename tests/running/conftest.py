"""Fake Strava API and stream builders shared by running tests."""

import time
from datetime import UTC, datetime

import httpx
import numpy as np
import pytest

from app.core import db
from app.modules.running import Running, config, storage
from app.modules.running.sources import strava


def make_streams(minutes: int = 40, speed: float = 1000 / 300, hr_end: float = 150, drift=0.0):
    """1 Hz run at constant speed; HR ramps over 5 min to `hr_end`, then drifts linearly."""
    t = np.arange(minutes * 60, dtype=np.float64)
    hr = np.minimum(100 + (hr_end - 100) * t / 300, hr_end) + drift * t / t[-1]
    v = np.full_like(t, speed)
    return {
        "time": t,
        "heartrate": hr,
        "velocity_smooth": v,
        "distance": np.cumsum(v),
        "grade_smooth": np.zeros_like(t),
        "moving": np.ones_like(t),
    }


def summary(i: int, start: int, sport: str = "Run", manual: bool = False) -> dict:
    iso = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": i, "name": f"Run {i}", "sport_type": sport, "start_date": iso,
        "start_date_local": iso, "moving_time": 2400, "elapsed_time": 2500, "distance": 8000.0,
        "total_elevation_gain": 20.0, "average_heartrate": 145.0, "max_heartrate": 160.0,
        "manual": manual,
    }  # fmt: skip


class FakeStrava:
    """In-memory Strava: newest-first activity list, streams per id, token endpoint."""

    def __init__(self, activities: list[dict]):
        self.activities = sorted(activities, key=lambda a: a["start_date"], reverse=True)
        self.calls: list[str] = []
        self.status_override: dict[str, int] = {}
        self.gear: dict[str, dict] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        headers = {"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": f"{len(self.calls)},10"}
        if path in self.status_override:
            return httpx.Response(self.status_override.pop(path), headers=headers)
        if path == "/oauth/token":
            expires = int(time.time()) + 3600
            return httpx.Response(200, json={
                "access_token": "acc", "refresh_token": "ref", "expires_at": expires,
                "athlete": {"id": 42},
            })  # fmt: skip
        if path == "/api/v3/athlete/activities":
            q = req.url.params
            acts = self.activities
            if "before" in q:
                acts = [a for a in acts if _ts(a) < int(q["before"])]
            if "after" in q:
                acts = [a for a in acts if _ts(a) > int(q["after"])]
            per, page = int(q["per_page"]), int(q.get("page", 1))
            return httpx.Response(200, json=acts[(page - 1) * per : page * per], headers=headers)
        if path.startswith("/api/v3/gear/") and (g := self.gear.get(path.rsplit("/", 1)[1])):
            return httpx.Response(200, json=g, headers=headers)
        if path.endswith("/streams"):
            s = make_streams()
            body = {k: {"data": v.tolist()} for k, v in s.items()}
            return httpx.Response(200, json=body)  # real Strava sends no usage headers here
        return httpx.Response(404, headers=headers)


def _ts(a: dict) -> int:
    return int(datetime.fromisoformat(a["start_date"]).timestamp())


@pytest.fixture
def running_conn(settings):
    c = db.connect(settings.db_path)
    sources = {"core": db.MIGRATIONS_DIR, "running": Running().migrations_dir()}
    db.migrate(c, sources, settings.backup_dir)
    yield c
    c.close()


@pytest.fixture
def fake(monkeypatch):
    """Route Strava HTTP to a FakeStrava with 3 recent runs; credentials configured."""
    now = int(time.time())
    f = FakeStrava([summary(i, now - i * 86400) for i in range(1, 4)])
    monkeypatch.setattr(strava, "make_http", lambda: httpx.Client(transport=httpx.MockTransport(f)))
    monkeypatch.setattr(
        config, "get_strava_settings",
        lambda: config.StravaSettings(_env_file=None, client_id="1", client_secret="s"),
    )  # fmt: skip
    return f


def connect(conn) -> None:
    storage.kv_set(
        conn,
        strava.TOKEN_KEY,
        strava.StravaToken(access_token="a", refresh_token="r", expires_at=int(time.time()) + 3600),
    )
    conn.commit()
