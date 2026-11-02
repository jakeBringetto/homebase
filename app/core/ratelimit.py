"""Per-provider API call budgets, persisted in SQLite so restarts don't forget usage."""

import logging
import sqlite3
import time
from dataclasses import dataclass

from app.core.jobs import RetryLater

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Window:
    """Budget over an epoch-aligned period (900 = quarter hours, 86400 = UTC days)."""

    seconds: int
    max_calls: int


def next_reset(seconds: int, now: float) -> float:
    return (now // seconds + 1) * seconds


class RateLimiter:
    """Reserve calls before making them; sync with provider-reported usage after."""

    def __init__(self, provider: str, windows: list[Window], reserve: int = 0):
        self.provider = provider
        self.windows = windows
        self.reserve = reserve

    def _rows(self, conn: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
        for w in self.windows:
            conn.execute(
                "INSERT OR IGNORE INTO rate_limits (provider, window_s, max_calls, used, resets_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (self.provider, w.seconds, w.max_calls, next_reset(w.seconds, now)),
            )
            conn.execute(
                "UPDATE rate_limits SET used = 0, resets_at = ?"
                " WHERE provider = ? AND window_s = ? AND resets_at <= ?",
                (next_reset(w.seconds, now), self.provider, w.seconds, now),
            )
        return conn.execute(
            "SELECT * FROM rate_limits WHERE provider = ?", (self.provider,)
        ).fetchall()

    def acquire(self, conn: sqlite3.Connection, cost: int = 1, now: float | None = None) -> None:
        """Reserve `cost` calls in every window (commits), or raise RetryLater until one frees."""
        now = now or time.time()
        rows = self._rows(conn, now)
        blocked = [r for r in rows if r["used"] + cost > r["max_calls"] - self.reserve]
        if blocked:
            conn.commit()
            wait = max(r["resets_at"] for r in blocked) - now + 1
            raise RetryLater(wait, f"{self.provider} rate limit; retry in {wait:.0f}s")
        conn.execute(
            "UPDATE rate_limits SET used = used + ? WHERE provider = ?", (cost, self.provider)
        )
        conn.commit()

    def release(self, conn: sqlite3.Connection, cost: int = 1) -> None:
        """Return calls the provider didn't count (commits)."""
        conn.execute(
            "UPDATE rate_limits SET used = max(used - ?, 0) WHERE provider = ?",
            (cost, self.provider),
        )
        conn.commit()

    def observe(
        self, conn: sqlite3.Connection, usage: dict[int, tuple[int, int]], now: float | None = None
    ) -> None:
        """Overwrite budgets with provider-reported `{window_s: (limit, used)}` (commits)."""
        now = now or time.time()
        self._rows(conn, now)
        for seconds, (limit, used) in usage.items():
            conn.execute(
                "UPDATE rate_limits SET max_calls = ?, used = ?"
                " WHERE provider = ? AND window_s = ?",
                (limit, used, self.provider, seconds),
            )
        conn.commit()

    def status(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """Current usage rows, for status pages."""
        return self._rows(conn, time.time())
