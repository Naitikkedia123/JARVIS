"""Read-only listing/reading and exclusive creation in an injected workspace."""

from __future__ import annotations

import heapq
import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.tools.dispatcher import ToolExecutor
from jarvis.tools.models import MAX_TOOL_RESULT_BYTES, ToolAccess, ToolDefinition
from jarvis.tools.registry import ToolRegistry

MAX_WORKSPACE_PATH_LENGTH = 1_024
MAX_WORKSPACE_COMPONENTS = 64
MAX_WORKSPACE_LIST_ENTRIES_SCANNED = 10_000
MAX_WORKSPACE_LIST_FILES = 100
MAX_WORKSPACE_READ_BYTES = 65_536
MAX_WORKSPACE_WRITE_BYTES = 32_768
WORKSPACE_READ_CAPABILITY = "workspace:read"
WORKSPACE_WRITE_CAPABILITY = "workspace:write"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)

_Filename = Annotated[str, Field(min_length=1, max_length=255)]


class WorkspaceToolError(Exception):
    """Base class for safe internal workspace-operation errors."""


class WorkspacePathError(WorkspaceToolError):
    """A path is invalid, missing, or outside the injected workspace."""


class WorkspaceOperationError(WorkspaceToolError):
    """A filesystem operation failed without exposing host details."""


class ListWorkspaceFilesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_directory: str = Field(default=".", max_length=MAX_WORKSPACE_PATH_LENGTH)


class ListWorkspaceFilesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_directory: str = Field(max_length=MAX_WORKSPACE_PATH_LENGTH)
    files: list[_Filename] = Field(max_length=MAX_WORKSPACE_LIST_FILES)


class ReadWorkspaceFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_WORKSPACE_PATH_LENGTH)


class ReadWorkspaceFileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_WORKSPACE_PATH_LENGTH)
    content: str = Field(max_length=MAX_WORKSPACE_READ_BYTES)
    byte_count: int = Field(ge=0, le=MAX_WORKSPACE_READ_BYTES)


class CreateWorkspaceFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_WORKSPACE_PATH_LENGTH)
    content: str = Field(max_length=MAX_WORKSPACE_WRITE_BYTES)

    @field_validator("content")
    @classmethod
    def content_has_bounded_utf8_size(cls, value: str) -> str:
        try:
            byte_count = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("Content must be valid UTF-8 text.") from None
        if byte_count > MAX_WORKSPACE_WRITE_BYTES:
            raise ValueError("Workspace file content exceeds the configured limit.")
        return value


class CreateWorkspaceFileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=MAX_WORKSPACE_PATH_LENGTH)
    byte_count: int = Field(ge=0, le=MAX_WORKSPACE_WRITE_BYTES)


