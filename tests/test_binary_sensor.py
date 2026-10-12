"""Tests for the GKP Home binary sensors."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import FROZEN_NOW, KITCHEN, kitchen_content, register_cloud


async def test_binary_sensors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    online = hass.states.get("binary_sensor.kitchen_online")
    assert online.state == STATE_ON  # reported 2 minutes ago
    assert online.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.CONNECTIVITY
    assert (
        hass.states.get("binary_sensor.office_online").state == STATE_OFF
    )  # 90 minutes ago

    heating = hass.states.get("binary_sensor.kitchen_heating")
    assert heating.state == STATE_OFF
    assert heating.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.HEAT
    assert hass.states.get("binary_sensor.office_heating").state == STATE_ON

    online_entry = entity_registry.async_get("binary_sensor.kitchen_online")
    assert online_entry.unique_id == f"{KITCHEN}_online"
    assert online_entry.entity_category is EntityCategory.DIAGNOSTIC
    heating_entry = entity_registry.async_get("binary_sensor.kitchen_heating")
    assert heating_entry.unique_id == f"{KITCHEN}_heating"
    assert heating_entry.entity_category is None


async def test_online_follows_the_device_timestamp(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    assert hass.states.get("binary_sensor.kitchen_online").state == STATE_ON

    # The cloud keeps returning the same (now 42 minutes old) timestamp.
    freezer.tick(timedelta(minutes=40))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("binary_sensor.kitchen_online").state == STATE_OFF


async def test_heating_unknown_without_flag(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content()
    del kitchen["Hs"]
    del kitchen["Dd"]
    register_cloud(aioclient_mock, kitchen={"json": kitchen})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.kitchen_heating").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.kitchen_online").state == STATE_OFF
