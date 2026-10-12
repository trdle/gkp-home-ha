"""Tests for the coordinator, devices and areas."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import ATTR_CURRENT_TEMPERATURE, HVACMode
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gkp_home.const import DOMAIN
from custom_components.gkp_home.coordinator import Radiator, parse_installations

from .conftest import (
    INSTALLATION_ID,
    KITCHEN,
    OFFICE,
    calls,
    content_reads,
    kitchen_content,
    register_cloud,
)
from .helpers import INSTALLATIONS_URL, TOKEN_URL

STOCKHOLM = ZoneInfo("Europe/Stockholm")


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance past the poll interval so the coordinator refreshes."""
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_devices_and_areas(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    kitchen = device_registry.async_get_device_by_identifier(
        (DOMAIN, KITCHEN), init_integration.entry_id
    )
    assert kitchen is not None
    assert kitchen.connections == {(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:01")}
    assert kitchen.manufacturer == "Gnosjö Klimatprodukter (Watts)"
    assert kitchen.model == "P08073"
    assert kitchen.sw_version == "v01.04.00"
    assert kitchen.name == "Kitchen"
    kitchen_area = area_registry.async_get_area_by_name("Kitchen")
    assert kitchen_area is not None
    assert kitchen.area_id == kitchen_area.id

    office = device_registry.async_get_device_by_identifier(
        (DOMAIN, OFFICE), init_integration.entry_id
    )
    assert office is not None
    assert office.name == "Office"  # trailing whitespace trimmed
    assert office.area_id == area_registry.async_get_area_by_name("Office").id
    assert (
        len(
            dr.async_entries_for_config_entry(
                device_registry, init_integration.entry_id
            )
        )
        == 2
    )


async def test_polling(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    assert content_reads(mock_cloud, KITCHEN) == 1
    await poll(hass, freezer)
    assert content_reads(mock_cloud, KITCHEN) == 2
    assert content_reads(mock_cloud, OFFICE) == 2
    assert len(calls(mock_cloud, "PUT", "/iotmanagement/v1/devices/userconnected")) == 2
    # The access token is reused while it is valid.
    assert len(calls(mock_cloud, "POST", "/connect/token")) == 1


async def test_content_failure_keeps_previous_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    register_cloud(mock_cloud, kitchen={"status": 500, "text": "Internal Server Error"})
    await poll(hass, freezer)

    coordinator = init_integration.runtime_data
    assert coordinator.last_update_success
    state = hass.states.get("climate.kitchen")
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21.1
    assert coordinator.data.radiators[KITCHEN].stale is True
    assert coordinator.data.radiators[OFFICE].stale is False

    # Once the cloud answers again the state is fresh.
    mock_cloud.clear_requests()
    register_cloud(mock_cloud, kitchen={"json": kitchen_content(At=690)})
    await poll(hass, freezer)
    assert coordinator.data.radiators[KITCHEN].stale is False
    assert (
        hass.states.get("climate.kitchen").attributes[ATTR_CURRENT_TEMPERATURE] == 20.5
    )


async def test_update_failure_and_recovery(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    mock_cloud.get(INSTALLATIONS_URL, status=503, text="Service Unavailable")
    await poll(hass, freezer)

    assert not init_integration.runtime_data.last_update_success
    assert hass.states.get("climate.kitchen").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.kitchen_rated_power").state == STATE_UNAVAILABLE

    mock_cloud.clear_requests()
    register_cloud(mock_cloud)
    await poll(hass, freezer)
    assert hass.states.get("climate.kitchen").state == HVACMode.HEAT


async def test_radiator_removed_from_cloud(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    register_cloud(mock_cloud)
    installations = [
        {
            "In": INSTALLATION_ID,
            "Il": "MyHouse",
            "rooms": [
                {
                    "Zn": "11111111-1111-1111-1111-111111111111",
                    "Rn": "Kitchen",
                    "devices": [{"Id_deviceId": KITCHEN, "Ty": "503038303733"}],
                }
            ],
        }
    ]
    mock_cloud.clear_requests()
    mock_cloud.get(INSTALLATIONS_URL, json=installations)
    register_cloud(mock_cloud)
    await poll(hass, freezer)

    assert hass.states.get("climate.kitchen").state == HVACMode.HEAT
    assert hass.states.get("climate.office").state == STATE_UNAVAILABLE


async def test_auth_failure_during_update_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    mock_cloud.post(TOKEN_URL, status=400, text='{"error":"invalid_grant"}')
    mock_cloud.get(INSTALLATIONS_URL, status=401, text="Unauthorized")
    await poll(hass, freezer)

    assert hass.states.get("climate.kitchen").state == STATE_UNAVAILABLE
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_auth_failure_reading_content_starts_reauth(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_cloud.clear_requests()
    mock_cloud.post(TOKEN_URL, status=400, text='{"error":"invalid_grant"}')
    register_cloud(mock_cloud, kitchen={"status": 401, "text": "Unauthorized"})
    await poll(hass, freezer)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


def test_parse_installations_edge_cases() -> None:
    homes, devices = parse_installations(
        [
            {"Il": "No id"},
            {
                "In": "inst-1",
                "Il": "  House  ",
                "rooms": [
                    {"Rn": "   ", "devices": [{"Id_deviceId": "AABBCCDDEE11"}]},
                    {
                        "Zn": "room-2",
                        "Rn": "Hall",
                        "devices": [{"Dn": "no id"}, None][:1],
                    },
                    {"Zn": "room-3", "Rn": "Attic"},
                ],
            },
            {"id": "inst-2", "rooms": None},
        ]
    )
    assert list(homes) == ["inst-1", "inst-2"]
    assert homes["inst-1"].name == "House"
    assert homes["inst-2"].name == "GKP Home"
    assert [room.name for room in homes["inst-1"].rooms.values()] == [
        "Room 1",
        "Hall",
        "Attic",
    ]
    assert homes["inst-1"].rooms["inst-1-0"].radiator_ids == ["AABBCCDDEE11"]
    assert [device.id for device in devices] == ["AABBCCDDEE11"]
    assert devices[0].room_name == "Room 1"
    assert devices[0].home_name == "House"


def test_radiator_properties() -> None:
    radiator = Radiator(
        id="AABBCCDDEE01",
        home_id="h",
        home_name="H",
        room_id="r",
        room_name="Kitchen",
        content={"At": "700", "Dm": 2.0, "Sp": 680, "Hs": True, "Pw": None, "Sv": 104},
        model=None,
        last_report=None,
    )
    assert radiator.name == "Kitchen"
    assert radiator.current_temperature == 21.1
    assert radiator.demanded_mode == 2
    assert radiator.target_temperature == 20.0  # auto has no setpoint key
    assert radiator.setpoint_for_mode(2) is None
    assert radiator.setpoint_for_mode(None) is None
    assert radiator.heating is True
    assert radiator.rated_power is None
    assert radiator.firmware == "104"
    assert radiator.boost_minutes is None
    assert radiator.is_online(None) is False
