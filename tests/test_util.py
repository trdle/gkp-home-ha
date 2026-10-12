"""Tests for the conversion helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.gkp_home.util import (
    celsius_to_tenths_f,
    device_id_to_mac,
    device_time_age,
    hex_ascii_to_str,
    parse_device_time,
    tenths_f_to_celsius,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")


@pytest.mark.parametrize(
    ("tenths", "celsius"),
    [(700, 21.1), (680, 20.0), (590, 15.0), (464, 8.0), (752, 24.0)],
)
def test_tenths_f_to_celsius(tenths: int, celsius: float) -> None:
    assert tenths_f_to_celsius(tenths) == celsius


@pytest.mark.parametrize(
    ("celsius", "tenths"),
    [(20.0, 680), (15.0, 590), (8.0, 464), (24.0, 752), (21.1, 699)],
)
def test_celsius_to_tenths_f(celsius: float, tenths: int) -> None:
    assert celsius_to_tenths_f(celsius) == tenths


@pytest.mark.parametrize("celsius", [step / 2 for step in range(10, 61)])
def test_round_trip_half_degree_steps(celsius: float) -> None:
    assert tenths_f_to_celsius(celsius_to_tenths_f(celsius)) == celsius


def test_device_id_to_mac() -> None:
    assert device_id_to_mac("AABBCCDDEE01") == "aa:bb:cc:dd:ee:01"
    assert device_id_to_mac("aabbccddee01") == "aa:bb:cc:dd:ee:01"
    assert device_id_to_mac("not-a-mac") is None
    assert device_id_to_mac("AABBCCDDEE0") is None


def test_hex_ascii_to_str() -> None:
    assert hex_ascii_to_str("503038303733") == "P08073"
    assert hex_ascii_to_str("zz") is None
    assert hex_ascii_to_str("ff") is None


def test_parse_device_time() -> None:
    parsed = parse_device_time("12/10/2026 20:57:58", STOCKHOLM)
    assert parsed == datetime(2026, 10, 12, 20, 57, 58, tzinfo=STOCKHOLM)
    assert parsed.astimezone(UTC) == datetime(2026, 10, 12, 18, 57, 58, tzinfo=UTC)
    assert parse_device_time("2026-10-12 20:57:58", STOCKHOLM) is None
    assert parse_device_time("", STOCKHOLM) is None
    assert parse_device_time(None, STOCKHOLM) is None


def test_device_time_age() -> None:
    reported = parse_device_time("12/10/2026 20:57:58", STOCKHOLM)
    now = datetime(2026, 10, 12, 19, 7, 58, tzinfo=UTC)
    assert device_time_age(reported, now) == timedelta(minutes=10)


def test_device_time_age_during_dst_fallback() -> None:
    # 02:30 on 25 Oct 2026 happens twice in Stockholm (CEST then CET).
    reported = parse_device_time("25/10/2026 02:30:00", STOCKHOLM)
    now = datetime(2026, 10, 25, 1, 31, 0, tzinfo=UTC)  # 02:31 CET, second pass
    assert device_time_age(reported, now) == timedelta(minutes=1)
