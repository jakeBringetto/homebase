# Homebase

A self-hosted, single-user dashboard app for personal data. It runs as one Docker container with a SQLite database, on a laptop or a Raspberry Pi. Built with FastAPI, server-rendered pages (Jinja and htmx) and uPlot charts.

Each area is a module with its own pages, tables and background jobs. Current modules:

- **Running** (Strava): syncs activities and second-by-second streams, then computes:
  - training load (TRIMP, fitness/fatigue/form), long-run spikes, monotony and strain
  - HR at pace, aerobic efficiency, cadence, best efforts (moving time) and a race estimate
  - a weekly plan matched against completed runs
- **Recovery** (Garmin): overnight HRV against its normal range, resting HR and sleep. These are compared with running load (next-morning response) and with how each run went (readiness).

This is one person's app, not a hosted service or a multi-user product.

## Run it yourself

Requires Docker. Strava and Garmin are both optional; each module shows a setup hint until it's configured.

1. **Strava**: create an API app at strava.com/settings/api (this requires a Strava subscription). Set the Authorization Callback Domain to `localhost`. You only need the Client ID and Client Secret.
2. **Garmin** (optional): create a Personal API key at connect.garmin.com/app/settings/personalApiToken. It's shown once and expires after 30–365 days.
3. **Configure and start:**
   ```
   cp .env.example .env      # fill in STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, GARMIN_API_KEY
   mkdir -p data             # on Linux/Pi it must be owned by uid 1000
   docker compose up -d --build
   ```
4. Open http://localhost:8000, go to Running → Sync and click Connect Strava. The first sync backfills your activity list; second-by-second history is a separate button on the same page. Garmin backfills the last year on its own.

**Login:** leave `HOMEBASE_PASSWORD_HASH` empty for local use. Before running it anywhere else, set a password (`scripts/dev.sh hash-password`) and `HOMEBASE_SECRET_KEY`. It's meant for a LAN or Tailscale, not the public internet.

**Data:** everything is in `data/homebase.db`. A nightly backup is written to `data/backups/`, and one is taken before every migration. Don't open the live database from the host while the container is running; query it with `docker compose exec`.

## Use it as a reference

If you're building your own integration or app, these are the useful parts:

| Topic | Where |
|---|---|
| Garmin Personal API: auth, endpoints, field quirks, history window, limits | `docs/garmin-api.md` |
| Strava API: OAuth scope, endpoints, real rate limits, sync strategy | `docs/strava-api.md` |
| Formulas and the research behind each analysis | `docs/analyses.md` |
| Architecture, storage, deployment, releases | `docs/system-design.md` |
| Analysis code (TRIMP, GAP, steady-state detection, best efforts) | `app/modules/running/analysis/` |
| Durable job queue, rate limiter, module contract | `app/core/jobs.py`, `app/core/ratelimit.py`, `app/core/module.py` |

**Agents:** read `AGENTS.md` first (principles and conventions). `context/` holds local design notes and task lists; it's gitignored because it contains personal data.

## Strava MCP: a simpler option for questions

If you only want to ask an AI assistant about your Strava data, Strava's MCP connector is less setup than this app. It doesn't need an API app, a client secret or a callback domain. In Claude Code:

```
claude mcp add --transport http strava-mcp https://mcp.strava.com/mcp
```

Limitations compared with the API this app uses:

- **Subscription:** it requires a Strava subscription.
- **Assistant only:** it works through an assistant (Claude or Gemini) for the account owner, not from a background service. There's no scheduled sync and no local copy of your data.
- **Read-only:** it can't upload, edit or delete anything.
- **Limited and unspecified:**
  - The rate limits exist but aren't published.
  - Strava doesn't document the response shapes, so they can change.
- **Not the same as the app:** answers may differ from Strava's own analytics, and you can't run your own analysis over a stored history.

Use the API, as Homebase does, when you want stored data, your own metrics, or other data sources joined in. Details: [Strava support article](https://support.strava.com/hc/en-us/articles/46190267796237).

## Development

```
scripts/dev.sh serve | test | lint | format    # uses local uv, or a Python 3.12 container
```

Releases and deployment: `docs/system-design.md`.
