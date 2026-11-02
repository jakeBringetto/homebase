"""App factory: wires core, modules, and the job worker.

Run with `uvicorn --factory app.main:create_app`.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.core import auth, db, home
from app.core.config import Settings, get_settings
from app.core.jobs import Recurring, Worker
from app.core.module import Module
from app.core.web import APP_DIR, build_templates
from app.modules.recovery import Recovery
from app.modules.running import Running

log = logging.getLogger(__name__)

MODULES: list[Module] = [Running(), Recovery()]  # register modules here


def create_app(settings: Settings | None = None, modules: list[Module] | None = None) -> FastAPI:
    settings = settings or get_settings()
    modules = MODULES if modules is None else modules
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    handlers = {"core.maintenance": home.maintenance(settings)}
    recurring = [Recurring("core.maintenance", 86400)]
    migrations = {"core": db.MIGRATIONS_DIR}
    for m in modules:
        bad = [k for k in m.handlers() if not k.startswith(f"{m.slug}.")]
        if bad:
            raise ValueError(f"module {m.slug!r} job kinds must start with '{m.slug}.': {bad}")
        handlers |= m.handlers()
        recurring += m.recurring()
        if d := m.migrations_dir():
            migrations[m.slug] = d

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn, migrations, settings.backup_dir)
        finally:
            conn.close()
        worker = Worker(settings.db_path, handlers, recurring)
        task = asyncio.create_task(worker.run())
        log.info("homebase %s started with modules: %s", __version__, [m.slug for m in modules])
        if not settings.auth_enabled:
            log.warning("HOMEBASE_PASSWORD_HASH unset: login disabled for everyone on the network")
        yield
        worker.stop()
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            log.warning("worker still busy at shutdown; interrupted job will rerun on start")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.modules = modules
    app.state.templates = build_templates([d for m in modules if (d := m.templates_dir())])
    app.state.templates.env.globals.update(
        nav=[(m.slug, m.title) for m in modules],
        version=__version__,
        auth_enabled=settings.auth_enabled,
    )

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(home.router)
    for m in modules:
        app.include_router(m.router(), prefix=f"/{m.slug}")

    # Last added runs first: the session must be loaded before the login check.
    app.middleware("http")(auth.require_login)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="homebase_session",
        max_age=settings.session_days * 86400,
        same_site="lax",
    )
    return app
