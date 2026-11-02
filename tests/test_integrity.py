import time
from datetime import date, timedelta

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core import db, integrity, jobs, kv
from app.core.module import Module, SyncStatus, Tile
from app.main import create_app
from tests.conftest import PASSWORD
from tests.recovery.conftest import KEY, set_key


class Source(Module):
    slug = "src"
    title = "Source"
    last_ok: float | None = None

    def router(self):
        return APIRouter()

    def tile(self, conn):
        return Tile([])

    def syncs(self, conn):
        return [SyncStatus("Thing", self.last_ok)]


def restore_check(conn, settings):
    integrity.restore_check(settings)(conn, {})
    return kv.get(conn, "core_kv", integrity.RESTORE_KEY, integrity.RestoreCheck)


def test_restore_check_reads_back_the_newest_backup(conn, settings):
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(10)])
    backup = db.backup(conn, settings.backup_dir, "daily")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    rows = sum(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for (t,) in tables)
    check = restore_check(conn, settings)
    assert check.ok and check.backup == backup.name
    assert check.detail == f"{len(tables)} tables, {rows} rows"
    assert not (settings.data_dir / "restore-check.db").exists()  # scratch copy removed


def test_restore_check_records_a_damaged_backup(conn, settings):
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [("x" * 500,) for _ in range(200)])
    backup = db.backup(conn, settings.backup_dir, "daily")
    with backup.open("r+b") as f:
        f.seek(4096 * 3)
        f.write(b"\xff" * 4096)
    check = restore_check(conn, settings)
    assert not check.ok
    assert integrity.report(conn, settings, [])["restore"]["issue"].startswith("Last check failed")


def test_report_counts_issues_until_everything_is_fresh(conn, settings):
    src = Source()
    r = integrity.report(conn, settings, [src])
    assert [s["issue"] for s in r["syncs"]] == ["Never synced"]
    assert r["backup"]["issue"] == "No backup yet"
    assert r["restore"]["issue"] == "No verified restore yet"
    assert r["issues"] == 3

    src.last_ok = time.time() - 3 * 86400
    jobs.enqueue(conn, "x.job", max_attempts=1)
    conn.execute("UPDATE jobs SET status = 'failed', last_error = 'boom'")
    r = integrity.report(conn, settings, [src])
    assert r["syncs"][0]["issue"] == "more than 2 days ago"
    assert [j["last_error"] for j in r["failed"]] == ["boom"]
    assert r["issues"] == 4

    conn.execute("DELETE FROM jobs")
    src.last_ok = time.time()
    db.backup(conn, settings.backup_dir, "daily")
    restore_check(conn, settings)
    r = integrity.report(conn, settings, [src])
    assert r["issues"] == 0 and r["storage"]["db"] > 0


def test_garmin_key_expiry_warns_and_resets_with_a_new_key(conn, monkeypatch):
    set_key(monkeypatch, KEY)
    assert integrity._garmin_key(conn)["issue"] is None  # not entered: no warning
    kid = integrity.garmin.key_id(KEY)
    soon = integrity.KeyExpiry(key_id=kid, expires=date.today() + timedelta(days=10))
    kv.put(conn, "core_kv", integrity.KEY_EXPIRY_KEY, soon)
    assert integrity._garmin_key(conn)["issue"] == "Expires in 10 days"
    set_key(monkeypatch, KEY + "-new")
    assert integrity._garmin_key(conn)["expires"] is None


@pytest.mark.parametrize("value", ["2099-01-31", "", "not-a-date"])
def test_page_and_expiry_form(settings, monkeypatch, value):
    set_key(monkeypatch, KEY)
    with TestClient(create_app(settings, [Source()])) as tc:
        tc.post("/login", data={"password": PASSWORD})
        assert "data integrity issue" in tc.get("/").text  # "Thing" never synced
        r = tc.post("/integrity/garmin-key", data={"expires": value}, follow_redirects=False)
        assert r.status_code == 303
        page = tc.get("/integrity").text
        assert "Last successful sync" in page and "Thing" in page
        assert ("2099-01-31" in page) == (value == "2099-01-31")
        assert tc.post("/integrity/restore-check", follow_redirects=False).status_code == 303
