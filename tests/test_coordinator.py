"""Tests for the coordinator, devices and areas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_PRESET_MODE,
    HVACMode,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gkp_home.const import CONF_WAKE_INTERVAL, DOMAIN
from custom_components.gkp_home.coordinator import (
    Radiator,
    normalize_content,
    parse_installations,
    report_time,
)

from .conftest import (
    FROZEN_NOW,
    INSTALLATION_ID,
    KITCHEN,
    OFFICE,
    calls,
    config_writes,
    content_reads,
    envelope,
    kitchen_content,
    office_content,
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


def test_normalize_content() -> None:
    assert normalize_content(
        {
            "At": {"wattsTypeValue": 700},
            "Dd": {"value": "12/10/2026 20:57:58"},
            "Sp": {"Dd": 464},
            "Ty": {"Value": "P08073"},
            "Cg": [5, 0],
            "Pw": 1000,
            "H3": {"a": 1, "b": 2},
            "Sv": None,
        }
    ) == {
        "At": 700,
        "Dd": "12/10/2026 20:57:58",
        "Sp": 464,
        "Ty": "P08073",
        "Cg": [5, 0],
        "Pw": 1000,
        "H3": {"a": 1, "b": 2},
        "Sv": None,
    }


async def test_wrapped_values_are_unwrapped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content(
        At={"wattsTypeValue": 700},
        Dd={"value": "12/10/2026 20:57:58"},
        Dm={"value": 3},
        Ty={"Value": "P08073"},
    )
    register_cloud(aioclient_mock, kitchen={"json": kitchen})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("climate.kitchen")
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21.1
    assert state.attributes[ATTR_PRESET_MODE] == "comfort"
    assert hass.states.get("binary_sensor.kitchen_online").state == STATE_ON
    assert hass.states.get("sensor.kitchen_last_report").state == (
        "2026-10-12T18:57:58+00:00"
    )
    radiator = mock_config_entry.runtime_data.data.radiators[KITCHEN]
    assert radiator.model == "P08073"
    assert radiator.raw_content["At"] == {"wattsTypeValue": 700}


def test_report_time() -> None:
    raw = {
        "At": {"timestamp": 1791831478, "value": 700},
        "Dd": {"timestamp": 1791831470, "value": "12/10/2026 20:57:50"},
        "Dm": {"timestamp": 1791000000, "value": 4},
    }
    # The stamp of the device clock wins over the device clock string.
    assert report_time(raw, normalize_content(raw), STOCKHOLM) == datetime(
        2026, 10, 12, 18, 57, 50, tzinfo=UTC
    )
    # Without a stamped device clock the newest stamp is used.
    del raw["Dd"]
    assert report_time(raw, normalize_content(raw), STOCKHOLM) == datetime(
        2026, 10, 12, 18, 57, 58, tzinfo=UTC
    )
    # Flat payloads fall back to the device clock in local time.
    flat = {"At": 700, "Dd": "12/10/2026 20:57:58"}
    assert report_time(flat, flat, STOCKHOLM) == datetime(
        2026, 10, 12, 20, 57, 58, tzinfo=STOCKHOLM
    )
    assert report_time({"At": 700}, {"At": 700}, STOCKHOLM) is None
    zero = {"At": {"timestamp": 0, "value": 700}}
    assert report_time(zero, normalize_content(zero), STOCKHOLM) is None


async def test_unexpected_device_clock_shape_uses_value_stamps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(FROZEN_NOW)
    kitchen = kitchen_content(Dd={"date": "12/10/2026", "time": "20:57:58"}, Ty=None)
    register_cloud(aioclient_mock, kitchen={"json": kitchen})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.kitchen").state == HVACMode.HEAT
    # The other values carry stamps, so the report time is still known.
    assert hass.states.get("binary_sensor.kitchen_online").state == STATE_ON
    assert hass.states.get("sensor.kitchen_last_report").state == (
        "2026-10-12T18:57:58+00:00"
    )
    assert "unexpected format" not in caplog.text
    radiator = mock_config_entry.runtime_data.data.radiators[KITCHEN]
    assert radiator.raw_content["Dd"] == {"date": "12/10/2026", "time": "20:57:58"}
    assert radiator.model == "P08073"  # decoded from the device list instead


async def test_unparsable_device_clock_without_stamps(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(FROZEN_NOW)
    register_cloud(aioclient_mock, office={"json": office_content(Dd="not a date")})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.office").state == HVACMode.HEAT
    assert hass.states.get("binary_sensor.office_online").state == STATE_OFF
    assert hass.states.get("sensor.office_last_report").state == STATE_UNKNOWN
    assert "unexpected format" in caplog.text

    # The warning is logged once, not on every poll.
    caplog.clear()
    await poll(hass, freezer)
    assert "unexpected format" not in caplog.text


async def test_ping_results(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(FROZEN_NOW)
    aioclient_mock.put(
        "https://gkp-apim-prd.azure-api.net/iotmanagement/v1/devices/userconnected",
        status=500,
        text="Internal Server Error",
    )
    register_cloud(aioclient_mock)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    assert coordinator.ping_results[INSTALLATION_ID].startswith("failed: HTTP 500")
    assert "user-connected ping for installation MyHouse failed" in caplog.text

    # Once the ping works again the result reflects it, without more warnings.
    caplog.clear()
    aioclient_mock.clear_requests()
    register_cloud(aioclient_mock)
    await poll(hass, freezer)
    assert coordinator.ping_results == {INSTALLATION_ID: "ok"}
    assert "user-connected ping" not in caplog.text


async def setup_with_default_wake(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Set up the entry with the default options (wake after 5 silent minutes)."""
    freezer.move_to(FROZEN_NOW)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={})
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_wake_silent_radiator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await setup_with_default_wake(hass, mock_config_entry, freezer)

    # The office has been silent for 90 minutes: its current mode is re-sent
    # and it is read again; the kitchen reported 2 minutes ago and is left alone.
    assert config_writes(mock_cloud) == [
        envelope(OFFICE, [{"wattsType": "Dm", "wattsTypeValue": 3}])
    ]
    assert content_reads(mock_cloud, OFFICE) == 2
    assert content_reads(mock_cloud, KITCHEN) == 1
    assert hass.states.get("climate.office").state == HVACMode.HEAT

    # Not nudged again within the wake interval, even though it stays silent.
    await poll(hass, freezer)
    assert len(config_writes(mock_cloud)) == 1

    # Six minutes later both are nudged: the office again, and the kitchen for
    # the first time because its last report is now eight minutes old.
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    writes = config_writes(mock_cloud)
    assert len(writes) == 3
    assert [write["Id_deviceId"] for write in writes[1:]] == [KITCHEN, OFFICE]
    assert writes[1] == envelope(KITCHEN, [{"wattsType": "Dm", "wattsTypeValue": 4}])
    assert content_reads(mock_cloud, KITCHEN) == 4  # three polls plus one re-read


