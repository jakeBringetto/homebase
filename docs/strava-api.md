# Strava API

Notes from building the `running` module.

## App and OAuth

- **App:** create one at strava.com/settings/api (this requires a Strava subscription). New apps run in single-player mode: only the owner's account can authorize.
- **Callback:** set the Authorization Callback Domain to `localhost`. The field takes a bare domain (no scheme, port or path), and Strava matches only the redirect_uri host, so `http://localhost:8000/running/strava/callback` is valid. On another host, use an SSH tunnel or set that hostname as the callback domain.
- **Scope:** `activity:read_all`. Authorize once, store the refresh token, and refresh the access token when it expires. Env vars: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`.
- **Don't use the tokens shown on the settings page:** they lack activity scope.

## Endpoints

- `GET /athlete/activities?after=&per_page=200`: activity list, 1 call per 200 activities.
- `GET /activities/{id}/streams?keys=time,distance,heartrate,velocity_smooth,cadence,altitude,grade_smooth&key_by_type=true`: streams, 1 call per activity.
- `GET /gear/{id}`: gear name, brand/model, `retired`, Strava's own total distance. Works with `activity:read_all` (returned 200, 2026-11-01). The activity list carries each activity's `gear_id` (`g…` shoes, `b…` bikes).
- `POST /oauth/token`: token refresh.

## Rate limits

- **Overall:** 200 per 15 min / 2,000 per day. **Read:** 100 / 1,000. Every call here is a read, so 100 / 1,000 is the real budget (seen in response headers, 2026-11-01).
- **Headers:** `X-RateLimit-*` and `X-ReadRateLimit-*` give limit and usage for both windows. `sources/strava.py:parse_usage()` takes the tighter one and feeds the shared limiter.
- **Streams responses carry no usage headers and weren't counted** against the budget (2026-11-01). Don't rely on this staying true.
- **429:** wait for the next 15-minute window. Windows are aligned to the quarter hour; daily windows reset at midnight UTC.

## Sync strategy

- **Backfill:** page the activity list newest-first with `before=`. Summaries only.
- **History streams:** opt-in, queued at low priority for runs without streams.
- **Ongoing:** poll daily with `after=<newest stored start time>`. New runs get one streams fetch. No webhooks, since they need a public URL.
- **Never refetch:** streams are stored once, gear details once per gear id. Name, type and gear edits are picked up from list pages.
- **New summary fields:** bump `sync.LIST_VERSION`; the next sync re-pages the whole list once (about 1 call per 200 activities) so stored rows get the field.

## Data quirks

- **Cadence** is per-foot rpm; double it for steps per minute.
- **Manual activities** have no streams.
- **Rides logged as runs:** this app treats a run as a ride if it's faster than 4:00/mi over 800 m, or averages faster than 5:00/mi over 2+ mi.
