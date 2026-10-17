"""Local JARVIS API foundation. Extend in tested phases."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis.config import Settings
from jarvis.logging import configure_logging

settings = Settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="JARVIS",
    description="A model-independent personal AI operating system (initial scaffold).",
    version="0.1.0",
)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    del request, exc
    return _error_response(422, "invalid_request", "Request validation failed.")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    del request
    messages = {
        400: "The request could not be completed.",
        401: "Authentication is required.",
        403: "Access is forbidden.",
        404: "The requested resource was not found.",
    }
    return _error_response(
        exc.status_code,
        "http_error",
        messages.get(exc.status_code, "The request could not be completed."),
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    # Record only the exception type. Messages and tracebacks may contain secrets.
    logger.error("Unhandled application error", extra={"exception_type": type(exc).__name__})
    return _error_response(500, "internal_error", "An internal error occurred.")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return a minimal liveness result. This is not a readiness/security check."""
    return {"status": "ok", "service": "jarvis"}


@app.get("/ready", tags=["system"])
def readiness() -> dict[str, str]:
    """Return deterministic readiness; this scaffold has no external dependencies."""
    return {"status": "ready", "service": "jarvis"}
