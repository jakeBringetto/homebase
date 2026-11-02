"""Fake Garmin Personal API shared by recovery tests."""

import base64
import json
import time
from datetime import date, timedelta

import httpx
import pytest

from app.core import db
from app.core import garmin as client
from app.modules.recovery import Recovery
from app.modules.running import Running

KEY = "GCDC_test"


def make_jwt(exp: float) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp)})}.sig"


def _days(start: str, end: str) -> list[date]:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return [s + timedelta(days=i) for i in range((e - s).days + 1)]


class FakeGarmin:
    """Data for every day except `missing` (not worn): HRV null, sleep placeholder, no stress."""

    def __init__(self, missing: set[date] = frozenset()):
        self.missing = set(missing)
        self.calls: list[tuple[str, dict]] = []
        self.status_override: dict[str, list[int]] = {}
        self.activities: list[dict] = []  # activity search results, newest first

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, dict(req.url.params)
        self.calls.append((path, q))
        if codes := self.status_override.get(path):
            return httpx.Response(codes.pop(0))
        if path == client.TOKEN_PATH:
            if req.headers.get("accept") == "application/json":
                return httpx.Response(406)
            assert req.headers["authorization"] == f"ApiKey {KEY}"
            return httpx.Response(200, text=make_jwt(time.time() + 86400))
        assert req.headers["authorization"].startswith("Bearer ")
        if path == "/usersummary-service/v2/dailysummaries":
            return httpx.Response(
                200, json={"data": [self._daily(d) for d in _days(q["startDate"], q["endDate"])]}
            )
        if path.startswith("/hrv-service/hrv/daily/"):
            start, end = path.split("/")[-2:]
            return httpx.Response(
                200, json={"hrvSummaries": [self._hrv(d) for d in _days(start, end)]}
            )
        if path == "/sleep-service/sleep/dailySleepSummariesByDate":
            return httpx.Response(
                200, json=[self._sleep(d) for d in _days(q["startDate"], q["endDate"])]
            )
        if path == "/activitylist-service/activities/search/activities":
            start, limit = int(q["start"]), int(q["limit"])
            return httpx.Response(200, json=self.activities[start : start + limit])
        return httpx.Response(404)  # fmt: skip

    def _daily(self, d: date) -> dict:
        worn = d not in self.missing
        return {
            "calendarDate": d.isoformat(),
            "heartRate": {"restingValue": 50 if worn else 70, "minValue": 48, "maxValue": 160},
            "stress": {"avgLevel": 30 if worn else -1, "maxLevel": 90 if worn else -1},
            "bodyBattery": {"minValue": 20 if worn else 37, "maxValue": 80 if worn else 37,
                            "chargedValue": 50, "drainedValue": 45},
            "respiration": {"avgValue": 14},
        }  # fmt: skip

    def _hrv(self, d: date) -> dict:
        return {
            "calendarDate": d.isoformat(),
            "lastNightAvg": None if d in self.missing else 70,
            "lastNight5MinHigh": None if d in self.missing else 95,
            "weeklyAvg": 70,
            "status": "BALANCED",
            "baseline": {"lowUpper": 55, "balancedLow": 60, "balancedUpper": 90},
        }

    def _sleep(self, d: date) -> dict:
        if d in self.missing:
            return {"calendarDate": None, "sleepTimeSeconds": None, "sleepNeed": {}, "retro": False}
        start_ms = int(time.mktime(d.timetuple())) * 1000 - 8 * 3600 * 1000
        return {
            "calendarDate": d.isoformat(),
            "sleepStartTimestampGMT": start_ms, "sleepEndTimestampGMT": start_ms + 28800000,
            "sleepStartTimestampLocal": start_ms - 7 * 3600 * 1000,
            "sleepTimeSeconds": 27000, "deepSleepSeconds": 6000, "lightSleepSeconds": 15000,
            "remSleepSeconds": 6000, "awakeSleepSeconds": 1800, "awakeCount": 2,
            "avgHeartRate": 49.0, "avgSleepStress": 15.0, "averageRespirationValue": 13.5,
            "sleepScores": {"overall": {"value": 83, "qualifierKey": "GOOD"}},
        }  # fmt: skip

    def data_calls(self) -> list[str]:
        return [p for p, _ in self.calls if p != client.TOKEN_PATH]


@pytest.fixture
def recovery_conn(settings):
    c = db.connect(settings.db_path)
    sources = {
        "core": db.MIGRATIONS_DIR,
        "running": Running().migrations_dir(),
        "recovery": Recovery().migrations_dir(),
    }
    db.migrate(c, sources, settings.backup_dir)
    yield c
    c.close()


@pytest.fixture
def garmin_fake(monkeypatch):
    """Route Garmin HTTP to a FakeGarmin; key configured."""
    f = FakeGarmin()
    monkeypatch.setattr(client, "make_http", lambda: httpx.Client(transport=httpx.MockTransport(f)))
    set_key(monkeypatch, KEY)
    return f


def set_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(
        client, "get_settings", lambda: client.GarminSettings(_env_file=None, api_key=key)
    )
