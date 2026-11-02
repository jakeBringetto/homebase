-- Garmin wellness data, one row per local day. Recent days are re-fetched and upserted,
-- since Garmin fills them in whenever the watch syncs.

CREATE TABLE recovery_daily (
    day TEXT PRIMARY KEY,           -- YYYY-MM-DD (Garmin calendarDate)
    resting_hr REAL,                -- falls back to the day's minimum when not worn
    stress_avg REAL,                -- 0-100; NULL when Garmin reports -1
    stress_max REAL,
    bb_min REAL,                    -- Body Battery, 5-100
    bb_max REAL,
    bb_charged REAL,
    bb_drained REAL,
    resp_avg REAL,                  -- breaths/min
    fetched_at REAL NOT NULL
);

CREATE TABLE recovery_hrv (
    day TEXT PRIMARY KEY,           -- wake-up day
    last_night_avg REAL,            -- ms; NULL on nights not worn
    last_night_5min_high REAL,
    weekly_avg REAL,
    status TEXT,                    -- BALANCED, LOW, UNBALANCED, POOR, NONE
    balanced_low REAL,              -- Garmin's normal range; NULL until ~19 nights
    balanced_upper REAL,
    low_upper REAL,
    fetched_at REAL NOT NULL
);

CREATE TABLE recovery_sleep (
    day TEXT PRIMARY KEY,           -- wake-up day
    start_utc INTEGER,              -- unix seconds
    end_utc INTEGER,
    sleep_s INTEGER,
    deep_s INTEGER,
    light_s INTEGER,
    rem_s INTEGER,
    awake_s INTEGER,
    awake_count INTEGER,
    avg_hr REAL,
    avg_stress REAL,
    resp_avg REAL,
    score INTEGER,                  -- 0-100
    scores_json TEXT,               -- per-component sleepScores, kept raw
    fetched_at REAL NOT NULL
);

CREATE TABLE recovery_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
