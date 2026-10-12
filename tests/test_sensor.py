"""Tests for the GKP Home diagnostic sensors."""

from __future__ import annotations

from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gkp_home.sensor import GkpHomeSensor

from .conftest import FROZEN_NOW, KITCHEN, kitchen_content, register_cloud


async def test_sensors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    power = hass.states.get("sensor.kitchen_rated_power")
    assert power.state == "1000"
    assert power.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfPower.WATT
    assert power.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.POWER
    assert ATTR_STATE_CLASS not in power.attributes
    assert hass.states.get("sensor.office_rated_power").state == "600"

    assert hass.states.get("sensor.kitchen_firmware").state == "v01.04.00"

    last_report = hass.states.get("sensor.kitchen_last_report")
    assert last_report.state == "2026-10-12T18:57:58+00:00"
    assert last_report.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TIMESTAMP
    assert (
        hass.states.get("sensor.office_last_report").state
        == "2026-10-12T17:30:00+00:00"
    )

    for key in ("rated_power", "firmware", "last_report"):
        entry = entity_registry.async_get(f"sensor.kitchen_{key}")
        assert entry.unique_id == f"{KITCHEN}_{key}"
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert entry.disabled_by is None

    # The setpoint sensors exist but are disabled by default.
    for key in ("eco_setpoint", "comfort_setpoint", "frost_setpoint"):
        entry = entity_registry.async_get(f"sensor.kitchen_{key}")
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category is EntityCategory.DIAGNOSTIC
        assert hass.states.get(f"sensor.kitchen_{key}") is None


async def test_setpoint_sensors_when_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    mock_config_entry.add_to_hass(hass)
    with patch.object(GkpHomeSensor, "entity_registry_enabled_default", True):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    for key, value in (
        ("eco_setpoint", "15.0"),
        ("comfort_setpoint", "20.0"),
        ("frost_setpoint", "8.0"),
    ):
        state = hass.states.get(f"sensor.kitchen_{key}")
        assert state.state == value
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTemperature.CELSIUS
        assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.TEMPERATURE


async def test_sensors_with_missing_values(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content(Dd="not a date", Sv="")
    del kitchen["Pw"]
    register_cloud(aioclient_mock, kitchen={"json": kitchen})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_rated_power").state == STATE_UNKNOWN
    assert hass.states.get("sensor.kitchen_firmware").state == STATE_UNKNOWN
    assert hass.states.get("sensor.kitchen_last_report").state == STATE_UNKNOWN
