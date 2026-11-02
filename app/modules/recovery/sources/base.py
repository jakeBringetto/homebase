"""Data-source contract for daily wellness records (one per local day)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Daily:
    day: str
    resting_hr: float | None
    stress_avg: float | None
    stress_max: float | None
    bb_min: float | None
    bb_max: float | None
    bb_charged: float | None
    bb_drained: float | None
    resp_avg: float | None


@dataclass(frozen=True)
class Hrv:
    day: str
    last_night_avg: float | None
    last_night_5min_high: float | None
    weekly_avg: float | None
    status: str | None
    balanced_low: float | None
    balanced_upper: float | None
    low_upper: float | None


@dataclass(frozen=True)
class Sleep:
    day: str
    start_utc: int | None
    end_utc: int | None
    sleep_s: int
    deep_s: int | None
    light_s: int | None
    rem_s: int | None
    awake_s: int | None
    awake_count: int | None
    avg_hr: float | None
    avg_stress: float | None
    resp_avg: float | None
    score: int | None
    scores_json: str | None


class RecoverySource(ABC):
    name: str

    @abstractmethod
    def daily(self, start: date, end: date) -> list[Daily]:
        """Daily summaries for local days start..end inclusive (days without data omitted)."""

    @abstractmethod
    def hrv(self, start: date, end: date) -> list[Hrv]:
        """Nightly HRV for start..end inclusive."""

    @abstractmethod
    def sleep(self, start: date, end: date) -> list[Sleep]:
        """Sleep for start..end inclusive; nights without sleep omitted."""
