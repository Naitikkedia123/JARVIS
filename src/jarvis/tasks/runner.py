"""Freshly authenticated, dispatcher-only execution for owned device tasks."""

from __future__ import annotations

from pydantic import SecretStr

from jarvis.devices.models import Device
from jarvis.devices.service import DeviceService
from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.tasks.authorization import (
    DeviceTaskDispatchPolicy,
    DispatchAuthorization,
    resolve_dispatch_authorization,
)
from jarvis.tasks.errors import TaskAuthorizationError, TaskDispatchDeniedError
from jarvis.tasks.models import AttemptAuthorizationBinding, Step, StepResult
from jarvis.tasks.store import TaskStore
from jarvis.tools.dispatcher import ToolDispatcher
from jarvis.tools.errors import AuthorizationDeniedError, ToolAuditError, ToolExecutionError
from jarvis.tools.models import (
    ApprovedTaskActionPermit,
    ToolDefinition,
    ToolExecutionCorrelation,
)


class DispatcherBackedTaskRunner:
    """The only authorized task runner; all tool work goes through ToolDispatcher."""

    def __init__(
        self,
        *,
        device_service: DeviceService,
        store: TaskStore,
        dispatcher: ToolDispatcher,
        policy: DeviceTaskDispatchPolicy | None,
    ) -> None:
        if type(dispatcher) is not ToolDispatcher:
            raise ValueError("A concrete ToolDispatcher is required.")
        dispatcher.require_policy(DeviceTaskToolPolicy)
        self.device_service = device_service
        self.store = store
        self.dispatcher = dispatcher
        self.policy = policy

    def authorize(
        self, task_id: str, step: Step, access_token: SecretStr
    ) -> DispatchAuthorization:
        """Authenticate now and bind a fresh policy grant to the persisted action."""
        if not isinstance(access_token, SecretStr):
            raise TaskAuthorizationError("Task execution is not authorized.")
        try:
            device = self.device_service.authenticate(access_token)
        except Exception:
            raise TaskAuthorizationError("Task execution is not authorized.") from None
        return self.authorize_authenticated_device(task_id, step, device)

    def authorize_authenticated_device(
        self, task_id: str, step: Step, device: Device
    ) -> DispatchAuthorization:
        """Authorize a trusted DeviceStore identity without opening another auth transaction."""
        try:
            ownership = self.store.get_task_ownership(task_id)
            current = self.store.get_step(step.id)
        except Exception:
            raise TaskAuthorizationError("Task execution is not authorized.") from None
        if step.action is None or current.action is None or (
            ownership is None
            or ownership.device_id != device.id
            or current.task_id != task_id
            or current.action.digest() != step.action.digest()
        ):
            raise TaskAuthorizationError("Task execution is not authorized.")
        try:
            return resolve_dispatch_authorization(device, current.action, self.policy)
        except TaskDispatchDeniedError:
            raise
        except TaskAuthorizationError:
            raise TaskDispatchDeniedError(
                device_id=device.id,
                action_digest=current.action.digest(),
            ) from None

    async def execute_claimed(
        self,
        task_id: str,
        step: Step,
        access_token: SecretStr,
        binding: AttemptAuthorizationBinding,
        *,
        dispatch_claimed: bool = False,
        approval_permit: ApprovedTaskActionPermit | None = None,
    ) -> StepResult:
        """Reauthenticate and reauthorize immediately before the dispatcher call."""
        if not dispatch_claimed:
            self.store.claim_dispatch(binding)
        authorization = self.authorize(task_id, step, access_token)
        if (
            binding.task_id != task_id
            or binding.step_id != step.id
            or binding.device_id != authorization.device_id
            or binding.action_digest != authorization.action_digest
            or binding.authorization_fingerprint != authorization.fingerprint
        ):
            raise TaskAuthorizationError("Task execution is not authorized.")
        correlation = ToolExecutionCorrelation(
            task_id=binding.task_id,
            step_id=binding.step_id,
            action_digest=binding.action_digest,
            attempt_id=binding.attempt_id,
            attempt_number=binding.attempt_number,
            device_id=binding.device_id,
            authorization_fingerprint=binding.authorization_fingerprint,
        )
        try:
            result = await self.dispatcher.dispatch(
                step.action.to_tool_request(),
                authorization_context=authorization.tool_authorization,
                correlation=correlation,
                approval_permit=approval_permit,
            )
        except (AuthorizationDeniedError, ToolAuditError, ToolExecutionError):
            raise
        except Exception:
            # A failure after entering dispatch has no reliable effect boundary.
            raise
        return StepResult(data=result.output.model_dump(mode="json"))

    def inspect_approval_action(
        self, task_id: str, step: Step, access_token: SecretStr
    ) -> tuple[DispatchAuthorization, ToolDefinition]:
        """Freshly authorize and validate an action before creating durable approval."""
        authorization = self.authorize(task_id, step, access_token)
        if step.action is None:
            raise TaskAuthorizationError("Task execution is not authorized.")
        definition = self.dispatcher.inspect_authorized_request(
            step.action.to_tool_request(), authorization.tool_authorization
        )
        return authorization, definition

    def inspect_stored_approval_action(
        self, task_id: str, step: Step, device_id: str
    ) -> tuple[DispatchAuthorization, ToolDefinition]:
        """Recheck a persisted task device, current grant, definition, and input."""
        device = self.device_service.get_device(device_id)
        return self.inspect_approval_action_for_device(task_id, step, device)

    def inspect_approval_action_for_device(
        self, task_id: str, step: Step, device: Device
    ) -> tuple[DispatchAuthorization, ToolDefinition]:
        """Inspect an action using the DeviceStore identity held by a reservation."""
        if step.action is None:
            raise TaskAuthorizationError("Task execution is not authorized.")
        ownership = self.store.get_task_ownership(task_id)
        current = self.store.get_step(step.id)
        if (
            ownership is None
            or ownership.device_id != device.id
            or device.revoked_at is not None
            or current.task_id != task_id
            or current.action is None
            or current.action.digest() != step.action.digest()
        ):
            raise TaskAuthorizationError("Task execution is not authorized.")
        authorization = resolve_dispatch_authorization(device, current.action, self.policy)
        definition = self.dispatcher.inspect_authorized_request(
            current.action.to_tool_request(), authorization.tool_authorization
        )
        return authorization, definition
