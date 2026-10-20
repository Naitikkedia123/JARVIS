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


class TaskRunnerUnavailableError(TaskError):
    """Legacy task execution is unavailable because no legacy runner is configured."""


class TaskAuthorizationError(TaskError):
    """A device task lacks a valid explicit policy authorization decision."""


class TaskSubmissionPolicyUnavailableError(TaskError):
    """The local submission policy could not be read or validated safely."""


class TaskDispatchDeniedError(TaskAuthorizationError):
    """An authenticated owner was denied by the current dispatch policy."""

    def __init__(
        self,
        *,
        device_id: str,
        action_digest: str,
        authorization_fingerprint: str | None = None,
    ) -> None:
        self.device_id = device_id
        self.action_digest = action_digest
        self.authorization_fingerprint = authorization_fingerprint
        super().__init__("Task execution is not authorized.")


class TaskSubmissionConflictError(TaskError):
    """An idempotency key was reused for a different request by the same device."""


class DependencyNotSatisfiedError(TaskError):
    """Raised when a step is started before all dependencies succeeded."""


class RetryLimitExceededError(TaskError):
    """Raised when a step has consumed its configured attempt limit."""


class ActiveAttemptError(TaskError):
    """Raised when an operation conflicts with an unfinished step attempt."""
