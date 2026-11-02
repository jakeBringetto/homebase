"""Running: Strava sync, training analysis, and weekly plan."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter

from app.core.jobs import Handler, Recurring
from app.core.module import Module, Tile

HERE = Path(__file__).parent


class Running(Module):
    slug = "running"
    title = "Running"

    def router(self) -> APIRouter:
        from app.modules.running.routes import router

        return router

    def tile(self, conn: sqlite3.Connection) -> Tile:
        from datetime import date

        from app.modules.running import plan, reports, storage
        from app.modules.running.fmt import Fmt
        from app.modules.running.sources.strava import TOKEN_KEY, StravaToken

        f = Fmt(storage.athlete_settings(conn).units)
        today = date.today()
        week = reports.periods(conn, today, "week", today)[-1]
        lines = [f"This week: {f.dist(week['distance_m'])} · {week['runs']} runs"]
        wk = plan.week(conn, week["start"], today)
        if wk["planned"]:
            target = f" · {f.dist(wk['planned_m'])} planned" if wk["planned_m"] else ""
            lines.append(f"Plan: {wk['done']}/{wk['planned']} sessions{target}")
        if last := reports.last_run(conn):
            lines.append(
                f"Last: {f.day(last['start_local'])}, {f.dist(last['distance_m'])}"
                f" @ {f.pace(last['moving_s'], last['distance_m'])}"
            )
        if storage.kv_get(conn, TOKEN_KEY, StravaToken) is None:
            lines.append("Strava not connected")
        return Tile(lines)

    def migrations_dir(self) -> Path:
        return HERE / "migrations"

    def templates_dir(self) -> Path:
        return HERE / "templates"

    def handlers(self) -> dict[str, Handler]:
        from app.modules.running import sync

        return {
            "running.sync": sync.run_sync,
            "running.streams": sync.fetch_streams,
            "running.recompute": sync.recompute,
        }

    def recurring(self) -> list[Recurring]:
        return [Recurring("running.sync", 86400)]
