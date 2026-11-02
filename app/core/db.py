"""SQLite connections, migrations, and backups."""

import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys, and Row results.

    check_same_thread is off because FastAPI may hand a request's connection across
    threadpool threads; each connection is still used by one request or the worker at a time.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection, sources: dict[str, Path], backup_dir: Path) -> list[str]:
    """Apply pending `NNN_*.sql` files from each source dir in order; returns applied ids."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (id TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
    pending = [
        (f"{name}/{f.name}", f)
        for name, d in sources.items()
        for f in sorted(d.glob("[0-9][0-9][0-9]_*.sql"))
        if f"{name}/{f.name}" not in done
    ]
    if not pending:
        return []
    if done:
        backup(conn, backup_dir, "premigrate")
    for mid, f in pending:
        log.info("applying migration %s", mid)
        try:
            conn.executescript(
                f"BEGIN;\n{f.read_text()}\n"
                f"INSERT INTO schema_migrations VALUES ('{mid}', {time.time()});\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            log.error("migration %s failed; rolled back", mid)
            raise
    return [mid for mid, _ in pending]


def backup(conn: sqlite3.Connection, backup_dir: Path, label: str, keep: int | None = None) -> Path:
    """Snapshot the DB with VACUUM INTO, then keep only the newest `keep` with this label."""
    conn.commit()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"homebase-{label}-{stamp}.db"
    conn.execute("VACUUM INTO ?", (str(dest),))
    if keep is not None:
        for old in sorted(backup_dir.glob(f"homebase-{label}-*.db"))[:-keep]:
            old.unlink()
    log.info("backup written: %s", dest.name)
    return dest
