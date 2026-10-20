"""Interactive host-local recovery for a lost or expired owner credential."""

from __future__ import annotations

import hashlib
import secrets
import sys
from pathlib import Path
from typing import TextIO

from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.devices.models import DeviceRole, OwnerCredentialReceipt
from jarvis.devices.store import DeviceNotFoundError, DeviceStore, DeviceStoreError

_CONFIRMATION = "RECOVER OWNER"


def recover_owner_credential(
    database_path: Path,
    *,
    lifetime_days: int = 30,
) -> OwnerCredentialReceipt:
    """Replace the owner bearer locally; OS access to the configured DB is the trust anchor."""
    replacement = secrets.token_urlsafe(32)
    store = DeviceStore(
        database_path,
        owner_credential_lifetime_days=lifetime_days,
    )
    try:
        owner = next(
            (
                device
                for device in store.list_devices()
                if device.role is DeviceRole.OWNER and device.revoked_at is None
            ),
            None,
        )
        if owner is None:
            raise DeviceNotFoundError("Owner device was not found.")
        expires_at = store.recover_owner_credential(
            hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        )
        return OwnerCredentialReceipt(
            device=store.get_device(owner.id),
            access_token=SecretStr(replacement),
            expires_at=expires_at,
        )
    finally:
        store.close()


def run_recovery(
    *,
    settings: Settings | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Require an interactive local operator and reveal the replacement once."""
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    if not input_stream.isatty() or not output_stream.isatty():
        print("Owner recovery requires an interactive local terminal.", file=error_stream)
        return 2

    print(
        "This replaces the owner credential in the configured local device database.",
        file=output_stream,
    )
    try:
        confirmation = input_stream.readline().strip()
    except (OSError, UnicodeError):
        print("Owner recovery could not be confirmed.", file=error_stream)
        return 2
    if confirmation != _CONFIRMATION:
        print("Owner recovery cancelled.", file=output_stream)
        return 1

    resolved_settings = settings or Settings()
    try:
        receipt = recover_owner_credential(
            resolved_settings.device_database_path,
            lifetime_days=resolved_settings.owner_credential_lifetime_days,
        )
    except (DeviceNotFoundError, DeviceStoreError, OSError, ValueError):
        print("Owner recovery failed.", file=error_stream)
        return 1

    print(f"Expires at: {receipt.expires_at.isoformat()}", file=output_stream)
    print(
        "Warning: this token is secret. Confirm this terminal is private and not being recorded. "
        "Terminal output contains the token. Do not capture it in shell transcripts, logs, "
        "screenshots, or shared recordings.",
        file=output_stream,
    )
    print("The token will be shown once; store it securely:", file=output_stream)
    print(receipt.access_token.get_secret_value(), file=output_stream)
    return 0


def main() -> int:
    return run_recovery()


if __name__ == "__main__":
    raise SystemExit(main())
