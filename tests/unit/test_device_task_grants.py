"""Default-deny tests for local device dispatch grants."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jarvis.devices.models import Device, DevicePlatform, DeviceRole
from jarvis.policy.device_grants import LocalDeviceTaskGrantPolicy
from jarvis.tasks.action import AuthorizedTaskAction


def make_device(
    device_id: str = "a" * 32,
    *,
    role: DeviceRole = DeviceRole.COMPANION,
    capabilities: frozenset[str] = frozenset(),
) -> Device:
    now = datetime.now(UTC)
    return Device(
        id=device_id,
        name="test companion",
        platform=DevicePlatform.ANDROID,
        role=role,
        reported_capabilities=capabilities,
        paired_at=now,
        last_seen_at=now,
    )


def write_config(path, device_ids: list[str]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": device_ids}),
        encoding="utf-8",
    )


def status_action(**arguments: object) -> AuthorizedTaskAction:
    return AuthorizedTaskAction(tool_name="jarvis.status", arguments=arguments)


def test_missing_and_empty_allowlists_deny_without_issuing_context(tmp_path) -> None:
    missing = LocalDeviceTaskGrantPolicy(tmp_path / "missing.json")
    decision = missing.authorize(device_id="a" * 32, action=status_action())
    assert decision.allowed is False
    assert decision.authorization_context is None

    config = tmp_path / "grants.json"
    write_config(config, [])
    empty = LocalDeviceTaskGrantPolicy(config)
    decision = empty.authorize(device_id="a" * 32, action=status_action())
    assert decision.allowed is False
    assert decision.authorization_context is None


def test_allow_is_device_specific_and_grants_only_exact_status_permission(tmp_path) -> None:
    config = tmp_path / "grants.json"
    granted_id = "a" * 32
    write_config(config, [granted_id])
    policy = LocalDeviceTaskGrantPolicy(config)

    # The policy only receives authenticated ID and action; claimed role and
    # capabilities cannot widen the grant.
    owner = make_device(
        granted_id,
        role=DeviceRole.OWNER,
        capabilities=frozenset({"tool:write", "workspace:write"}),
    )
    decision = policy.authorize(device_id=owner.id, action=status_action())
    assert decision.allowed is True
    assert decision.authorization_context is not None
    assert decision.authorization_context.principal_id == granted_id
    assert decision.authorization_context.granted_capabilities == frozenset(
        {"system:status:read"}
    )

    unlisted = make_device(
        "b" * 32,
        capabilities=frozenset({"system:status:read", "tool:write"}),
    )
    denied = policy.authorize(device_id=unlisted.id, action=status_action())
    assert denied.allowed is False
    assert denied.authorization_context is None


@pytest.mark.parametrize(
    "action",
    [
        AuthorizedTaskAction(tool_name="workspace.read_file", arguments={}),
        AuthorizedTaskAction(tool_name="jarvis.status", arguments={"extra": True}),
    ],
)
def test_non_status_or_nonempty_arguments_are_denied(tmp_path, action) -> None:
    config = tmp_path / "grants.json"
    write_config(config, ["a" * 32])
    decision = LocalDeviceTaskGrantPolicy(config).authorize(
        device_id="a" * 32,
        action=action,
    )
    assert decision.allowed is False
    assert decision.authorization_context is None


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        '{"schema_version":1,"allowed_device_ids":[],"allowed_device_ids":[]}',
        '{"schema_version":1,"allowed_device_ids":["' + "a" * 32 + '","' + "a" * 32 + '"]}',
        '{"schema_version":2,"allowed_device_ids":[]}',
        '{"schema_version":1,"allowed_device_ids":["not-a-device-id"]}',
        '{"schema_version":1,"allowed_device_ids":[],"unexpected":true}',
        '{"schema_version":NaN,"allowed_device_ids":[]}',
    ],
)
def test_invalid_or_ambiguous_configuration_fails_closed(tmp_path, raw) -> None:
    config = tmp_path / "grants.json"
    config.write_text(raw, encoding="utf-8")
    decision = LocalDeviceTaskGrantPolicy(config).authorize(
        device_id="a" * 32,
        action=status_action(),
    )
    assert decision.allowed is False
    assert decision.authorization_context is None


def test_configuration_is_reloaded_and_revocation_applies_on_next_decision(tmp_path) -> None:
    config = tmp_path / "grants.json"
    device_id = "a" * 32
    write_config(config, [device_id])
    policy = LocalDeviceTaskGrantPolicy(config)
    assert policy.authorize(device_id=device_id, action=status_action()).allowed is True

    write_config(config, [])
    revoked = policy.authorize(device_id=device_id, action=status_action())
    assert revoked.allowed is False
    assert revoked.authorization_context is None

