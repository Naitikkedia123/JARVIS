"""Typed errors for the durable task domain and store."""


class TaskError(Exception):
    """Base class for safe task-domain errors."""


class InvalidTransitionError(TaskError):
    """Raised when a state change is not permitted by the finite state machine."""


class InvalidDependencyError(TaskError):
    """Raised when a task plan contains an invalid dependency graph."""


class EntityNotFoundError(TaskError):
    """Raised when a requested persisted entity does not exist."""


class TaskStoreError(TaskError):
    """Raised for safe, normalized persistence errors."""


class DependencyNotSatisfiedError(TaskError):
    """Raised when a step is started before all dependencies succeeded."""


class RetryLimitExceededError(TaskError):
    """Raised when a step has consumed its configured attempt limit."""


class ActiveAttemptError(TaskError):
    """Raised when an operation conflicts with an unfinished step attempt."""
