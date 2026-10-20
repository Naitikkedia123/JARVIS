"""Typed, local-first application settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings read from the environment with safe defaults."""

    model_config = SettingsConfigDict(env_prefix="JARVIS_", case_sensitive=False)

    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="JARVIS_ENV"
    )
    host: Literal["127.0.0.1"] = "127.0.0.1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    device_database_path: Path = Path("data/local/devices.sqlite3")
    device_presence_freshness_seconds: int = Field(default=300, ge=1, le=86_400)
    owner_credential_lifetime_days: int = Field(default=30, ge=1, le=365)
    approval_lifetime_minutes: int = Field(default=5, ge=1, le=60)
    task_database_path: Path = Path("data/local/tasks.sqlite3")
    device_task_grants_path: Path = Path("data/local/device-task-grants.json")
    task_submission_grants_path: Path = Path("data/local/task-submission-grants.json")
    device_bootstrap_code: SecretStr | None = None
