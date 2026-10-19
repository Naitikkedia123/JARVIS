"""Local JARVIS API foundation. Extend in tested phases."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
)
from jarvis.logging import configure_logging

settings = Settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


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
    """Create the loopback API and manage its optional local device store lifecycle."""
    resolved_settings = app_settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        service = device_service
        store: DeviceStore | None = None
        database_path: Path = resolved_settings.device_database_path
        # Do not create a local database on health/readiness-only starts unless
        # bootstrap provisioning is configured. Existing device databases reopen.
        if service is None and (
            resolved_settings.device_bootstrap_code is not None or database_path.is_file()
        ):
            database_path.parent.mkdir(parents=True, exist_ok=True)
            store = DeviceStore(database_path)
            try:
                service = DeviceService(
                    store,
                    bootstrap_code=resolved_settings.device_bootstrap_code,
                )
            except Exception:
                store.close()
                raise
        application.state.device_service = service
        try:
            yield
        finally:
            if store is not None:
                store.close()

    application = FastAPI(
        title="JARVIS",
        description="A model-independent personal AI operating system.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(DeviceRequestSizeLimitMiddleware)
    application.include_router(device_router)
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

    application.add_exception_handler(Exception, unexpected_error_handler)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        """Return a minimal liveness result. This is not a readiness/security check."""
        return {"status": "ok", "service": "jarvis"}

    @application.get("/ready", tags=["system"])
    def readiness() -> dict[str, str]:
        """Return deterministic readiness; this scaffold has no external dependencies."""
        return {"status": "ready", "service": "jarvis"}

    return application


app = create_app(settings)
