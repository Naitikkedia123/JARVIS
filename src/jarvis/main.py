"""Local JARVIS API foundation. Extend in tested phases."""

import logging
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis.config import Settings
from jarvis.devices.api import DeviceRequestSizeLimitMiddleware
from jarvis.devices.api import router as device_router
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
    OwnerCredentialRateLimitError,
)
from jarvis.logging import configure_logging
from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.policy.device_grants import LocalDeviceTaskGrantPolicy
from jarvis.policy.task_submission import LocalTaskSubmissionPolicy
from jarvis.tasks.api import TaskRequestSafetyMiddleware
from jarvis.tasks.api import router as task_router
from jarvis.tasks.authorization import TaskSubmissionPolicy
from jarvis.tasks.errors import (
    TaskAuthorizationError,
    TaskStoreError,
    TaskSubmissionConflictError,
    TaskSubmissionPolicyUnavailableError,
)
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.runner import DispatcherBackedTaskRunner
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore
from jarvis.tools.dispatcher import InMemoryToolAuditSink, ToolDispatcher
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.status import register_status_tool

settings = Settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TaskApplicationServices:
    """Private lifespan-owned dependencies; routes expose only scoped operations."""

    store: TaskStore
    runtime: TaskRuntime
    authorized_runner: DispatcherBackedTaskRunner | None
    submission_policy: TaskSubmissionPolicy


