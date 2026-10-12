"""Tests for the GKP Home climate entity."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_TARGET_TEMP_STEP,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import (
    FROZEN_NOW,
    KITCHEN,
    OFFICE,
    config_writes,
    content_reads,
    envelope,
    kitchen_content,
    office_content,
    register_cloud,
)
from .helpers import TOKEN_URL

KITCHEN_ENTITY = "climate.kitchen"
OFFICE_ENTITY = "climate.office"


async def fire_write_refresh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Let the refresh scheduled after a write run."""
    freezer.tick(timedelta(seconds=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def set_hvac_mode(hass: HomeAssistant, entity_id: str, mode: HVACMode) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_HVAC_MODE: mode},
        blocking=True,
    )


async def set_preset(hass: HomeAssistant, entity_id: str, preset: str) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: preset},
        blocking=True,
    )


async def set_temperature(hass: HomeAssistant, entity_id: str, **data: object) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id, **data},
        blocking=True,
    )


async def test_climate_entities(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    kitchen = hass.states.get(KITCHEN_ENTITY)
    assert kitchen is not None
    assert kitchen.state == HVACMode.HEAT  # frost protection is a manual heating mode
    attributes = kitchen.attributes
    assert attributes[ATTR_FRIENDLY_NAME] == "Kitchen"
    assert attributes[ATTR_CURRENT_TEMPERATURE] == 21.1
    assert attributes[ATTR_TEMPERATURE] == 8.0
    assert attributes[ATTR_PRESET_MODE] == "frost_protection"
    assert attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    assert attributes[ATTR_PRESET_MODES] == [
        "comfort",
        "eco",
        "frost_protection",
        "boost",
    ]
    assert attributes[ATTR_MIN_TEMP] == 5.0
    assert attributes[ATTR_MAX_TEMP] == 30.0
    assert attributes[ATTR_TARGET_TEMP_STEP] == 0.5
    assert attributes[ATTR_SUPPORTED_FEATURES] == (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    assert attributes["demanded_mode"] == 4
    assert attributes["current_mode"] == 4
    assert attributes["boost_minutes"] == 120
    assert attributes["room"] == "Kitchen"
    assert attributes["home"] == "MyHouse"

    office = hass.states.get(OFFICE_ENTITY)
    assert office is not None
    assert office.state == HVACMode.HEAT
    assert office.attributes[ATTR_FRIENDLY_NAME] == "Office"
    assert office.attributes[ATTR_CURRENT_TEMPERATURE] == 19.0
    assert office.attributes[ATTR_TEMPERATURE] == 21.0
    assert office.attributes[ATTR_PRESET_MODE] == "comfort"
    assert office.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert office.attributes["boost_minutes"] is None

    entry = entity_registry.async_get(KITCHEN_ENTITY)
    assert entry is not None
    assert entry.unique_id == KITCHEN
    assert entry.translation_key == "radiator"
    assert entry.original_name is None


@pytest.mark.parametrize(
    ("hvac_mode", "mode", "expected_state"),
    [
        (HVACMode.OFF, 0, "off"),
        (HVACMode.AUTO, 2, "auto"),
        (HVACMode.HEAT, 3, "heat"),
    ],
)
async def test_set_hvac_mode(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hvac_mode: HVACMode,
    mode: int,
    expected_state: str,
) -> None:
    reads_before = content_reads(mock_cloud, KITCHEN)

    await set_hvac_mode(hass, KITCHEN_ENTITY, hvac_mode)

    assert config_writes(mock_cloud) == [
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": mode}])
    ]
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.state == expected_state  # applied locally right away
    assert state.attributes["demanded_mode"] == mode
    if hvac_mode == HVACMode.HEAT:
        assert state.attributes[ATTR_PRESET_MODE] == "comfort"
        assert state.attributes[ATTR_TEMPERATURE] == 20.0
    if hvac_mode == HVACMode.OFF:
        assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.OFF
        assert state.attributes[ATTR_PRESET_MODE] is None

    # A refresh is requested about 3 s after the write and restores the cloud state.
    assert content_reads(mock_cloud, KITCHEN) == reads_before
    await fire_write_refresh(hass, freezer)
    assert content_reads(mock_cloud, KITCHEN) == reads_before + 1
    assert hass.states.get(KITCHEN_ENTITY).state == HVACMode.HEAT


async def test_set_preset_mode(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await set_preset(hass, KITCHEN_ENTITY, "eco")
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_PRESET_MODE] == "eco"
    assert state.attributes[ATTR_TEMPERATURE] == 15.0

    await set_preset(hass, KITCHEN_ENTITY, "boost")  # the kitchen reports Bt 120
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.attributes[ATTR_PRESET_MODE] == "boost"
    assert state.attributes[ATTR_TEMPERATURE] == 24.0
    assert state.attributes["boost_minutes"] == 120

    await set_preset(hass, OFFICE_ENTITY, "boost")  # no Bt reported: default 60
    assert hass.states.get(OFFICE_ENTITY).attributes["boost_minutes"] == 60

    await set_preset(hass, KITCHEN_ENTITY, "comfort")
    await set_preset(hass, KITCHEN_ENTITY, "frost_protection")

    assert config_writes(mock_cloud) == [
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 1}]),
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 5},
                {"wattsType": "Bt", "wattsTypeValue": 120},
            ],
        ),
        envelope(
            OFFICE,
            [
                {"wattsType": "Dm", "wattsTypeValue": 5},
                {"wattsType": "Bt", "wattsTypeValue": 60},
            ],
        ),
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 3}]),
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 4}]),
    ]

    with pytest.raises(ServiceValidationError):
        await set_preset(hass, KITCHEN_ENTITY, "away")

    await fire_write_refresh(hass, freezer)


