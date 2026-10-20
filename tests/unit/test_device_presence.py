"""Conservative classification for persisted device authentication times."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jarvis.config import Settings
from jarvis.devices.models import (
    DEFAULT_DEVICE_PRESENCE_FRESHNESS_SECONDS,
    DevicePresence,
    classify_device_presence,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
FRESHNESS = timedelta(seconds=DEFAULT_DEVICE_PRESENCE_FRESHNESS_SECONDS)


def test_presence_includes_exact_freshness_boundary_and_normalizes_timezones() -> None:
    boundary = NOW - FRESHNESS
    other_timezone = boundary.astimezone(timezone(timedelta(hours=5, minutes=30)))

    assert classify_device_presence(
        other_timezone, now=NOW, freshness_window=FRESHNESS
    ) is DevicePresence.RECENTLY_SEEN
    assert classify_device_presence(
        boundary - timedelta(microseconds=1), now=NOW, freshness_window=FRESHNESS
    ) is DevicePresence.STALE


@pytest.mark.parametrize(
    "last_seen_at",
    [None, NOW + timedelta(microseconds=1), NOW.replace(tzinfo=None)],
)
def test_presence_is_unknown_without_a_usable_non_future_timestamp(last_seen_at) -> None:
    assert classify_device_presence(
        last_seen_at, now=NOW, freshness_window=FRESHNESS
    ) is DevicePresence.UNKNOWN


def test_presence_freshness_setting_defaults_to_five_minutes_and_is_bounded() -> None:
    assert Settings().device_presence_freshness_seconds == 300
    with pytest.raises(ValidationError):
        Settings(device_presence_freshness_seconds=0)
