"""Small JSON documents in a `<slug>_kv` table, or `core_kv` (key TEXT PK, value TEXT)."""

import sqlite3

from pydantic import BaseModel


def get[M: BaseModel](conn: sqlite3.Connection, table: str, key: str, model: type[M]) -> M | None:
    row = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
    return model.model_validate_json(row[0]) if row else None


def put(conn: sqlite3.Connection, table: str, key: str, value: BaseModel) -> None:
    """Insert or replace (caller commits)."""
    conn.execute(
        f"INSERT INTO {table} (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value.model_dump_json()),
    )


def delete(conn: sqlite3.Connection, table: str, key: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
