import http.client
import io
import json
import logging
import socket
import threading

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis.config import Settings
from jarvis.logging import JsonRedactingFormatter, configure_logging
from jarvis.main import app, request_validation_error_handler, unexpected_error_handler


def test_settings_default_to_loopback_and_local_development(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JARVIS_ENV", "JARVIS_HOST", "JARVIS_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.environment == "development"
    assert settings.host == "127.0.0.1"
    assert settings.log_level == "INFO"


def test_environment_setting_uses_existing_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_ENV", "test")

    assert Settings().environment == "test"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_settings_reject_non_loopback_hosts(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("JARVIS_HOST", host)

    with pytest.raises(ValidationError):
        Settings()


def test_json_logging_redacts_message_and_structured_secret_values() -> None:
    secrets = {
        "fake-password-value",
        "fake-bearer-token-value",
        "fake-api-key-value",
        "fake-cookie-value",
        "fake-auth-header-value",
    }
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonRedactingFormatter())
    logger = logging.getLogger("jarvis.test.redaction")
    previous_handlers = logger.handlers[:]
    previous_propagate = logger.propagate
    logger.handlers = [handler]
    logger.propagate = False
    try:
        logger.warning(
            "Login password=fake-password-value Authorization: Bearer fake-bearer-token-value",
            extra={
                "api_key": "fake-api-key-value",
                "headers": {
                    "Cookie": "fake-cookie-value",
                    "Authorization": "Bearer fake-auth-header-value",
                },
                "nested": [
                    {"PaSsWoRd": "fake-nested-password"},
                    {"API-KEY": "fake-nested-api-key"},
                    "Bearer fake-list-bearer-token",
                    "access_token=fake-case-token",
                ],
            },
        )
    finally:
        logger.handlers = previous_handlers
        logger.propagate = previous_propagate

    output = stream.getvalue()
    for secret in secrets:
        assert secret not in output
    for secret in (
        "fake-nested-password",
        "fake-nested-api-key",
        "fake-list-bearer-token",
        "fake-case-token",
    ):
        assert secret not in output
    assert "[REDACTED]" in output
    assert json.loads(output)["level"] == "WARNING"


def test_uvicorn_error_logging_omits_exception_message_and_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_logger = logging.getLogger()
    error_logger = logging.getLogger("uvicorn.error")
    original_root_handlers = root_logger.handlers[:]
    original_root_level = root_logger.level
    original_error_handlers = error_logger.handlers[:]
    original_error_propagate = error_logger.propagate
    original_error_disabled = error_logger.disabled
    original_error_level = error_logger.level
    try:
        configure_logging("INFO")
        capture = io.StringIO()
        root_logger.handlers[0].stream = capture

        assert error_logger.handlers == []
        assert error_logger.propagate

        try:
            raise RuntimeError("fake-uvicorn-exception-secret")
        except RuntimeError:
            error_logger.exception("Unhandled ASGI application exception")

        output = capture.getvalue()
        assert "fake-uvicorn-exception-secret" not in output
        assert "Traceback" not in output
        assert "RuntimeError" not in output
        assert "Unhandled ASGI application exception" in output
    finally:
        root_logger.handlers = original_root_handlers
        root_logger.setLevel(original_root_level)
        error_logger.handlers = original_error_handlers
        error_logger.propagate = original_error_propagate
        error_logger.disabled = original_error_disabled
        error_logger.setLevel(original_error_level)


def test_real_uvicorn_request_does_not_log_unhandled_exception_details() -> None:
    secret = "fake-real-uvicorn-secret-7e84a"
    raw_exception_message = f"sensitive failure {secret}"
    test_app = FastAPI()
    test_app.add_exception_handler(Exception, unexpected_error_handler)

    @test_app.get("/test-only-failure")
    def fail() -> None:
        raise RuntimeError(raw_exception_message)

    root_logger = logging.getLogger()
    error_logger = logging.getLogger("uvicorn.error")
    access_logger = logging.getLogger("uvicorn.access")
    original_root_handlers = root_logger.handlers[:]
    original_root_level = root_logger.level
    original_error_handlers = error_logger.handlers[:]
    original_error_propagate = error_logger.propagate
    original_error_disabled = error_logger.disabled
    original_error_level = error_logger.level
    original_access_disabled = access_logger.disabled
    original_access_propagate = access_logger.propagate
    server_started = threading.Event()
    capture = io.StringIO()
    server_thread: threading.Thread | None = None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    class SignalingServer(uvicorn.Server):
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets=sockets)
            if self.started:
                server_started.set()

    try:
        configure_logging("INFO")
        root_logger.handlers[0].stream = capture

        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = SignalingServer(
            uvicorn.Config(test_app, log_config=None, access_log=False)
        )
        server_thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="test-uvicorn-server",
            daemon=True,
        )
        server_thread.start()
        assert server_started.wait(timeout=5), "Uvicorn server did not finish startup"

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/test-only-failure")
            response = connection.getresponse()
            response_body = response.read().decode("utf-8")
            response_status = response.status
        finally:
            connection.close()

        assert response_status == 500
        assert json.loads(response_body) == {
            "error": {"code": "internal_error", "message": "An internal error occurred."}
        }
        assert secret not in response_body
        assert raw_exception_message not in response_body

        emitted_logs = capture.getvalue()
        assert '"logger":"uvicorn.error"' in emitted_logs
        assert secret not in emitted_logs
        assert raw_exception_message not in emitted_logs
        assert "Traceback" not in emitted_logs
    finally:
        if server_thread is not None:
            server.should_exit = True
            server_thread.join(timeout=5)
        listener.close()
        root_logger.handlers = original_root_handlers
        root_logger.setLevel(original_root_level)
        error_logger.handlers = original_error_handlers
        error_logger.propagate = original_error_propagate
        error_logger.disabled = original_error_disabled
        error_logger.setLevel(original_error_level)
        access_logger.disabled = original_access_disabled
        access_logger.propagate = original_access_propagate

        if server_thread is not None:
            assert not server_thread.is_alive(), "Uvicorn test server did not shut down"


def test_uvicorn_access_logging_is_disabled_to_avoid_request_target_leaks() -> None:
    assert logging.getLogger("uvicorn.access").disabled


def test_unexpected_errors_return_generic_client_response() -> None:
    secret = "fake-internal-secret-value"
    test_app = FastAPI()
    test_app.add_exception_handler(Exception, unexpected_error_handler)

    @test_app.get("/failure")
    def fail() -> None:
        raise RuntimeError(secret)

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/failure")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    assert secret not in response.text


def test_http_errors_use_standardized_response() -> None:
    with TestClient(app) as client:
        response = client.get("/missing-resource")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "http_error",
            "message": "The requested resource was not found.",
        }
    }


def test_validation_errors_do_not_echo_invalid_values() -> None:
    secret = "fake-validation-secret-value"
    test_app = FastAPI()
    test_app.add_exception_handler(RequestValidationError, request_validation_error_handler)

    @test_app.get("/items")
    def list_items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    with TestClient(test_app) as client:
        response = client.get("/items", params={"limit": secret})

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Request validation failed."}
    }
    assert secret not in response.text
