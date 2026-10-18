"""Safe typed errors for memory operations."""


class MemoryError(Exception):
    """Base error for memory operations."""


class MemoryRecordNotFoundError(MemoryError):
    """A record is missing or inaccessible under the supplied policy."""


class MemoryAuthorizationError(MemoryError):
    """A caller cannot perform a requested memory mutation."""


class MemoryConflictError(MemoryError):
    """A record changed or was superseded since the caller read it."""


class MemoryStoreError(MemoryError):
    """A safe, normalized persistence failure."""
