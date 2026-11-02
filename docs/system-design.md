# System design

## Goals

Single user, self-hosted, low resource. A minimal homepage of module tiles; each module owns its own pages, data, and background jobs.

## Architecture

One process, one container:

```
browser ──HTTP──> FastAPI (Jinja + htmx pages, small JSON endpoints for charts)
                     │
                     ├── core: config, db, migrations, auth, job worker, module registry, rate limiter
                     └── modules/<name>: routes, templates, migrations, jobs, sources, analysis
                     │
                  SQLite file (/data/homebase.db, WAL)
```

- **Module contract** (`app/core/module.py`, ABC): `slug`, `title`, `router`, `migrations_dir`, `jobs()` (recurring jobs to schedule), `tile()` (homepage summary). Modules register in `app/main.py`.
- **Job worker**: an asyncio loop in the app process. Jobs are rows in a `jobs` table (`kind`, `payload`, `run_after`, `attempts`, `status`). Crash-safe: on startup, `running` jobs reset to `pending`. Retries use backoff. Recurring jobs are re-enqueued after completion.
- **Rate limiter** (`app/core/ratelimit.py`): per-provider budgets persisted in the DB and updated from response headers. Jobs that would exceed budget are rescheduled, not dropped.
- **Cache**: `functools.lru_cache` for hot lookups for now (e.g. settings). Always rebuildable from the DB; add a TTL cache only when something needs invalidation.

## Storage

- SQLite, stdlib `sqlite3`, WAL mode, `foreign_keys=ON`.
- Each module prefixes its tables (`running_activities`, ...). Core tables have no prefix.
- Migrations: `migrations/NNN_name.sql` per module + core; a `schema_migrations` table tracks applied files.
- Large arrays (activity streams) are stored as compressed blobs; derived metrics are stored in normal columns for querying.

## Security

- Single-user password login (hashed in `.env`), signed session cookie. Login is disabled while `HOMEBASE_PASSWORD_HASH` is unset (current dev setup); set it before deploying to the Pi.
- Intended for LAN or Tailscale only; don't expose it publicly.
- OAuth and API access tokens are stored in the DB; client secrets and API keys (e.g. `GARMIN_API_KEY`) live in `.env`.

## Resource budget

- Idle RSS under 150MB. No pandas in the request path. Analysis runs in jobs on ingest, and pages read precomputed tables.
- Charts get downsampled server-side to at most ~2k points per series.

## Deployment

- `Dockerfile`: python:3.12-slim, `uv` install, non-root user, `/data` volume.
- `compose.yaml`: one service, `.env`, `./data:/data`, `restart: unless-stopped`.
- Laptop and Pi run the same image; the Pi uses 64-bit Raspberry Pi OS.
- Backup: nightly job runs `VACUUM INTO /data/backups/homebase-YYYYMMDD.db`, keeping the last 7.

## Release strategy

- `main` is always deployable. Work on short-lived branches and merge when tests pass.
- Semver in `pyproject.toml`. Release = bump version, update `CHANGELOG.md`, tag `vX.Y.Z`.
- Build multi-arch: `docker buildx build --platform linux/amd64,linux/arm64 -t homebase:X.Y.Z .` (registry TBD; GHCR likely).
- Deploy: pull the tag on the target and run `docker compose up -d`. Migrations run on startup; a DB backup is taken before applying new migrations.
- Rollback: redeploy the previous tag and restore the pre-migration backup if the schema changed.
