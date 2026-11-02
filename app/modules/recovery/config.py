"""Recovery settings: the Garmin Personal API key (env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class GarminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GARMIN_", env_file=".env", extra="ignore")

    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@lru_cache
def get_garmin_settings() -> GarminSettings:
    return GarminSettings()
