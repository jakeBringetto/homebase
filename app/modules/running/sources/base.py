"""Data-source contract for activities and their second-by-second streams."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

RUN_SPORTS = {"Run", "TrailRun", "VirtualRun"}
# A "run" is a ride logged as a run (user rules) if it's faster than 4:00/mi over 800 m,
# or averages faster than 5:00/mi over 2+ mi.
RIDE_SPEED = 1609.344 / 240  # m/s
RIDE_MIN_M = 800
RIDE_LONG_SPEED = 1609.344 / 300
RIDE_LONG_M = 2 * 1609.344

# Channel name -> float32 array, all the same length as "time" (seconds from start).
Streams = dict[str, np.ndarray]


@dataclass(frozen=True)
class Activity:
    source: str
    source_id: str
    sport: str
    name: str
    start_time: int  # unix seconds, UTC
    start_local: str  # 'YYYY-MM-DDTHH:MM:SS'
    moving_s: int
    elapsed_s: int
    distance_m: float
    elev_gain_m: float
    avg_hr: float | None
    max_hr: float | None
    is_manual: bool
    gear_id: str | None = None

    @property
    def ride_like(self) -> bool:
        """Logged as a run but averaged ride speed (streams can also flag it; see analysis)."""
        if self.sport not in RUN_SPORTS or self.moving_s <= 0:
            return False
        speed, d = self.distance_m / self.moving_s, self.distance_m
        return (d > RIDE_MIN_M and speed > RIDE_SPEED) or (
            d >= RIDE_LONG_M and speed > RIDE_LONG_SPEED
        )

    @property
    def is_run(self) -> bool:
        return self.sport in RUN_SPORTS and not self.ride_like


@dataclass(frozen=True)
class Gear:
    id: str
    name: str
    retired: bool


class RunningSource(ABC):
    name: str

    @abstractmethod
    def activities(
        self, *, after: int | None = None, before: int | None = None, page: int = 1
    ) -> list[Activity]:
        """One page of activities started after/before the given unix times (any order)."""

    @abstractmethod
    def streams(self, source_id: str) -> Streams | None:
        """Streams for one activity, or None if the source has none."""

    @abstractmethod
    def gear(self, gear_id: str) -> Gear | None:
        """Details for one piece of gear, or None if unavailable."""
