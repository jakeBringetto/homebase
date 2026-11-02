"""Data integrity page: last syncs, backups and test restores, failed jobs, DB size, key expiry.

Daily maintenance backs up the DB, then `core.restore_check` restores the newest backup to a
scratch file and reads it back, so a broken backup shows up here rather than when it's needed.
"""

import logging
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core import db, garmin, jobs, kv
from app.core.config import Settings
from app.core.jobs import Handler
from app.core.module import Module, SyncStatus
from app.core.web import get_conn, render

log = logging.getLogger(__name__)

router = APIRouter()
Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

RESTORE_KEY = "restore_check"
KEY_EXPIRY_KEY = "garmin_key_expiry"
STALE_S = 2 * 86400  # syncs, backups and restore checks all run at least daily
KEY_WARN_DAYS = 14
MIN_FREE = 0.05  # share of the disk


class RestoreCheck(BaseModel):
    at: float
    backup: str
    ok: bool
    detail: str


class KeyExpiry(BaseModel):
    """Entered by hand: the Personal API doesn't report when a key expires."""

    key_id: str  # a new key in .env makes the stored date stale
    expires: date


def newest_backup(backup_dir: Path) -> Path | None:
    files = backup_dir.glob("homebase-*.db") if backup_dir.exists() else []
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def restore_check(settings: Settings) -> Handler:
    """Restore the newest backup to a scratch file, read it back, and record the result."""

    def run(conn: sqlite3.Connection, payload: dict) -> None:
        backup = newest_backup(settings.backup_dir)
        if backup is None:
            return
        live = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
        try:
            r = db.verify_backup(backup, settings.data_dir / "restore-check.db")
            detail = f"{r.tables} tables, {r.rows:,} rows"
            if behind := len(live - r.migrations):
                detail += f"; {behind} migration(s) behind, applied on start after a restore"
            check = RestoreCheck(at=time.time(), backup=backup.name, ok=True, detail=detail)
        except (sqlite3.Error, OSError) as e:
            log.error("restore check of %s failed: %s", backup.name, e)
            check = RestoreCheck(at=time.time(), backup=backup.name, ok=False, detail=str(e))
        kv.put(conn, "core_kv", RESTORE_KEY, check)

    return run


def _age_issue(ts: float | None, never: str) -> str | None:
    if ts is None:
        return never
    return "more than 2 days ago" if time.time() - ts > STALE_S else None


def _syncs(conn: sqlite3.Connection, modules: list[Module]) -> list[dict]:
    rows = []
    for m in modules:
        try:
            statuses = m.syncs(conn)
        except Exception:
            log.exception("sync status failed for %s", m.slug)
            statuses = [SyncStatus(m.title, None, "Status unavailable; see logs")]
        for s in statuses:
            issue = s.note or _age_issue(s.last_ok, "Never synced")
            rows.append({"name": s.name, "last_ok": s.last_ok, "issue": issue})
    return rows


def _backups(settings: Settings) -> dict:
    files = list(settings.backup_dir.glob("homebase-*.db")) if settings.backup_dir.exists() else []
    newest = newest_backup(settings.backup_dir)
    at = newest.stat().st_mtime if newest else None
    return {
        "name": newest.name if newest else None,
        "at": at,
        "count": len(files),
        "size": sum(f.stat().st_size for f in files),
        "issue": _age_issue(at, "No backup yet"),
    }


def _restore(conn: sqlite3.Connection) -> dict:
    check = kv.get(conn, "core_kv", RESTORE_KEY, RestoreCheck)
    last_ok = check.at if check and check.ok else None
    issue = _age_issue(last_ok, "No verified restore yet")
    if check and not check.ok:
        issue = f"Last check failed: {check.detail}"
    return {"check": check, "issue": issue}


def _storage(settings: Settings) -> dict:
    files = [settings.db_path, settings.db_path.with_name(settings.db_path.name + "-wal")]
    disk = shutil.disk_usage(settings.data_dir)
    low = disk.free < disk.total * MIN_FREE
    return {
        "db": sum(f.stat().st_size for f in files if f.exists()),
        "free": disk.free,
        "total": disk.total,
        "issue": f"Under {MIN_FREE:.0%} of the disk is free" if low else None,
    }


def _garmin_key(conn: sqlite3.Connection) -> dict:
    settings = garmin.get_settings()
    if not settings.configured:
        return {"configured": False, "issue": None}
    kid = garmin.key_id(settings.api_key)
    auth = garmin.auth_state(conn, settings)
    stored = kv.get(conn, "core_kv", KEY_EXPIRY_KEY, KeyExpiry)
    expires = stored.expires if stored and stored.key_id == kid else None
    days = (expires - date.today()).days if expires else None
    issue = None
    if auth.invalid:
        issue = f"Rejected: {auth.invalid}"
    elif days is not None and days < 0:
        issue = "Expired"
    elif days is not None and days <= KEY_WARN_DAYS:
        issue = f"Expires in {days} day{'s' if days != 1 else ''}"
    return {"configured": True, "expires": expires, "days": days, "issue": issue}


def report(conn: sqlite3.Connection, settings: Settings, modules: list[Module]) -> dict:
    failed = jobs.failed(conn)
    r = {
        "syncs": _syncs(conn, modules),
        "backup": _backups(settings),
        "restore": _restore(conn),
        "storage": _storage(settings),
        "failed": failed,
        "garmin_key": _garmin_key(conn),
    }
    singles = [r["backup"], r["restore"], r["storage"], r["garmin_key"]]
    r["issues"] = (
        sum(bool(s["issue"]) for s in r["syncs"])
        + sum(bool(s["issue"]) for s in singles)
        + len(failed)
    )
    return r


@router.get("/integrity")
def integrity_page(request: Request, conn: Conn):
    app = request.app.state
    return render(request, "integrity.html", **report(conn, app.settings, app.modules))


@router.post("/integrity/restore-check")
def queue_restore_check(conn: Conn):
    jobs.enqueue(conn, "core.restore_check", priority=1, dedupe_key="core.restore_check")
    conn.commit()
    return RedirectResponse("/integrity", 303)


@router.post("/integrity/garmin-key")
def set_garmin_key_expiry(conn: Conn, expires: Annotated[str, Form()] = ""):
    """Store the key's expiry date (from Garmin's key page); a blank date clears it."""
    settings = garmin.get_settings()
    try:
        day = date.fromisoformat(expires) if expires else None
    except ValueError:
        return RedirectResponse("/integrity", 303)
    if day is None:
        kv.delete(conn, "core_kv", KEY_EXPIRY_KEY)
    elif settings.configured:
        kid = garmin.key_id(settings.api_key)
        kv.put(conn, "core_kv", KEY_EXPIRY_KEY, KeyExpiry(key_id=kid, expires=day))
    conn.commit()
    return RedirectResponse("/integrity", 303)
