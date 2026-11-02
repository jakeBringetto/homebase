-- Rides logged as runs: set from summaries (avg pace) on upsert and from streams (any
-- 800 m faster than 4:00/mi) in analysis. Flagged activities have is_run = 0.
ALTER TABLE running_activities ADD COLUMN ride_like INTEGER NOT NULL DEFAULT 0;
UPDATE running_activities SET ride_like = 1, is_run = 0
    WHERE is_run AND distance_m > 800 AND moving_s > 0 AND distance_m / moving_s > 1609.344 / 240.0;

ALTER TABLE running_metrics ADD COLUMN gap_s_km REAL;       -- grade-adjusted pace
ALTER TABLE running_metrics ADD COLUMN cadence_spm REAL;    -- steps per minute while moving
ALTER TABLE running_pace_bins ADD COLUMN avg_cadence REAL;  -- spm

-- Fastest elapsed time over standard distances, per activity.
CREATE TABLE running_best (
    activity_id INTEGER NOT NULL REFERENCES running_activities (id) ON DELETE CASCADE,
    distance_m REAL NOT NULL,
    seconds REAL NOT NULL,
    PRIMARY KEY (activity_id, distance_m)
);
CREATE INDEX running_best_distance ON running_best (distance_m, seconds);
