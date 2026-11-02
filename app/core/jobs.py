"""Durable SQLite job queue and the single background worker that runs it.

Handlers are sync functions `(conn, payload) -> None`, named `<module>.<name>`, and must be
idempotent: a crash mid-job resets it to pending and it runs again. The worker commits the
handler's writes together with marking the job done.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core import db

log = logging.getLogger(__name__)

Handler = Callable[[sqlite3.Connection, dict], None]

MAX_BACKOFF_S = 6 * 3600


class RetryLater(Exception):
    """Raised by a handler to reschedule without counting a failed attempt."""

    def __init__(self, delay_s: float, reason: str = ""):
        super().__init__(reason or f"retry in {delay_s:.0f}s")
        self.delay_s = delay_s


@dataclass(frozen=True)
class Recurring:
    """A job re-enqueued `every_s` seconds after each run finishes."""

    kind: str
    every_s: float
    run_at_start: bool = True

    @property
    def dedupe_key(self) -> str:
        return f"recurring:{self.kind}"


def enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict | None = None,
    *,
    run_after: float | None = None,
    priority: int = 0,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> bool:
    """Add a job (caller commits); returns False if an active job has the same dedupe_key."""
    now = time.time()
    cur = conn.execute(
        "INSERT OR IGNORE INTO jobs (kind, payload, priority, run_after, max_attempts, dedupe_key,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            json.dumps(payload or {}),
            priority,
            run_after or now,
            max_attempts,
            dedupe_key,
            now,
            now,
        ),
    )
    return cur.rowcount == 1


def failed(conn: sqlite3.Connection, prefix: str = "", limit: int = 20) -> list[sqlite3.Row]:
    """Newest failed jobs (kept 30 days), optionally only kinds starting with `prefix`."""
    return conn.execute(
        "SELECT kind, attempts, last_error, updated_at FROM jobs"
        " WHERE status = 'failed' AND kind LIKE ? ORDER BY updated_at DESC LIMIT ?",
        (f"{prefix}%", limit),
    ).fetchall()


def backoff_s(attempts: int) -> float:
    """Exponential backoff from 1 min, capped at 6 h."""
    return min(60 * 2 ** (attempts - 1), MAX_BACKOFF_S)


class Worker:
    """Runs due jobs one at a time, highest priority first."""

    def __init__(
        self,
        db_path: Path,
        handlers: dict[str, Handler],
        recurring: list[Recurring],
        idle_s: float = 5.0,
    ):
        self.db_path = db_path
        self.handlers = handlers
        self.recurring = {r.kind: r for r in recurring}
        self.idle_s = idle_s
        self.conn: sqlite3.Connection | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Open the connection, recover jobs interrupted by a crash, seed recurring jobs."""
        self.conn = db.connect(self.db_path)
        reset = self.conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (time.time(),),
        ).rowcount
        if reset:
            log.warning("reset %d interrupted job(s) to pending", reset)
        now = time.time()
        for r in self.recurring.values():
            first = now if r.run_at_start else now + r.every_s
            enqueue(self.conn, r.kind, run_after=first, dedupe_key=r.dedupe_key)
        self.conn.commit()

    def run_once(self) -> bool:
        """Claim and run one due job; returns False if none was due."""
        now = time.time()
        job = self.conn.execute(
            "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ("
            " SELECT id FROM jobs WHERE status = 'pending' AND run_after <= ?"
            " ORDER BY priority DESC, run_after LIMIT 1) RETURNING *",
            (now, now),
        ).fetchone()
        self.conn.commit()
        if job is None:
            return False
        self._execute(job)
        return True

    def _execute(self, job: sqlite3.Row) -> None:
        kind = job["kind"]
        try:
            handler = self.handlers.get(kind)
            if handler is None:
                raise LookupError(f"no handler registered for {kind!r}")
            handler(self.conn, json.loads(job["payload"]))
        except RetryLater as e:
            self.conn.rollback()
            self._update(job, "pending", job["attempts"], time.time() + e.delay_s, str(e))
            log.info("job %s #%d deferred: %s", kind, job["id"], e)
            return
        except Exception as e:
            self.conn.rollback()
            attempts = job["attempts"] + 1
            failed = attempts >= job["max_attempts"]
            log.exception("job %s #%d failed (attempt %d)", kind, job["id"], attempts)
            self._update(
                job,
                "failed" if failed else "pending",
                attempts,
                time.time() + backoff_s(attempts),
                f"{type(e).__name__}: {e}",
            )
            if failed:
                self._reschedule(job)
            return
        self._update(job, "done", job["attempts"] + 1, job["run_after"], None)
        self._reschedule(job)

    def _update(self, job, status: str, attempts: int, run_after: float, error: str | None):
        self.conn.execute(
            "UPDATE jobs SET status = ?, attempts = ?, run_after = ?, last_error = ?,"
            " updated_at = ? WHERE id = ?",
            (status, attempts, run_after, error, time.time(), job["id"]),
        )
        self.conn.commit()

    def _reschedule(self, job: sqlite3.Row) -> None:
        r = self.recurring.get(job["kind"])
        if r and job["dedupe_key"] == r.dedupe_key:
            enqueue(self.conn, r.kind, run_after=time.time() + r.every_s, dedupe_key=r.dedupe_key)
            self.conn.commit()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Loop until stopped, sleeping `idle_s` when no job is due."""
        await asyncio.to_thread(self.start)
        log.info("worker started with %d handler(s)", len(self.handlers))
        try:
            while not self._stop.is_set():
                try:
                    ran = await asyncio.to_thread(self.run_once)
                except sqlite3.Error:
                    log.exception("worker DB error; backing off")
                    ran = False
                if not ran:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.idle_s)
        finally:
            self.conn.close()
