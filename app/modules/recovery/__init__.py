"""Recovery: Garmin overnight HRV, resting HR and sleep, against running's training load."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter

from app.core.jobs import Handler, Recurring
from app.core.module import Module, Tile

HERE = Path(__file__).parent
SYNC_EVERY_S = 3 * 3600  # cheap when fresh: the job skips once last night is stored


class Recovery(Module):
    slug = "recovery"
    title = "Recovery"

    def router(self) -> APIRouter:
        from app.modules.recovery.routes import router

        return router

    def tile(self, conn: sqlite3.Connection) -> Tile:
        from datetime import date

        from app.modules.recovery import config, reports

        if not config.get_garmin_settings().configured:
            return Tile(["Set GARMIN_API_KEY in .env"])
        latest = reports.latest(conn, date.today())
        lines = []
        if n := latest["night"]:
            band = f" (normal {n.low:.0f}–{n.high:.0f})" if n.low is not None else ""
            lines.append(f"HRV {n.hrv:.0f} ms{band} · {n.day.strftime('%b %-d')}")
        if r := latest["rhr"]:
            lines.append(f"Resting HR {r[1]:.0f} bpm · 7-day {r[2]:.0f}")
        if s := latest["sleep"]:
            score = f" · score {s['score']}" if s["score"] is not None else ""
            lines.append(f"Sleep {s['sleep_s'] / 3600:.1f} h{score}")
        return Tile(lines or ["No Garmin data yet"])

    def migrations_dir(self) -> Path:
        return HERE / "migrations"

    def templates_dir(self) -> Path:
        return HERE / "templates"

    def handlers(self) -> dict[str, Handler]:
        from app.modules.recovery import sync

        return {"recovery.sync": sync.run_sync}

    def recurring(self) -> list[Recurring]:
        return [Recurring("recovery.sync", SYNC_EVERY_S)]
