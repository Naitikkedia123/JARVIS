"""Default-deny tests for local device task-submission grants."""

from __future__ import annotations

import json

import pytest

from jarvis.devices.models import DeviceId
from jarvis.policy.task_submission import LocalTaskSubmissionPolicy
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskSubmissionProposal
from jarvis.tasks.errors import TaskSubmissionPolicyUnavailableError

DEVICE_ID: DeviceId = "a" * 32


def proposal(*, tool_name: str = "jarvis.status", arguments=None) -> TaskSubmissionProposal:
    return TaskSubmissionProposal(
        idempotency_key="submission-key-0001",
        title="Check local status",
        action=AuthorizedTaskAction(
            tool_name=tool_name,
            arguments={} if arguments is None else arguments,
        ),
    )


def write_policy(path, device_ids: list[str]) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": device_ids}),
        encoding="utf-8",
    )


def test_missing_unreadable_or_invalid_policy_is_unavailable(tmp_path) -> None:
    policy = LocalTaskSubmissionPolicy(tmp_path / "missing.json")
    with pytest.raises(TaskSubmissionPolicyUnavailableError):
        policy.authorize(device_id=DEVICE_ID, proposal=proposal())

    config = tmp_path / "grants.json"
    config.write_text("{invalid", encoding="utf-8")
    with pytest.raises(TaskSubmissionPolicyUnavailableError):
        LocalTaskSubmissionPolicy(config).authorize(
            device_id=DEVICE_ID,
            proposal=proposal(),
        )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{invalid",
        '{"schema_version":1,"allowed_device_ids":[],"allowed_device_ids":[]}',
        '{"schema_version":1,"allowed_device_ids":["' + "a" * 32 + '","' + "a" * 32 + '"]}',
        '{"schema_version":2,"allowed_device_ids":[]}',
        '{"schema_version":1,"allowed_device_ids":["bad"]}',
        '{"schema_version":1,"allowed_device_ids":[],"extra":true}',
        '{"schema_version":NaN,"allowed_device_ids":[]}',
    ],
)
def test_invalid_duplicate_or_unsupported_config_raises_safe_error(tmp_path, raw) -> None:
    config = tmp_path / "grants.json"
    config.write_text(raw, encoding="utf-8")
    with pytest.raises(TaskSubmissionPolicyUnavailableError) as raised:
        LocalTaskSubmissionPolicy(config).authorize(
            device_id=DEVICE_ID,
            proposal=proposal(),
        )
    assert str(raised.value) == "Task submission policy is unavailable."
    if raw:
        assert raw not in str(raised.value)


def test_valid_empty_or_nonmatching_policy_denies_without_context(tmp_path) -> None:
    config = tmp_path / "grants.json"
    write_policy(config, [])
    policy = LocalTaskSubmissionPolicy(config)
    denied = policy.authorize(device_id=DEVICE_ID, proposal=proposal())
    assert denied.allowed is False
    assert denied.authorization_context is None

    write_policy(config, ["b" * 32])
    denied = policy.authorize(device_id=DEVICE_ID, proposal=proposal())
    assert denied.allowed is False
    assert denied.authorization_context is None


def test_exact_device_and_empty_status_action_receive_no_extra_capabilities(tmp_path) -> None:
    config = tmp_path / "grants.json"
    write_policy(config, [DEVICE_ID])
    policy = LocalTaskSubmissionPolicy(config)

    decision = policy.authorize(device_id=DEVICE_ID, proposal=proposal())
    assert decision.allowed is True
    assert decision.authorization_context is not None
    assert decision.authorization_context.principal_id == DEVICE_ID
    assert decision.authorization_context.granted_capabilities == frozenset()

    assert not policy.authorize(
        device_id=DEVICE_ID,
        proposal=proposal(tool_name="workspace.read_file"),
    ).allowed
    assert not policy.authorize(
        device_id=DEVICE_ID,
        proposal=proposal(arguments={"unexpected": True}),
    ).allowed


def test_policy_file_is_reloaded_so_device_revocation_takes_effect(tmp_path) -> None:
    config = tmp_path / "grants.json"
    write_policy(config, [DEVICE_ID])
    policy = LocalTaskSubmissionPolicy(config)
    assert policy.authorize(device_id=DEVICE_ID, proposal=proposal()).allowed is True

    write_policy(config, [])
    decision = policy.authorize(device_id=DEVICE_ID, proposal=proposal())
    assert decision.allowed is False
    assert decision.authorization_context is None

