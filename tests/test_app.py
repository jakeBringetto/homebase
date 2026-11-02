import sqlite3
import time

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.auth import verify_password
from app.core.module import Module, Tile
from app.main import create_app
from tests.conftest import PASSWORD


class Demo(Module):
    slug = "demo"
    title = "Demo"

    def router(self):
        r = APIRouter()
        r.get("/")(lambda: {"ok": True})
        return r

    def tile(self, conn):
        return Tile(["3 things"])


class Broken(Demo):
    slug = "broken"
    title = "Broken"

    def tile(self, conn):
        raise RuntimeError("nope")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, [Demo(), Broken()])) as c:
        yield c


def login(client):
    return client.post("/login", data={"password": PASSWORD}, follow_redirects=False)


def test_password_hash_roundtrip(password_hash):
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password(PASSWORD, "garbage")


def test_requires_login(client):
    r = client.get("/demo/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/demo/"
    assert client.get("/", headers={"HX-Request": "true"}).status_code == 401
    assert client.get("/healthz").json()["ok"]
    assert client.get("/static/app.css").status_code == 200


def test_login_and_homepage(client):
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    r = login(client)
    assert r.status_code == 303
    page = client.get("/").text
    assert "3 things" in page
    assert "Unavailable" in page  # broken tile isolated
    assert client.get("/demo/").json() == {"ok": True}
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_open_redirect_blocked(client):
    r = client.post(
        "/login", data={"password": PASSWORD, "next": "//evil.com"}, follow_redirects=False
    )
    assert r.headers["location"] == "/"


def test_worker_runs_maintenance_on_start(client, settings):
    conn = sqlite3.connect(settings.db_path)
    query = "SELECT status FROM jobs WHERE kind = 'core.maintenance' ORDER BY id"
    for _ in range(50):
        statuses = [r[0] for r in conn.execute(query)]
        if statuses == ["done", "pending"]:
            break
        time.sleep(0.1)
    assert statuses == ["done", "pending"]
    assert list(settings.backup_dir.glob("homebase-daily-*.db"))
    query = "SELECT status FROM jobs WHERE kind = 'core.restore_check'"
    for _ in range(50):
        if [r[0] for r in conn.execute(query)] == ["done"]:
            break
        time.sleep(0.1)
    assert [r[0] for r in conn.execute(query)] == ["done"]  # the backup is test-restored


def test_rejects_unprefixed_job_kinds(settings):
    class Bad(Demo):
        def handlers(self):
            return {"other.x": lambda c, p: None}

    with pytest.raises(ValueError):
        create_app(settings, [Bad()])


def test_login_disabled_without_password_hash(settings):
    settings = settings.model_copy(update={"password_hash": ""})
    with TestClient(create_app(settings, [Demo()])) as c:
        assert "3 things" in c.get("/").text
        assert "Log out" not in c.get("/").text
        assert c.get("/login", follow_redirects=False).headers["location"] == "/"


def test_settings_tolerate_empty_env(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("HOMEBASE_PASSWORD_HASH", "")
    monkeypatch.setenv("HOMEBASE_SECRET_KEY", "")
    s = Settings(_env_file=None)
    assert not s.auth_enabled and len(s.secret_key) >= 32
