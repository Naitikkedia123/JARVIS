"""Persistent device identity and pairing primitives for the JARVIS control plane."""

from jarvis.devices.models import (
    CapabilityReport,
    Device,
    DevicePlatform,
    DeviceRole,
    PairingMetadata,
    PairingReceipt,
)
from jarvis.devices.service import (
    DeviceAuthenticationError,
    DeviceAuthorizationError,
    DeviceService,
)
from jarvis.devices.store import (
    DeviceNotFoundError,
    DevicePairingUnavailableError,
    DeviceStore,
    DeviceStoreError,
)

__all__ = [
    "CapabilityReport",
    "Device",
    "DeviceAuthenticationError",
    "DeviceAuthorizationError",
    "DeviceNotFoundError",
    "DevicePairingUnavailableError",
    "DevicePlatform",
    "DeviceRole",
    "DeviceService",
    "DeviceStore",
    "DeviceStoreError",
    "PairingMetadata",
    "PairingReceipt",
]