def _build_task_application_services(
    settings: Settings,
    device_service: DeviceService | None,
    store: TaskStore,
) -> _TaskApplicationServices:
    runner: DispatcherBackedTaskRunner | None = None
    if device_service is not None:
        grant_policy = LocalDeviceTaskGrantPolicy(settings.device_task_grants_path)
        registry = ToolRegistry()
        status_executor = register_status_tool(registry)
        dispatcher_policy = DeviceTaskToolPolicy(registry)
        dispatcher = ToolDispatcher(
            registry,
            dispatcher_policy,
            {"jarvis.status": status_executor},
            InMemoryToolAuditSink(),
        )
        runner = DispatcherBackedTaskRunner(
            device_service=device_service,
            store=store,
            dispatcher=dispatcher,
            policy=grant_policy,
        )

    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        runner=None,
        authorized_runner=runner,
        approval_lifetime_minutes=settings.approval_lifetime_minutes,
    )
    return _TaskApplicationServices(
        store=store,
        runtime=runtime,
        authorized_runner=runner,
        submission_policy=LocalTaskSubmissionPolicy(
            settings.task_submission_grants_path
        ),
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    del request, exc
    return _error_response(422, "invalid_request", "Request validation failed.")


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    # Record only the exception type. Messages and tracebacks may contain secrets.
    logger.error("Unhandled application error", extra={"exception_type": type(exc).__name__})
    return _error_response(500, "internal_error", "An internal error occurred.")


def create_app(
    app_settings: Settings | None = None,
    *,
    device_service: DeviceService | None = None,
) -> FastAPI:
    """Create the loopback API and manage local device and task lifecycles."""
    resolved_settings = app_settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        service = device_service
        device_store: DeviceStore | None = None
        resources = ExitStack()
        application.state.device_service = None
        application.state._task_services = None
        try:
            database_path: Path = resolved_settings.device_database_path
            # Do not create a local device database unless bootstrap provisioning
            # is configured or an existing database needs to be reopened.
            if service is None and (
                resolved_settings.device_bootstrap_code is not None
                or database_path.is_file()
            ):
                database_path.parent.mkdir(parents=True, exist_ok=True)
                device_store = DeviceStore(
                    database_path,
                    owner_credential_lifetime_days=(
                        resolved_settings.owner_credential_lifetime_days
                    ),
                )
                resources.callback(device_store.close)
                service = DeviceService(
                    device_store,
                    bootstrap_code=resolved_settings.device_bootstrap_code,
                )
            task_database_path = resolved_settings.task_database_path
            task_database_path.parent.mkdir(parents=True, exist_ok=True)
            task_store = TaskStore(task_database_path)
            resources.callback(task_store.close)
            task_services = _build_task_application_services(
                resolved_settings,
                service,
                task_store,
            )
            application.state.device_service = service
            application.state._task_services = task_services
            yield
        finally:
            application.state._task_services = None
            application.state.device_service = None
            resources.close()

    application = FastAPI(
        title="JARVIS",
        description="A model-independent personal AI operating system.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.add_middleware(DeviceRequestSizeLimitMiddleware)
    application.add_middleware(TaskRequestSafetyMiddleware)
    application.include_router(device_router)
    application.include_router(task_router)
    application.add_exception_handler(RequestValidationError, request_validation_error_handler)

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        messages = {
            400: "The request could not be completed.",
            401: "Authentication is required.",
            403: "Access is forbidden.",
            404: "The requested resource was not found.",
            413: "Request body exceeds the allowed size.",
            503: "The device service is temporarily unavailable.",
        }
        return _error_response(
            exc.status_code,
            "http_error",
            messages.get(exc.status_code, "The request could not be completed."),
            headers=exc.headers,
        )

    @application.exception_handler(DeviceAuthenticationError)
    async def device_authentication_error(
        request: Request, exc: DeviceAuthenticationError
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            401,
            "authentication_required",
            "Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @application.exception_handler(DeviceAuthorizationError)
    async def device_authorization_error(
        request: Request, exc: DeviceAuthorizationError
    ) -> JSONResponse:
        del request, exc
        return _error_response(403, "forbidden", "The device is not authorized for this operation.")

    @application.exception_handler(DevicePairingUnavailableError)
    async def device_pairing_error(
        request: Request, exc: DevicePairingUnavailableError
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            409, "pairing_unavailable", "The pairing operation could not be completed."
        )

    @application.exception_handler(DeviceNotFoundError)
    async def device_not_found_error(request: Request, exc: DeviceNotFoundError) -> JSONResponse:
        del request, exc
        return _error_response(404, "device_not_found", "The requested device was not found.")

    @application.exception_handler(DeviceStoreError)
    async def device_store_error(request: Request, exc: DeviceStoreError) -> JSONResponse:
        del request, exc
        return _error_response(
            503, "device_store_unavailable", "The device service is temporarily unavailable."
        )

    @application.exception_handler(OwnerCredentialRateLimitError)
    async def owner_credential_rate_limit(
        request: Request, exc: OwnerCredentialRateLimitError
    ) -> JSONResponse:
        del request
        return _error_response(
            429,
            "owner_credential_rate_limited",
            "Owner credential rotation is temporarily rate limited.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @application.exception_handler(TaskAuthorizationError)
    async def task_authorization_error(
        request: Request, exc: TaskAuthorizationError
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            403, "task_submission_denied", "Task submission is not authorized."
        )

    @application.exception_handler(TaskSubmissionConflictError)
    async def task_submission_conflict_error(
        request: Request, exc: TaskSubmissionConflictError
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            409,
            "task_submission_conflict",
            "The idempotency key conflicts with an earlier submission.",
        )

    @application.exception_handler(TaskSubmissionPolicyUnavailableError)
    async def task_submission_policy_unavailable(
        request: Request, exc: TaskSubmissionPolicyUnavailableError
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            503,
            "task_submission_unavailable",
            "Task submission is temporarily unavailable.",
        )

    @application.exception_handler(TaskStoreError)
    async def task_store_error(request: Request, exc: TaskStoreError) -> JSONResponse:
        del request, exc
        return _error_response(
            503,
            "task_store_unavailable",
            "Task submission is temporarily unavailable.",
        )

    application.add_exception_handler(Exception, unexpected_error_handler)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Return a minimal liveness result. This is not a readiness/security check."""
        return {"status": "ok", "service": "jarvis"}

    @application.get("/ready", tags=["system"])
    def readiness() -> dict[str, str]:
        """Report API readiness; this does not assert task execution availability."""
        return {"status": "ready", "service": "jarvis"}

    return application


app = create_app(settings)
