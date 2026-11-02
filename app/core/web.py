"""Shared web helpers: templates, rendering, per-request DB connection."""

import sqlite3
import time
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
    env.filters["ago"] = format_ago
    env.filters["filesize"] = format_size
    return Jinja2Templates(env=env)


def format_timestamp(ts: float) -> str:
    """Unix seconds as 'YYYY-MM-DD HH:MM UTC'."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def format_ago(ts: float) -> str:
    """Unix seconds as a rough age: 'just now', '5 min ago', '3 h ago', '2 days ago'."""
    s = time.time() - ts
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60:.0f} min ago"
    if s < 2 * 86400:
        return f"{s // 3600:.0f} h ago"
    return f"{s // 86400:.0f} days ago"


def format_size(n: float) -> str:
    """Bytes as '812 KB', '5.2 MB' or '1.4 GB' (1000-based)."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n / 1e3:.0f} KB"


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
