"""Homepage, health check, and core maintenance job."""

import logging
import sqlite3
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app import __version__
from app.core import db, integrity
from app.core.config import Settings
from app.core.jobs import Handler, enqueue
from app.core.module import Module, Tile
from app.core.web import get_conn, render

log = logging.getLogger(__name__)

router = APIRouter()


def safe_tile(module: Module, conn: sqlite3.Connection) -> Tile:
    """A module's tile, or an error tile if it raises, so one module can't break the homepage."""
    try:
        return module.tile(conn)
    except Exception:
        log.exception("tile failed for %s", module.slug)
        return Tile(["Unavailable"], error=True)


@router.get("/")
def home(request: Request, conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
    app = request.app.state
    tiles = [(m, safe_tile(m, conn)) for m in app.modules]
    issues = integrity.report(conn, app.settings, app.modules)["issues"]
    return render(request, "home.html", tiles=tiles, issues=issues)


@router.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


def maintenance(settings: Settings) -> Handler:
    """Daily: prune old finished jobs, back up the DB, then queue a test restore of it."""

    def run(conn: sqlite3.Connection, payload: dict) -> None:
        now = time.time()
        conn.execute(
            "DELETE FROM jobs WHERE (status = 'done' AND updated_at < ?)"
            " OR (status = 'failed' AND updated_at < ?)",
            (now - 7 * 86400, now - 30 * 86400),
        )
        db.backup(conn, settings.backup_dir, "daily", keep=settings.backup_keep)
        enqueue(conn, "core.restore_check", dedupe_key="core.restore_check")

    return run
