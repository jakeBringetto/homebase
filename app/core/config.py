"""App settings loaded from environment / .env."""

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core settings; modules define their own Settings classes with their own prefix."""

    model_config = SettingsConfigDict(
        env_prefix="HOMEBASE_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    data_dir: Path = Path("data")
    # Unset: random per process, so sessions end on restart.
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48), min_length=32)
    password_hash: str = Field(default="", pattern=r"^(scrypt:.+)?$")  # empty disables login
    session_days: int = Field(default=30, ge=1)
    backup_keep: int = Field(default=7, ge=1)
    log_level: str = "INFO"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.password_hash)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "homebase.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_settings() -> Settings:
    """Load settings once; raises a validation error naming any missing values."""
    return Settings()
