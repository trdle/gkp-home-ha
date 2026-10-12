"""Fixtures for the GKP Home tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    load_json_array_fixture,
    load_json_object_fixture,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.gkp_home.const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DOMAIN,
)

from .helpers import (
    APIM_KEY,
    CLIENT_SECRET,
    CONFIG_REPLACE_URL,
    EMAIL,
    INSTALLATIONS_URL,
    NONCE_BYTES,
    STATE_BYTES,
    SUB,
    TOKEN_URL,
    USER_CONNECTED_URL,
    VERIFIER_BYTES,
    content_url,
    token_response,
)

KITCHEN = "AABBCCDDEE01"
OFFICE = "AABBCCDDEE02"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"
# 21:00 in Stockholm: the kitchen reported 2 minutes ago, the office 90 minutes ago.
FROZEN_NOW = "2026-10-12T19:00:00+00:00"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture
def fixed_random() -> Generator[None]:
    """Make the PKCE verifier, nonce and state deterministic."""
    with patch(
        "custom_components.gkp_home.api.secrets.token_bytes",
        side_effect=[VERIFIER_BYTES, NONCE_BYTES, STATE_BYTES],
    ):
        yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Provide a config entry for the test account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=SUB,
        entry_id="gkp-home-test-entry",
        data={
            CONF_EMAIL: EMAIL,
            CONF_SUB: SUB,
            CONF_REFRESH_TOKEN: "refresh-token-0",
            CONF_CLIENT_SECRET: CLIENT_SECRET,
            CONF_APIM_KEY: APIM_KEY,
        },
    )


def kitchen_content(**overrides: Any) -> dict[str, Any]:
    """Return the kitchen radiator content with optional changes."""
    return {**load_json_object_fixture("content_kitchen.json"), **overrides}


def office_content(**overrides: Any) -> dict[str, Any]:
    """Return the office radiator content with optional changes."""
    return {**load_json_object_fixture("content_office.json"), **overrides}


def register_cloud(
    aioclient_mock: AiohttpClientMocker,
    *,
    token: dict[str, Any] | None = None,
    kitchen: dict[str, Any] | None = None,
    office: dict[str, Any] | None = None,
    config_replace: dict[str, Any] | None = None,
) -> None:
    """Register the cloud responses; ``kitchen``/``office`` are mock kwargs."""
    aioclient_mock.post(TOKEN_URL, **(token or {"json": token_response()}))
    aioclient_mock.get(
        INSTALLATIONS_URL, json=load_json_array_fixture("installations.json")
    )
    aioclient_mock.get(content_url(KITCHEN), **(kitchen or {"json": kitchen_content()}))
    aioclient_mock.get(content_url(OFFICE), **(office or {"json": office_content()}))
    aioclient_mock.put(USER_CONNECTED_URL, text="")
    aioclient_mock.put(CONFIG_REPLACE_URL, **(config_replace or {"text": ""}))


@pytest.fixture
def mock_cloud(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Mock the cloud with the anonymised fixtures."""
    register_cloud(aioclient_mock)
    return aioclient_mock


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """Set up the integration with the mocked cloud at a fixed time."""
    freezer.move_to(FROZEN_NOW)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


def calls(aioclient_mock: AiohttpClientMocker, method: str, path: str) -> list[tuple]:
    """Return the recorded (method, url, data, headers) calls for a URL path."""
    return [
        call
        for call in aioclient_mock.mock_calls
        if call[0].upper() == method and call[1].path == path
    ]


def config_writes(aioclient_mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    """Return the bodies written to the configuration endpoint."""
    return [
        call[2]
        for call in calls(
            aioclient_mock,
            "PUT",
            "/iotmanagement/v1/devices/twin/properties/config/replace",
        )
    ]


def content_reads(aioclient_mock: AiohttpClientMocker, device_id: str) -> int:
    """Return how often the content of a radiator was read."""
    path = f"/iotmanagement/v1/configuration/{device_id}/{device_id}/v1/content/"
    return len(calls(aioclient_mock, "GET", path))


def envelope(device_id: str, data: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the expected configuration write body."""
    return {
        "Id_deviceId": device_id,
        "S1": device_id,
        "configurationVersion": "v1.0",
        "data": data,
    }
