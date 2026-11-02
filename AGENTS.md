# AGENTS.md

Homebase: a single-user personal app of modular dashboards (running first). Runs in Docker on a laptop or a Raspberry Pi (assume 4GB RAM, arm64).

**Before working, read `context/README.md`**. It explains how to find component overviews and task lists. Update the relevant `tasks.md` when you finish or discover work.

- `docs/` is tracked and public: system design, API references, analysis methods. Keep personal data out of it (health details, results, dates of specific runs, real measurements).
- `context/` is gitignored and local: notes that may include the user's own data. Code comments, tests and commit messages follow the `docs/` rule.

## Principles

1. **Extensible, not overengineered.** Use ABCs for real seams (modules, data sources), not for single implementations. Validate inputs at boundaries (Pydantic for requests and config, explicit checks on external API data). Handle errors gracefully: log, degrade, keep serving.
2. **Survive a crash.** Durable state lives in SQLite (WAL mode), including the job queue and sync cursors. In-memory caches are for hot lookups only and must be rebuildable from the DB. Every job must be idempotent and safe to re-run after a restart.
3. **Runs anywhere.** Everything runs in the container; no host-only dependencies. Images are multi-arch (amd64 + arm64). Follow the release strategy in `docs/system-design.md`.
4. **Compute and memory are first-class.** Target under 150MB RSS at idle. Stream rows from the DB rather than loading tables. Prefer numpy over pandas; precompute derived metrics on ingest instead of recomputing per page view. No new dependency without a clear payoff.
5. **One human audience.** Don't optimize query latency or throughput beyond "feels fine". Do respect external rate limits: fetch only what's needed, store it once, never refetch what we have.
6. **Succinct docs.** One-line docstrings saying what a function does. Explain "why" only when it isn't obvious. No restating the code.
7. **Lean code for agents.** Agents read this repo with limited context. Small focused files, no dead code, no speculative abstractions, no boilerplate comments. Names should make grep work.
8. **Reuse first.** Before writing something new, check `app/core/` and existing modules. If two modules need the same thing, move it to `core`.
9. **Minimal but readable UI.** Server-rendered Jinja + htmx, one shared stylesheet, system fonts. Clear labels, units, and axes on every number and chart. No decorative design.

## Stack

Python 3.12, FastAPI, Jinja2, htmx, uPlot (vendored in `static/`, no CDN), SQLite via stdlib `sqlite3`, httpx, Pydantic. `uv` for deps, `ruff` for lint/format, `pytest` for tests.

## Commands

`scripts/dev.sh` uses local `uv` if installed, otherwise runs it in a Python 3.12 container.

```
scripts/dev.sh format | lint | test [-k ...] | lock | serve | hash-password
docker compose up --build        # run the production image locally
scripts/release.sh X.Y.Z         # see Release strategy in docs/system-design.md
```

## Conventions

- Secrets come from `.env`; never commit them.
- **Never read `.env`** (or any env file with real values). If you need variable names, ask the user. `.env.example` has placeholders only and is safe to read.
- **Never open the live `data/homebase.db` from the host** while the container runs: WAL shared memory doesn't work across the Docker VM, and it breaks the app with `disk I/O error`. Query via `docker compose exec`, or copy the file and query the copy.
- Migrations are numbered plain SQL files, forward-only, applied at startup.
- External API clients live behind a source ABC and go through the shared rate limiter.
- Job handlers are sync `(conn, payload) -> None`, named `<slug>.<name>`; raise `RetryLater` to defer.
- Routes get a DB connection via `Depends(get_conn)` and render with `render(request, "<slug>/page.html", ...)`.
- Tests cover analysis math and API-parsing; mock external HTTP.
