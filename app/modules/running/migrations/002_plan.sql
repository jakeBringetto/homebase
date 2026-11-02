-- Planned sessions; matched to activities on the same local day at read time.
CREATE TABLE running_plan (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,                    -- 'YYYY-MM-DD', athlete-local
    kind TEXT NOT NULL,
    distance_m REAL,
    duration_s INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX running_plan_day ON running_plan (day);
