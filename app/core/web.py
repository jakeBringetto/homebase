"""Shared web helpers: templates, rendering, per-request DB connection."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core import db

APP_DIR = Path(__file__).parent.parent


def build_templates(module_dirs: list[Path]) -> Jinja2Templates:
    """Templates from app/templates plus each module's dir (modules nest under `<slug>/`)."""
    loader = jinja2.FileSystemLoader([APP_DIR / "templates", *module_dirs])
    env = jinja2.Environment(loader=loader, autoescape=True)
    env.filters["timestamp"] = format_timestamp
    return Jinja2Templates(env=env)


def format_timestamp(ts: float) -> str:
    """Unix seconds as 'YYYY-MM-DD HH:MM UTC'."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def render(request: Request, name: str, status: int = 200, **ctx):
    """Render a template with `request` in context."""
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a DB connection for the duration of one request."""
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
