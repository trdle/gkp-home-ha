"""Helpers shared by the GKP Home integration."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
import math
import re

from .const import DEVICE_TIME_FORMAT

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def tenths_f_to_celsius(value: int | float) -> float:
    """Convert a GKP temperature (tenths of °F) to °C, exactly like the app."""
    return math.floor((value - 320) * 0.5555555555555556) / 10


def celsius_to_tenths_f(value: float) -> int:
    """Convert °C to a GKP temperature (tenths of °F), exactly like the app."""
    return math.floor(value * 10 * 1.8 + 320)


def device_id_to_mac(device_id: str) -> str | None:
    """Return the radiator's WiFi MAC (``aa:bb:cc:dd:ee:ff``) from its device id."""
    if not _MAC_RE.match(device_id):
        return None
    lowered = device_id.lower()
    return ":".join(lowered[i : i + 2] for i in range(0, 12, 2))


def hex_ascii_to_str(value: str) -> str | None:
    """Decode a hex-encoded ASCII string such as the model code ``Ty``."""
    try:
        return bytes.fromhex(value).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None


def parse_device_time(value: str | None, zone: tzinfo) -> datetime | None:
    """Parse the radiator's own timestamp (``Dd``, ``dd/MM/yyyy HH:mm:ss``)."""
    if not value:
        return None
    try:
        naive = datetime.strptime(value, DEVICE_TIME_FORMAT)  # noqa: DTZ007 - zone added below
    except ValueError:
        return None
    return naive.replace(tzinfo=zone)


def device_time_age(reported: datetime, now: datetime) -> timedelta:
    """Return how long ago the radiator reported, tolerant of DST ambiguity.

    The radiator timestamp carries no UTC offset, so during the repeated hour at
    the end of daylight saving time both interpretations are considered and the
    smaller age is used.
    """
    ages = [abs(now - reported)]
    if reported.tzinfo is not None:
        ages.append(abs(now - reported.replace(fold=1 - reported.fold)))
    return min(ages)
