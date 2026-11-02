-- Training phases (blocks of weeks) and per-week targets for the Plan tab.
CREATE TABLE running_plan_phases (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    start_day TEXT NOT NULL,              -- 'YYYY-MM-DD', first day
    end_day TEXT NOT NULL,                -- 'YYYY-MM-DD', last day (inclusive)
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX running_plan_phases_start ON running_plan_phases (start_day);

CREATE TABLE running_plan_targets (
    week TEXT PRIMARY KEY,                -- Monday, 'YYYY-MM-DD'
    distance_m REAL,
    long_m REAL,
    quality INTEGER,                      -- tempo/interval sessions
    notes TEXT NOT NULL DEFAULT ''
);
