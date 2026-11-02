-- Garmin activity summaries for running form (watch-recorded runs only).
-- Joined to running_activities at read time on start time and distance (reports.form_runs).
CREATE TABLE running_garmin (
    garmin_id TEXT PRIMARY KEY,
    start_time INTEGER NOT NULL,   -- unix seconds, UTC (beginTimestamp / 1000)
    distance_m REAL,
    avg_power_w REAL,
    gct_ms REAL,                   -- ground contact time
    vert_osc_cm REAL,              -- vertical oscillation
    vert_ratio REAL,               -- oscillation / stride, %
    stride_cm REAL,
    cadence_spm REAL,
    vo2max REAL,                   -- documented, but null for every run seen so far
    fetched_at REAL NOT NULL
);
CREATE INDEX running_garmin_start ON running_garmin (start_time);