async def test_wake_disabled(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    assert init_integration.options == {CONF_WAKE_INTERVAL: 0}
    await poll(hass, freezer)
    assert config_writes(mock_cloud) == []
    assert content_reads(mock_cloud, OFFICE) == 2  # one read per poll, nothing more


async def test_wake_leaves_boost_and_changing_radiators_alone(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Office: silent, but boosting. Kitchen: silent (old clock stamp), but the
    # radiator has not applied the demanded mode yet.
    office = office_content(Dm=5, Cm=5)
    kitchen = kitchen_content(
        Cm=3, Dd={"timestamp": 1791000000, "value": "02/10/2026 10:00:00"}
    )
    register_cloud(aioclient_mock, kitchen={"json": kitchen}, office={"json": office})
    await setup_with_default_wake(hass, mock_config_entry, freezer)

    assert config_writes(aioclient_mock) == []
    assert content_reads(aioclient_mock, KITCHEN) == 1
    assert content_reads(aioclient_mock, OFFICE) == 1


async def test_wake_failure_does_not_break_the_cycle(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    register_cloud(
        aioclient_mock, config_replace={"status": 500, "text": "Internal Server Error"}
    )
    await setup_with_default_wake(hass, mock_config_entry, freezer)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(config_writes(aioclient_mock)) == 1  # the attempt
    assert content_reads(aioclient_mock, OFFICE) == 1  # no re-read after a failure
    assert hass.states.get("climate.office").state == HVACMode.HEAT
