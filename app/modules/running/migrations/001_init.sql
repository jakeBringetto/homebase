CREATE TABLE running_activities (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    start_time INTEGER NOT NULL,          -- unix seconds, UTC
    start_local TEXT NOT NULL,            -- 'YYYY-MM-DDTHH:MM:SS', athlete-local wall time
    moving_s INTEGER NOT NULL DEFAULT 0,
    elapsed_s INTEGER NOT NULL DEFAULT 0,
    distance_m REAL NOT NULL DEFAULT 0,
    elev_gain_m REAL NOT NULL DEFAULT 0,
    avg_hr REAL,
    max_hr REAL,
    is_run INTEGER NOT NULL,
    is_manual INTEGER NOT NULL DEFAULT 0,
    streams_status TEXT NOT NULL DEFAULT 'none'
        CHECK (streams_status IN ('none', 'queued', 'stored', 'unavailable')),
    updated_at REAL NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX running_activities_start ON running_activities (start_local);

-- np.savez_compressed of float32 channels; fetched once, never refetched.
CREATE TABLE running_streams (
    activity_id INTEGER PRIMARY KEY REFERENCES running_activities (id) ON DELETE CASCADE,
    data BLOB NOT NULL
);

CREATE TABLE running_metrics (
    activity_id INTEGER PRIMARY KEY REFERENCES running_activities (id) ON DELETE CASCADE,
    z1_s REAL NOT NULL DEFAULT 0,
    z2_s REAL NOT NULL DEFAULT 0,
    z3_s REAL NOT NULL DEFAULT 0,
    z4_s REAL NOT NULL DEFAULT 0,
    z5_s REAL NOT NULL DEFAULT 0,
    ef REAL,                              -- avg speed (m/min) / avg HR
    decoupling_pct REAL,                  -- EF drop, first vs second half; long runs only
    trimp REAL,
    steady_s REAL NOT NULL DEFAULT 0      -- seconds counted in pace bins
);

-- Steady-state time per 5 s/km pace band; feeds HR-at-pace trends.
CREATE TABLE running_pace_bins (
    activity_id INTEGER NOT NULL REFERENCES running_activities (id) ON DELETE CASCADE,
    pace_s INTEGER NOT NULL,              -- band floor, seconds per km
    seconds REAL NOT NULL,
    avg_hr REAL NOT NULL,
    PRIMARY KEY (activity_id, pace_s)
);

-- Small JSON documents: Strava token, sync cursors, athlete settings.
CREATE TABLE running_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
