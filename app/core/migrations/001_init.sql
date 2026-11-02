CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    priority INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    dedupe_key TEXT,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX jobs_ready ON jobs (status, run_after);
-- At most one active job per dedupe key; finished jobs don't block re-enqueueing.
CREATE UNIQUE INDEX jobs_active_dedupe ON jobs (dedupe_key)
    WHERE status IN ('pending', 'running');

CREATE TABLE rate_limits (
    provider TEXT NOT NULL,
    window_s INTEGER NOT NULL,
    max_calls INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    resets_at REAL NOT NULL,
    PRIMARY KEY (provider, window_s)
);
