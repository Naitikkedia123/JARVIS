"""Typed, local-first application settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, IPvAnyAddress, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings read from the environment with safe defaults."""

    model_config = SettingsConfigDict(env_prefix="JARVIS_", case_sensitive=False)

    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="JARVIS_ENV"
    )
    host: Literal["127.0.0.1"] = "127.0.0.1"
    transport_mode: Literal["local", "wireguard"] = "local"
    wireguard_bind_address: IPvAnyAddress | None = None
    wireguard_interface_guid: UUID | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    device_database_path: Path = Path("data/local/devices.sqlite3")
    device_presence_freshness_seconds: int = Field(default=300, ge=1, le=86_400)
    owner_credential_lifetime_days: int = Field(default=30, ge=1, le=365)
    approval_lifetime_minutes: int = Field(default=5, ge=1, le=60)
    task_database_path: Path = Path("data/local/tasks.sqlite3")
    device_task_grants_path: Path = Path("data/local/device-task-grants.json")
    task_submission_grants_path: Path = Path("data/local/task-submission-grants.json")
    device_bootstrap_code: SecretStr | None = None

    @model_validator(mode="after")
    def validate_transport_configuration(self) -> Settings:
        has_wireguard_setting = (
            self.wireguard_bind_address is not None
            or self.wireguard_interface_guid is not None
        )
        if self.transport_mode == "local":
            if has_wireguard_setting:
                raise ValueError(
                    "WireGuard settings require JARVIS_TRANSPORT_MODE=wireguard."
                )
            return self

        if self.wireguard_bind_address is None or self.wireguard_interface_guid is None:
            raise ValueError(
                "WireGuard mode requires a bind address and interface GUID."
            )

        address = self.wireguard_bind_address
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            raise ValueError("WireGuard bind address must be a private unicast address.")
        return self
