"""Fail-closed configuration and verification for private listener binding."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.network_binding import (
    NetworkBindingError,
    NetworkInterfaceSnapshot,
    resolve_listener_host,
    windows_wireguard_interface,
)

INTERFACE_GUID = UUID("a1b2c3d4-e5f6-4789-9012-1234567890ab")
WG_ADDRESS = "10.66.0.1"


def wireguard_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "transport_mode": "wireguard",
        "wireguard_bind_address": WG_ADDRESS,
        "wireguard_interface_guid": INTERFACE_GUID,
    }
    values.update(overrides)
    return Settings(**values)


def snapshot(
    *,
    interface_guid: str = str(INTERFACE_GUID),
    description: str = "WireGuard Tunnel",
    status: str = "Up",
    addresses: tuple[str, ...] = (WG_ADDRESS,),
) -> NetworkInterfaceSnapshot:
    return NetworkInterfaceSnapshot(interface_guid, description, status, addresses)


def test_default_configuration_and_app_are_loopback_only(tmp_path) -> None:
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
    )

    assert settings.transport_mode == "local"
    assert settings.host == "127.0.0.1"
    assert resolve_listener_host(settings) == "127.0.0.1"
    application = create_app(settings)
    assert application.state.listener_host == "127.0.0.1"
    with TestClient(application) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"transport_mode": "wireguard"},
        {"transport_mode": "wireguard", "wireguard_bind_address": WG_ADDRESS},
        {"transport_mode": "wireguard", "wireguard_interface_guid": INTERFACE_GUID},
        {"wireguard_bind_address": WG_ADDRESS, "wireguard_interface_guid": INTERFACE_GUID},
    ],
)
def test_wireguard_mode_requires_explicit_opt_in_and_complete_configuration(overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_local_mode_rejects_ignored_wireguard_settings() -> None:
    with pytest.raises(ValidationError):
        Settings(wireguard_interface_guid=INTERFACE_GUID)


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "::", "8.8.8.8", "2001:4860:4860::8888", "127.0.0.1", "169.254.1.10", "224.0.0.1"],
)
def test_wireguard_configuration_rejects_wildcard_public_or_non_unicast_address(address) -> None:
    with pytest.raises(ValidationError):
        wireguard_settings(wireguard_bind_address=address)


def test_private_address_is_accepted_only_when_assigned_to_named_wireguard_interface() -> None:
    assert resolve_listener_host(
        wireguard_settings(), interface_provider=lambda _: snapshot()
    ) == WG_ADDRESS


def test_remote_app_revalidates_the_selected_interface_during_startup(tmp_path) -> None:
    settings = wireguard_settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
    )
    calls = 0

    def provider(_: UUID) -> NetworkInterfaceSnapshot:
        nonlocal calls
        calls += 1
        return snapshot()

    application = create_app(settings, interface_provider=provider)
    assert application.state.listener_host == WG_ADDRESS
    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
    assert calls >= 2


@pytest.mark.parametrize(
    "interface",
    [
        snapshot(addresses=("10.66.0.2",)),
        snapshot(description="Intel Ethernet Adapter"),
        snapshot(interface_guid="b1b2c3d4-e5f6-4789-9012-1234567890ab"),
        snapshot(status="Disabled"),
    ],
)
def test_unassigned_non_wireguard_mismatched_or_down_interfaces_fail_closed(interface) -> None:
    with pytest.raises(NetworkBindingError):
        resolve_listener_host(
            wireguard_settings(), interface_provider=lambda _: interface
        )


def test_interface_discovery_error_and_invalid_discovery_data_fail_closed() -> None:
    def unavailable(_: UUID) -> NetworkInterfaceSnapshot:
        raise OSError("adapter query failed")

    with pytest.raises(NetworkBindingError):
        resolve_listener_host(wireguard_settings(), interface_provider=unavailable)

    with pytest.raises(NetworkBindingError):
        resolve_listener_host(
            wireguard_settings(),
            interface_provider=lambda _: NetworkInterfaceSnapshot(
                str(INTERFACE_GUID), "WireGuard Tunnel", "Up", ("not-an-ip",)
            ),
        )


def test_app_creation_fails_before_startup_when_selected_interface_is_invalid(tmp_path) -> None:
    settings = wireguard_settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
    )
    with pytest.raises(NetworkBindingError):
        create_app(settings, interface_provider=lambda _: snapshot(status="Down"))


def test_windows_interface_query_uses_read_only_guid_and_address_lookups(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis import network_binding

    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"")
    monkeypatch.setattr(network_binding.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path))

    def run(command, **kwargs):
        assert command[0] == str(powershell)
        assert "-NoProfile" in command
        assert "-NonInteractive" in command
        assert kwargs["shell"] is False
        assert kwargs["timeout"] <= 5
        assert kwargs["env"]["JARVIS_WIREGUARD_INTERFACE_GUID"] == str(INTERFACE_GUID)
        assert "Get-NetAdapter -IncludeHidden" in command[-1]
        assert "Get-NetIPAddress -InterfaceIndex" in command[-1]
        assert "[uint32]$interfaceIndex -eq 0" in command[-1]
        assert "changed during verification" in command[-1]
        payload = {
            "interface_guid": str(INTERFACE_GUID),
            "description": "WireGuard Tunnel",
            "status": "Up",
            "addresses": [WG_ADDRESS],
        }
        encoded_payload = base64.b64encode(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii")
        return subprocess.CompletedProcess(command, 0, encoded_payload, "")

    monkeypatch.setattr(network_binding.subprocess, "run", run)
    assert windows_wireguard_interface(INTERFACE_GUID) == snapshot()


def test_windows_interface_query_failure_is_normalized_and_fails_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvis import network_binding

    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"")
    monkeypatch.setattr(network_binding.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setattr(
        network_binding.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "denied"),
    )
    with pytest.raises(NetworkBindingError, match="verification failed"):
        windows_wireguard_interface(INTERFACE_GUID)


def test_local_launcher_uses_the_validated_app_listener() -> None:
    content = Path("scripts/run-local.ps1").read_text(encoding="utf-8")
    assert "from jarvis.main import app; print(app.state.listener_host)" in content
    assert "--host $listenerHost" in content
    assert "--host 127.0.0.1" not in content
