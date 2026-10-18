"""Unit tests for workspace tool schemas and bounded local operations."""

import asyncio
import errno
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvis.tools import (
    AuthorizationContext,
    InMemoryToolAuditSink,
    ToolDispatcher,
    ToolExecutionError,
    ToolInputValidationError,
    ToolPolicy,
    ToolRegistry,
    ToolRequest,
)
from jarvis.tools.workspace import (
    MAX_WORKSPACE_READ_BYTES,
    MAX_WORKSPACE_WRITE_BYTES,
    CreateWorkspaceFileInput,
    WorkspaceTools,
)


def build_dispatcher(workspace_root) -> ToolDispatcher:
    registry = ToolRegistry()
    tools = WorkspaceTools(workspace_root)
    tools.register_tools(registry)
    return ToolDispatcher(registry, ToolPolicy(), tools.executors, InMemoryToolAuditSink())


def reader_context() -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="workspace-reader",
        granted_capabilities=frozenset({"workspace:read"}),
    )


def writer_context() -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="workspace-writer",
        granted_capabilities=frozenset({"workspace:write", "tool:write"}),
    )


def test_workspace_root_must_be_explicit_absolute_and_existing(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        WorkspaceTools("relative-workspace")
    with pytest.raises(ValueError, match="absolute"):
        WorkspaceTools(tmp_path / "missing")


def test_workspace_root_rejects_symlinked_original_path(tmp_path) -> None:
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked"
    try:
        linked_root.symlink_to(actual_root, target_is_directory=True)
    except OSError as error:
        permission_limited = error.errno in {errno.EACCES, errno.EPERM, errno.ENOTSUP}
        windows_privilege_limited = os.name == "nt" and getattr(error, "winerror", None) == 1314
        if permission_limited or windows_privilege_limited:
            pytest.skip("The current operating system or process cannot create symlinks.")
        raise

    with pytest.raises(ValueError, match="non-symlink"):
        WorkspaceTools(linked_root)


def test_workspace_root_rejects_identity_change_during_initialization(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    redirected_root = tmp_path / "redirected"
    redirected_root.mkdir()
    original_resolve = Path.resolve

    def resolve_with_redirect(path: Path, *, strict: bool = False) -> Path:
        if path == root:
            return original_resolve(redirected_root, strict=strict)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_with_redirect)
    with pytest.raises(ValueError, match="non-symlink"):
        WorkspaceTools(root)


def test_workspace_root_replacement_after_initialization_fails_closed(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    tools = WorkspaceTools(root)
    registry = ToolRegistry()
    tools.register_tools(registry)
    dispatcher = ToolDispatcher(registry, ToolPolicy(), tools.executors, InMemoryToolAuditSink())

    displaced_root = tmp_path / "displaced"
    root.rename(displaced_root)
    root.mkdir()

    with pytest.raises(ToolExecutionError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(tool_name="workspace.list_files"),
                authorization_context=reader_context(),
            )
        )


def test_workspace_tools_list_read_and_create_only_relative_files(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "known.txt").write_text("hello workspace", encoding="utf-8")
    dispatcher = build_dispatcher(root)

    listed = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(tool_name="workspace.list_files"),
            authorization_context=reader_context(),
        )
    )
    assert listed.output.files == ["known.txt"]

    read = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(tool_name="workspace.read_file", arguments={"path": "known.txt"}),
            authorization_context=reader_context(),
        )
    )
    assert read.output.content == "hello workspace"
    assert read.output.path == "known.txt"
    assert read.output.byte_count == len(b"hello workspace")

    (root / "new").mkdir()
    created = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(
                tool_name="workspace.create_file",
                arguments={"path": "new/note.txt", "content": "created safely"},
            ),
            authorization_context=writer_context(),
        )
    )
    assert created.output.path == "new/note.txt"
    assert (root / "new" / "note.txt").read_text(encoding="utf-8") == "created safely"


def test_workspace_read_and_write_content_limits_are_enforced(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    dispatcher = build_dispatcher(root)

    oversized = "é" * (MAX_WORKSPACE_WRITE_BYTES // 2 + 1)
    with pytest.raises(ValidationError):
        CreateWorkspaceFileInput(path="large.txt", content=oversized)
    with pytest.raises(ToolInputValidationError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(
                    tool_name="workspace.create_file",
                    arguments={"path": "large.txt", "content": oversized},
                ),
                authorization_context=writer_context(),
            )
        )

    (root / "large.txt").write_bytes(b"x" * (MAX_WORKSPACE_READ_BYTES + 1))
    with pytest.raises(ToolExecutionError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(tool_name="workspace.read_file", arguments={"path": "large.txt"}),
                authorization_context=reader_context(),
            )
        )


def test_workspace_request_schema_does_not_accept_a_root_override(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    dispatcher = build_dispatcher(root)

    with pytest.raises(ToolInputValidationError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(
                    tool_name="workspace.list_files",
                    arguments={"relative_directory": ".", "workspace_root": "C:/"},
                ),
                authorization_context=reader_context(),
            )
        )
