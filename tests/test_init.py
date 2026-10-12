"""Tests for setting up and unloading the GKP Home integration."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gkp_home.const import (
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .conftest import FROZEN_NOW, INSTALLATION_ID, calls, register_cloud
from .helpers import SUB, token_response


async def test_setup_and_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
) -> None:
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.kitchen") is not None
    assert hass.states.get("climate.office") is not None

    # The stored refresh token is used and the rotated one persisted.
    ((_, _, data, _),) = calls(mock_cloud, "POST", "/connect/token")
    assert data == "grant_type=refresh_token&refresh_token=refresh-token-0"
    assert init_integration.data[CONF_REFRESH_TOKEN] == "refresh-token-1"

    # Installations, both radiators and the app-open ping, once per cycle.
    assert (
        len(calls(mock_cloud, "GET", f"/businessmodule/v1/installations/admins/{SUB}"))
        == 1
    )
    pings = calls(mock_cloud, "PUT", "/iotmanagement/v1/devices/userconnected")
    assert [ping[2] for ping in pings] == [{"A1": SUB, "In": INSTALLATION_ID}]

    await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("climate.kitchen").state == STATE_UNAVAILABLE


async def test_refresh_token_is_kept_when_not_rotated(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    register_cloud(aioclient_mock, token={"json": token_response(refresh_token=None)})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "refresh-token-0"


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    register_cloud(
        aioclient_mock, token={"status": 400, "text": '{"error":"invalid_grant"}'}
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id


async def test_setup_retries_when_cloud_unreachable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(
        "https://gkp-id-prd.azurewebsites.net/connect/token", json=token_response()
    )
    aioclient_mock.get(
        f"https://gkp-apim-prd.azure-api.net/businessmodule/v1/installations/admins/{SUB}",
        status=503,
        text="Service Unavailable",
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_when_no_radiator_is_readable(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    failure = {"status": 500, "text": "Internal Server Error"}
    register_cloud(aioclient_mock, kitchen=failure, office=failure)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_poll_interval_option(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(FROZEN_NOW)
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_POLL_INTERVAL: 120}
    )
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=120)

    # Changing the option applies without a reload and never goes below the minimum.
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_POLL_INTERVAL: 5}
    )
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data is coordinator
    assert coordinator.update_interval == timedelta(seconds=30)
