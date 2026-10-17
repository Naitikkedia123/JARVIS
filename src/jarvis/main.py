"""Minimal local JARVIS API scaffold. Extend in tested phases."""

from fastapi import FastAPI

app = FastAPI(
    title="JARVIS",
    description="A model-independent personal AI operating system (initial scaffold).",
    version="0.1.0",
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Return a minimal liveness result. This is not a readiness/security check."""
    return {"status": "ok", "service": "jarvis"}
