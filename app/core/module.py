"""The contract every feature module implements."""

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from fastapi import APIRouter

from app.core.jobs import Handler, Recurring


@dataclass
class Tile:
    """Homepage summary: a few short lines."""

    lines: list[str] = field(default_factory=list)
    error: bool = False


@dataclass(frozen=True)
class SyncStatus:
    """One data source's last successful sync, for the integrity page."""

    name: str
    last_ok: float | None
    note: str = ""  # why it isn't syncing (e.g. not connected); blank when it should be


class Module(ABC):
    """A feature: routes at /<slug>, `<slug>_*` tables, `<slug>.*` jobs, and a homepage tile."""

    slug: ClassVar[str]
    title: ClassVar[str]

    @abstractmethod
    def router(self) -> APIRouter:
        """Routes, mounted at /<slug>."""

    @abstractmethod
    def tile(self, conn: sqlite3.Connection) -> Tile:
        """Homepage summary."""

    def migrations_dir(self) -> Path | None:
        return None

    def templates_dir(self) -> Path | None:
        """Dir containing a `<slug>/` folder of templates."""
        return None

    def handlers(self) -> dict[str, Handler]:
        """Job handlers keyed by `<slug>.<name>`."""
        return {}

    def recurring(self) -> list[Recurring]:
        return []

    def syncs(self, conn: sqlite3.Connection) -> list[SyncStatus]:
        """Each external source this module syncs from."""
        return []
