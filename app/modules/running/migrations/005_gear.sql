-- Shoes: the gear on each activity, and gear details fetched once per gear id.
-- Stored activities get gear_id when sync re-pages the list (SyncState.list_version).
ALTER TABLE running_activities ADD COLUMN gear_id TEXT;

CREATE TABLE running_gear (
    id TEXT PRIMARY KEY,           -- Strava gear id ("g..." shoes, "b..." bikes)
    name TEXT,                     -- NULL if the details couldn't be fetched
    retired INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);