class WorkspaceTools:
    """Workspace executors whose root comes only from trusted initialization."""

    def __init__(self, workspace_root: str | os.PathLike[str]) -> None:
        root = Path(workspace_root)
        if not root.is_absolute():
            raise ValueError("Workspace root must be an absolute, non-symlink directory.")
        try:
            original_status = root.lstat()
            if _is_redirected(original_status) or not stat.S_ISDIR(original_status.st_mode):
                raise ValueError(
                    "Workspace root must be an absolute, non-symlink directory."
                )
            resolved_root = root.resolve(strict=True)
            resolved_status = resolved_root.stat()
            final_original_status = root.lstat()
        except (OSError, RuntimeError):
            raise ValueError("Workspace root must be an absolute, non-symlink directory.") from None
        original_identity = _filesystem_identity(original_status)
        if (
            not stat.S_ISDIR(resolved_status.st_mode)
            or _is_redirected(final_original_status)
            or not stat.S_ISDIR(final_original_status.st_mode)
            or _filesystem_identity(resolved_status) != original_identity
            or _filesystem_identity(final_original_status) != original_identity
        ):
            raise ValueError("Workspace root must be an absolute, non-symlink directory.")
        self._root = resolved_root
        self._root_identity = original_identity
        self._list_executor = _ListWorkspaceFilesExecutor(self)
        self._read_executor = _ReadWorkspaceFileExecutor(self)
        self._create_executor = _CreateWorkspaceFileExecutor(self)

    @property
    def executors(self) -> Mapping[str, ToolExecutor]:
        return {
            "workspace.list_files": self._list_executor,
            "workspace.read_file": self._read_executor,
            "workspace.create_file": self._create_executor,
        }

    def register_tools(self, registry: ToolRegistry) -> None:
        """Register workspace contracts before constructing the dispatcher."""
        definitions = (
            ToolDefinition(
                name="workspace.list_files",
                description="List immediate regular file names in the configured workspace.",
                input_schema=ListWorkspaceFilesInput,
                output_schema=ListWorkspaceFilesOutput,
                required_capabilities=frozenset({WORKSPACE_READ_CAPABILITY}),
                access=ToolAccess.READ,
                timeout_seconds=2.0,
                max_result_bytes=16_384,
                audit_fields=frozenset({"relative_directory", "files"}),
            ),
            ToolDefinition(
                name="workspace.read_file",
                description="Read a bounded UTF-8 text file from the configured workspace.",
                input_schema=ReadWorkspaceFileInput,
                output_schema=ReadWorkspaceFileOutput,
                required_capabilities=frozenset({WORKSPACE_READ_CAPABILITY}),
                access=ToolAccess.READ,
                timeout_seconds=2.0,
                max_result_bytes=MAX_TOOL_RESULT_BYTES,
                audit_fields=frozenset({"path", "byte_count"}),
            ),
            ToolDefinition(
                name="workspace.create_file",
                description="Create a new bounded UTF-8 text file in the configured workspace.",
                input_schema=CreateWorkspaceFileInput,
                output_schema=CreateWorkspaceFileOutput,
                required_capabilities=frozenset({WORKSPACE_WRITE_CAPABILITY}),
                access=ToolAccess.WRITE,
                timeout_seconds=2.0,
                max_result_bytes=2_048,
                audit_fields=frozenset({"path", "byte_count"}),
            ),
        )
        for definition in definitions:
            registry.register(definition)

    def _list_files(self, relative_directory: str) -> ListWorkspaceFilesOutput:
        parts = _validated_parts(relative_directory, allow_root=True)
        directory = self._existing_directory(parts)
        scanned = 0

        def file_names():
            nonlocal scanned
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_WORKSPACE_LIST_ENTRIES_SCANNED:
                            raise WorkspaceOperationError()
                        try:
                            if not entry.is_symlink() and entry.is_file(follow_symlinks=False):
                                yield entry.name
                        except OSError:
                            raise WorkspaceOperationError() from None
            except WorkspaceToolError:
                raise
            except OSError:
                raise WorkspaceOperationError() from None

        ordered_names = heapq.nsmallest(
            MAX_WORKSPACE_LIST_FILES,
            file_names(),
            key=lambda name: (name.casefold(), name),
        )
        return ListWorkspaceFilesOutput(
            relative_directory="/".join(parts) if parts else ".",
            files=ordered_names,
        )

    def _read_file(self, relative_path: str) -> ReadWorkspaceFileOutput:
        parts = _validated_parts(relative_path)
        path = self._existing_file(parts)
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise WorkspacePathError()
                content_bytes = file.read(MAX_WORKSPACE_READ_BYTES + 1)
        except WorkspaceToolError:
            raise
        except OSError:
            raise WorkspaceOperationError() from None
        if len(content_bytes) > MAX_WORKSPACE_READ_BYTES:
            raise WorkspaceOperationError()
        try:
            content = content_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise WorkspaceOperationError() from None
        return ReadWorkspaceFileOutput(
            path="/".join(parts), content=content, byte_count=len(content_bytes)
        )

    def _create_file(self, relative_path: str, content: str) -> CreateWorkspaceFileOutput:
        parts = _validated_parts(relative_path)
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceOperationError() from None
        if len(content_bytes) > MAX_WORKSPACE_WRITE_BYTES:
            raise WorkspaceOperationError()
        parent = self._existing_directory(parts[:-1])
        target = parent / parts[-1]
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise WorkspaceOperationError() from None
        else:
            raise WorkspacePathError()

        descriptor: int | None = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                descriptor = None
                written = file.write(content_bytes)
                if written != len(content_bytes):
                    raise WorkspaceOperationError()
        except FileExistsError:
            raise WorkspacePathError() from None
        except WorkspaceToolError:
            raise
        except OSError:
            raise WorkspaceOperationError() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return CreateWorkspaceFileOutput(path="/".join(parts), byte_count=len(content_bytes))

    def _existing_directory(self, parts: tuple[str, ...]) -> Path:
        current = self._root
        for part in parts:
            current = current / part
            status = _lstat_status(current)
            if _is_redirected(status) or not stat.S_ISDIR(status.st_mode):
                raise WorkspacePathError()
        return self._resolve_inside_root(current, strict=True)

    def _existing_file(self, parts: tuple[str, ...]) -> Path:
        if not parts:
            raise WorkspacePathError()
        parent = self._existing_directory(parts[:-1])
        candidate = parent / parts[-1]
        status = _lstat_status(candidate)
        if _is_redirected(status) or not stat.S_ISREG(status.st_mode):
            raise WorkspacePathError()
        return self._resolve_inside_root(candidate, strict=True)

    def _resolve_inside_root(self, path: Path, *, strict: bool) -> Path:
        try:
            root_status = self._root.lstat()
            if (
                _is_redirected(root_status)
                or not stat.S_ISDIR(root_status.st_mode)
                or _filesystem_identity(root_status) != self._root_identity
            ):
                raise WorkspacePathError()
            resolved = path.resolve(strict=strict)
            resolved.relative_to(self._root)
        except WorkspacePathError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise WorkspacePathError() from None
        return resolved


