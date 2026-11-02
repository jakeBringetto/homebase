"""Running settings: Strava credentials (env) and athlete settings (DB)."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ZONE_PCT_MAX = (0.6, 0.7, 0.8, 0.9)  # lower bounds of Z2..Z5 as % of max HR


class StravaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STRAVA_", env_file=".env", extra="ignore")

    client_id: str = ""
    client_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@lru_cache
def get_strava_settings() -> StravaSettings:
    return StravaSettings()


class AthleteSettings(BaseModel):
    """User-editable values that drive HR zones, TRIMP, and display units."""

    max_hr: int = Field(default=190, ge=120, le=230)
    resting_hr: int = Field(default=60, ge=30, le=100)
    zone_bounds: list[int] | None = None  # bpm lower bounds of Z2..Z5; None = % of max
    units: Literal["km", "mi"] = "km"

    @model_validator(mode="after")
    def _check(self) -> Self:
        b = self.zone_bounds
        if b is not None:
            if len(b) != 4 or b != sorted(set(b)):
                raise ValueError("zone bounds must be 4 increasing values")
            if not (self.resting_hr < b[0] and b[-1] < self.max_hr):
                raise ValueError("zone bounds must lie between resting and max HR")
        return self

    def zones(self) -> list[float]:
        """Lower bounds (bpm) of Z2..Z5."""
        if self.zone_bounds:
            return [float(x) for x in self.zone_bounds]
        return [round(self.max_hr * p) for p in ZONE_PCT_MAX]

    def affects_metrics(self, other: "AthleteSettings") -> bool:
        return (self.max_hr, self.resting_hr, self.zones()) != (
            other.max_hr,
            other.resting_hr,
            other.zones(),
        )
