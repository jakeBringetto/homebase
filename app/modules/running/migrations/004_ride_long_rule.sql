-- Second ride rule (user, 2026-11-01): averaging faster than 5:00/mi over 2+ mi.
-- New activities get it on upsert (Activity.ride_like); this applies it to stored ones.
UPDATE running_activities SET ride_like = 1, is_run = 0
    WHERE is_run AND distance_m >= 2 * 1609.344 AND moving_s > 0
    AND distance_m / moving_s > 1609.344 / 300.0;
