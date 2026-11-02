-- VO2max dropped (user, 2026-11-01): the Personal API returns it null on every run.
ALTER TABLE running_garmin DROP COLUMN vo2max;
