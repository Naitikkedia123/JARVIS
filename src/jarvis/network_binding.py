"""Fail-closed listener binding for the optional Windows WireGuard transport."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from jarvis.config import Settings


class NetworkBindingError(RuntimeError):
    """The configured listener cannot be proven to belong to its selected interface."""


@dataclass(frozen=True, slots=True)
class NetworkInterfaceSnapshot:
    """Read-only Windows adapter identity and its currently assigned IP addresses."""

    interface_guid: str
    description: str
    status: str
    addresses: tuple[str, ...]


InterfaceProvider = Callable[[UUID], NetworkInterfaceSnapshot]

_POWERSHELL_QUERY = r"""
$ErrorActionPreference = 'Stop'
$expectedGuid = [guid]$env:JARVIS_WIREGUARD_INTERFACE_GUID
$matches = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {
    $_.InterfaceGuid -and ([guid]$_.InterfaceGuid -eq $expectedGuid)
})
if ($matches.Count -ne 1) { throw 'Configured network adapter was not found uniquely.' }
$adapter = $matches[0]
$interfaceIndex = $adapter.ifIndex
if ($null -eq $interfaceIndex -or [uint32]$interfaceIndex -eq 0) {
    throw 'Configured network adapter has no valid interface index.'
}
$interfaceIndex = [uint32]$interfaceIndex
$addresses = @(Get-NetIPAddress -InterfaceIndex $interfaceIndex -AddressState Preferred -ErrorAction Stop |
    ForEach-Object { $_.IPAddress })
$currentAdapter = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {
    $_.InterfaceGuid -and ([guid]$_.InterfaceGuid -eq $expectedGuid)
})
if ($currentAdapter.Count -ne 1 -or [uint32]$currentAdapter[0].ifIndex -ne $interfaceIndex) {
    throw 'Configured network adapter changed during verification.'
}
$adapter = $currentAdapter[0]
$json = [ordered]@{
    interface_guid = [string]$adapter.InterfaceGuid
    description = [string]$adapter.InterfaceDescription
    status = [string]$adapter.Status
    addresses = $addresses
} | ConvertTo-Json -Compress
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
"""


def windows_wireguard_interface(interface_guid: UUID) -> NetworkInterfaceSnapshot:
    """Read adapter details through Windows' built-in NetAdapter/NetTCPIP modules.

    Any missing cmdlet, adapter, permission, timeout, or malformed response fails
    closed. This query does not change interface, firewall, or VPN configuration.
    """
    if sys.platform != "win32":
        raise NetworkBindingError("WireGuard interface verification is supported on Windows only.")

    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise NetworkBindingError("Windows system directory is unavailable.")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise NetworkBindingError("Windows PowerShell is unavailable.")

    environment = os.environ.copy()
    environment["JARVIS_WIREGUARD_INTERFACE_GUID"] = str(interface_guid)
    try:
        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_QUERY,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise NetworkBindingError("WireGuard interface verification failed.") from exc
    if result.returncode != 0:
        raise NetworkBindingError("WireGuard interface verification failed.")

    try:
        encoded_payload = result.stdout.strip()
        payload = json.loads(base64.b64decode(encoded_payload, validate=True).decode("utf-8"))
        raw_addresses = payload["addresses"]
        if isinstance(raw_addresses, str):
            raw_addresses = [raw_addresses]
        if not isinstance(raw_addresses, list):
            raise TypeError
        return NetworkInterfaceSnapshot(
            interface_guid=str(payload["interface_guid"]),
            description=str(payload["description"]),
            status=str(payload["status"]),
            addresses=tuple(str(address) for address in raw_addresses),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NetworkBindingError("WireGuard interface verification returned invalid data.") from exc


def resolve_listener_host(
    settings: Settings,
    *,
    interface_provider: InterfaceProvider | None = None,
) -> str:
    """Return the local bind address or verify the explicit WireGuard binding."""
    if settings.transport_mode == "local":
        return settings.host

    address_value = settings.wireguard_bind_address
    interface_guid = settings.wireguard_interface_guid
    if address_value is None or interface_guid is None:
        # Also guard Settings constructed by untyped callers or test doubles.
        raise NetworkBindingError("WireGuard mode requires a bind address and interface GUID.")

    try:
        address = ipaddress.ip_address(str(address_value))
    except ValueError as exc:
        raise NetworkBindingError("WireGuard bind address is invalid.") from exc
    if (
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    ):
        raise NetworkBindingError("WireGuard bind address must be a private unicast address.")

    provider = interface_provider or windows_wireguard_interface
    try:
        interface = provider(interface_guid)
        actual_guid = UUID(interface.interface_guid)
        assigned_addresses = {ipaddress.ip_address(value) for value in interface.addresses}
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NetworkBindingError("WireGuard interface could not be verified.") from exc

    if (
        actual_guid != interface_guid
        or "wireguard" not in interface.description.casefold()
        or interface.status.casefold() != "up"
        or address not in assigned_addresses
    ):
        raise NetworkBindingError("Configured address is not assigned to the selected WireGuard interface.")
    return str(address)
