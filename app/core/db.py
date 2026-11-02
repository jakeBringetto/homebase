"""SQLite connections, migrations, and backups."""

import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Restored:
    tables: int
    rows: int
    migrations: frozenset[str]


def verify_backup(backup: Path, scratch: Path) -> Restored:
    """Restore a backup to a scratch file and read all of it back; raises if it's damaged."""
    shutil.copyfile(backup, scratch)
    try:
        conn = sqlite3.connect(scratch)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise sqlite3.DatabaseError(f"integrity check: {result}")
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%'"
                )
            ]
            rows = sum(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables)
            migrations = frozenset(r[0] for r in conn.execute("SELECT id FROM schema_migrations"))
        finally:
            conn.close()
    finally:
        scratch.unlink(missing_ok=True)
    return Restored(len(tables), rows, migrations)
