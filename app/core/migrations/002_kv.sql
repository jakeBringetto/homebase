-- Small JSON documents shared across modules (e.g. the Garmin token, used by recovery and running).
CREATE TABLE core_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