async def test_set_temperature(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # In frost protection the frost setpoint is written.
    await set_temperature(hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 20.0})
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.attributes[ATTR_TEMPERATURE] == 20.0
    assert state.attributes[ATTR_PRESET_MODE] == "frost_protection"

    # In comfort the comfort setpoint is written.
    await set_temperature(hass, OFFICE_ENTITY, **{ATTR_TEMPERATURE: 22.5})
    assert hass.states.get(OFFICE_ENTITY).attributes[ATTR_TEMPERATURE] == 22.5

    # Off (and auto) have no setpoint: switch to comfort, like a HomeKit thermostat.
    await set_hvac_mode(hass, KITCHEN_ENTITY, HVACMode.OFF)
    await set_temperature(hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 21.0})
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_PRESET_MODE] == "comfort"
    assert state.attributes[ATTR_TEMPERATURE] == 21.0

    await set_hvac_mode(hass, KITCHEN_ENTITY, HVACMode.AUTO)
    await set_temperature(hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 19.0})
    assert hass.states.get(KITCHEN_ENTITY).state == HVACMode.HEAT

    # A mode passed along with the temperature is honoured.
    await set_temperature(
        hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 18.0, ATTR_HVAC_MODE: HVACMode.HEAT}
    )
    await set_temperature(
        hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 17.5, ATTR_HVAC_MODE: HVACMode.OFF}
    )
    assert hass.states.get(KITCHEN_ENTITY).state == HVACMode.HEAT

    assert config_writes(mock_cloud) == [
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 4},
                {"wattsType": "df", "wattsTypeValue": 680},
            ],
        ),
        envelope(
            OFFICE,
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 725},
            ],
        ),
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 0}]),
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 698},
            ],
        ),
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 2}]),
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 662},
            ],
        ),
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 644},
            ],
        ),
        envelope(
            KITCHEN,
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 635},
            ],
        ),
    ]

    await fire_write_refresh(hass, freezer)
    assert hass.states.get(KITCHEN_ENTITY).attributes[ATTR_TEMPERATURE] == 8.0


async def test_set_temperature_out_of_range(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
) -> None:
    with pytest.raises(ServiceValidationError):
        await set_temperature(hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 35.0})
    assert config_writes(mock_cloud) == []


async def test_turn_on_off(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: KITCHEN_ENTITY},
        blocking=True,
    )
    assert hass.states.get(KITCHEN_ENTITY).state == HVACMode.OFF
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: KITCHEN_ENTITY}, blocking=True
    )
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_PRESET_MODE] == "comfort"
    assert config_writes(mock_cloud) == [
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 0}]),
        envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 3}]),
    ]
    await fire_write_refresh(hass, freezer)


async def test_write_failure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    register_cloud(
        mock_cloud, config_replace={"status": 500, "text": "Internal Server Error"}
    )

    with pytest.raises(HomeAssistantError, match="Could not send the command"):
        await set_hvac_mode(hass, KITCHEN_ENTITY, HVACMode.OFF)
    with pytest.raises(HomeAssistantError, match="Could not send the command"):
        await set_temperature(hass, KITCHEN_ENTITY, **{ATTR_TEMPERATURE: 20.0})

    # Nothing is applied locally when the cloud rejected the write.
    state = hass.states.get(KITCHEN_ENTITY)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == 8.0


async def test_write_auth_failure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
) -> None:
    mock_cloud.clear_requests()
    mock_cloud.post(TOKEN_URL, status=400, text='{"error":"invalid_grant"}')
    register_cloud(mock_cloud, config_replace={"status": 401, "text": "Unauthorized"})

    with pytest.raises(HomeAssistantError, match="rejected the saved session"):
        await set_hvac_mode(hass, KITCHEN_ENTITY, HVACMode.OFF)
    assert hass.states.get(KITCHEN_ENTITY).state == HVACMode.HEAT


async def test_hvac_action_without_heating_flag(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without Hs the action is derived from the temperatures."""
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content(Dm=3, Cm=3, At=464)  # 8.0 °C, comfort setpoint 20.0 °C
    del kitchen["Hs"]
    office = office_content(Dm=3, Cm=3, At=700)  # 21.1 °C, comfort setpoint 21.0 °C
    del office["Hs"]
    register_cloud(aioclient_mock, kitchen={"json": kitchen}, office={"json": office})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert (
        hass.states.get(KITCHEN_ENTITY).attributes[ATTR_HVAC_ACTION]
        == HVACAction.HEATING
    )
    assert (
        hass.states.get(OFFICE_ENTITY).attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    )


async def test_unknown_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content()
    del kitchen["Dm"]
    office = office_content(Dm=9)
    register_cloud(aioclient_mock, kitchen={"json": kitchen}, office={"json": office})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    kitchen_state = hass.states.get(KITCHEN_ENTITY)
    assert kitchen_state.state == STATE_UNKNOWN
    assert kitchen_state.attributes[ATTR_PRESET_MODE] is None
    assert kitchen_state.attributes.get(ATTR_HVAC_ACTION) is None
    assert kitchen_state.attributes[ATTR_TEMPERATURE] == 8.0  # falls back to Sp
    office_state = hass.states.get(OFFICE_ENTITY)
    assert office_state.state == HVACMode.HEAT
    assert office_state.attributes[ATTR_PRESET_MODE] is None
    assert office_state.attributes[ATTR_TEMPERATURE] == 21.0  # Sp