class _ListWorkspaceFilesExecutor:
    def __init__(self, tools: WorkspaceTools) -> None:
        self._tools = tools

    async def execute(self, arguments: BaseModel) -> object:
        if not isinstance(arguments, ListWorkspaceFilesInput):
            raise WorkspaceOperationError()
        return self._tools._list_files(arguments.relative_directory)


class _ReadWorkspaceFileExecutor:
    def __init__(self, tools: WorkspaceTools) -> None:
        self._tools = tools

    async def execute(self, arguments: BaseModel) -> object:
        if not isinstance(arguments, ReadWorkspaceFileInput):
            raise WorkspaceOperationError()
        return self._tools._read_file(arguments.path)


class _CreateWorkspaceFileExecutor:
    def __init__(self, tools: WorkspaceTools) -> None:
        self._tools = tools

    async def execute(self, arguments: BaseModel) -> object:
        if not isinstance(arguments, CreateWorkspaceFileInput):
            raise WorkspaceOperationError()
        return self._tools._create_file(arguments.path, arguments.content)


def _validated_parts(relative_path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or len(relative_path) > MAX_WORKSPACE_PATH_LENGTH:
        raise WorkspacePathError()
    if allow_root and relative_path == ".":
        return ()
    if not relative_path or "\x00" in relative_path:
        raise WorkspacePathError()
    windows_path = PureWindowsPath(relative_path)
    if (
        windows_path.drive
        or windows_path.root
        or PurePosixPath(relative_path).is_absolute()
        or ":" in relative_path
        or relative_path.startswith(("/", "\\"))
    ):
        raise WorkspacePathError()
    components = tuple(relative_path.replace("\\", "/").split("/"))
    if (
        len(components) > MAX_WORKSPACE_COMPONENTS
        or any(
            component in {"", ".", ".."}
            or component.endswith((" ", "."))
            or component.split(".", maxsplit=1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for component in components
        )
    ):
        raise WorkspacePathError()
    return components


def _lstat_status(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise WorkspacePathError() from None


def _filesystem_identity(status: os.stat_result) -> tuple[int, int]:
    """Return the device and file identity used to detect root replacement."""
    return status.st_dev, status.st_ino


def _is_redirected(status: os.stat_result) -> bool:
    """Reject symbolic links and Windows reparse points such as junctions."""
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & reparse_point)
