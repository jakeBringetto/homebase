-- Local clock offset of each night (local minus UTC, seconds), for bedtime and wake time.
-- NULL on rows fetched before this; sync re-fetches sleep once (sync.SLEEP_VERSION).
ALTER TABLE recovery_sleep ADD COLUMN utc_offset_s INTEGER;
