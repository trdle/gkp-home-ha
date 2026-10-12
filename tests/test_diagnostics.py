"""Tests for the GKP Home diagnostics."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gkp_home.const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
)
from custom_components.gkp_home.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)

    assert diagnostics["entry"] == {
        "title": REDACTED,
        "unique_id": REDACTED,
        "data": {
            CONF_EMAIL: REDACTED,
            CONF_SUB: REDACTED,
            CONF_REFRESH_TOKEN: REDACTED,
            CONF_CLIENT_SECRET: REDACTED,
            CONF_APIM_KEY: REDACTED,
        },
        "options": {},
    }
    assert diagnostics["poll_interval_seconds"] == 60
    assert diagnostics["last_update_success"] is True

    (home,) = diagnostics["homes"]
    assert home["id"] == REDACTED
    assert home["name"] == "MyHouse"
    assert [room["name"] for room in home["rooms"].values()] == ["Kitchen", "Office"]

    kitchen, office = diagnostics["radiators"]
    assert kitchen["id"] == REDACTED
    assert kitchen["home_id"] == REDACTED
    assert kitchen["room_name"] == "Kitchen"
    assert kitchen["model"] == "P08073"
    assert kitchen["online"] is True
    assert kitchen["stale"] is False
    assert kitchen["last_report"] == "2026-10-12T20:57:58+02:00"
    assert kitchen["content"]["At"] == 700
    assert office["online"] is False

    (raw,) = diagnostics["raw_installations"]
    assert raw["id"] == REDACTED
    assert raw["In"] == REDACTED
    assert raw["It"] == REDACTED
    assert raw["A1"] == REDACTED
    assert raw["Il"] == "MyHouse"
    assert raw["rooms"][0]["Zt"] == REDACTED
    assert raw["rooms"][0]["Rn"] == "Kitchen"
    assert raw["rooms"][0]["devices"][0]["Id_deviceId"] == "AABBCCDDEE01"

    # The secrets never appear anywhere in the output.
    text = str(diagnostics)
    for secret in (
        "refresh-token",
        "dummy-client-secret",
        "dummy-apim-key",
        "user@example.com",
        "USER000001",
    ):
        assert secret not in text
