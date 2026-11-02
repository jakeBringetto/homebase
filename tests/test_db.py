import sqlite3

import pytest

from app.core import db


def test_migrate_applies_once(settings, tmp_path):
    src = tmp_path / "mig"
    src.mkdir()
    (src / "001_a.sql").write_text("CREATE TABLE a (x);")
    conn = db.connect(settings.db_path)
    assert db.migrate(conn, {"m": src}, settings.backup_dir) == ["m/001_a.sql"]
    assert db.migrate(conn, {"m": src}, settings.backup_dir) == []
    assert not settings.backup_dir.exists()  # fresh DB: nothing to back up


def test_migrate_backs_up_then_rolls_back_failure(settings, tmp_path):
    src = tmp_path / "mig"
    src.mkdir()
    (src / "001_a.sql").write_text("CREATE TABLE a (x);")
    conn = db.connect(settings.db_path)
    db.migrate(conn, {"m": src}, settings.backup_dir)
    (src / "002_bad.sql").write_text("CREATE TABLE b (x); INSERT INTO nope VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, {"m": src}, settings.backup_dir)
    assert len(list(settings.backup_dir.glob("homebase-premigrate-*.db"))) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "b" not in tables
    applied = [r[0] for r in conn.execute("SELECT id FROM schema_migrations")]
    assert applied == ["m/001_a.sql"]


def test_backup_prunes(conn, settings):
    for _ in range(4):
        db.backup(conn, settings.backup_dir, "daily", keep=2)
    assert len(list(settings.backup_dir.glob("homebase-daily-*.db"))) == 2
