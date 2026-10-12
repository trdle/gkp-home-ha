"""Tests for the GKP Home config flow."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.gkp_home.api import (
    ApiError,
    CannotConnect,
    InvalidAuth,
    TokenSet,
)
from custom_components.gkp_home.const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DOMAIN,
)

from .helpers import APIM_KEY, CLIENT_SECRET, EMAIL, PASSWORD, SUB

USER_INPUT = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: PASSWORD,
    CONF_CLIENT_SECRET: CLIENT_SECRET,
    CONF_APIM_KEY: APIM_KEY,
}
ENTRY_DATA = {
    CONF_EMAIL: EMAIL,
    CONF_SUB: SUB,
    CONF_REFRESH_TOKEN: "refresh-token-1",
    CONF_CLIENT_SECRET: CLIENT_SECRET,
    CONF_APIM_KEY: APIM_KEY,
}


def make_token(sub: str = SUB, refresh_token: str = "refresh-token-1") -> TokenSet:
    return TokenSet("access-token", refresh_token, time.time() + 3600, sub)


@pytest.fixture
def mock_create_api() -> Generator[MagicMock]:
    """Replace the cloud client used by the config flow."""
    with patch("custom_components.gkp_home.config_flow.create_api") as create_api:
        api = create_api.return_value
        api.async_login = AsyncMock(return_value=make_token())
        api.async_get_installations = AsyncMock(return_value=[])
        yield create_api


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Avoid setting up the integration after the flow finishes."""
    with patch(
        "custom_components.gkp_home.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def test_user_flow(
    hass: HomeAssistant, mock_create_api: MagicMock, mock_setup_entry: AsyncMock
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == ENTRY_DATA
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == SUB
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)
    api = mock_create_api.return_value
    api.async_login.assert_awaited_once_with(EMAIL, PASSWORD)
    api.async_get_installations.assert_awaited_once_with(SUB)
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_strips_whitespace(
    hass: HomeAssistant, mock_create_api: MagicMock, mock_setup_entry: AsyncMock
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: f"  {EMAIL} ",
            CONF_PASSWORD: PASSWORD,
            CONF_CLIENT_SECRET: f"{CLIENT_SECRET}\n",
            CONF_APIM_KEY: f" {APIM_KEY}",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)
    mock_create_api.return_value.async_login.assert_awaited_once_with(EMAIL, PASSWORD)


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (InvalidAuth("bad credentials"), "invalid_auth"),
        (CannotConnect("down"), "cannot_connect"),
        (ApiError(503, "unavailable"), "cannot_connect"),
        (
            ApiError(401, "Access denied due to invalid subscription key"),
            "invalid_auth",
        ),
        (RuntimeError("surprise"), "unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    side_effect: Exception,
    error: str,
) -> None:
    api = mock_create_api.return_value
    api.async_login.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    # The user can correct the input and finish the flow.
    api.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA


async def test_user_flow_subscription_key_rejected(
    hass: HomeAssistant, mock_create_api: MagicMock, mock_setup_entry: AsyncMock
) -> None:
    api = mock_create_api.return_value
    api.async_get_installations.side_effect = ApiError(401, "invalid subscription key")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    api.async_login.assert_awaited_once()


async def test_user_flow_duplicate_account(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_EMAIL] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == ENTRY_DATA
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)
    mock_create_api.return_value.async_login.assert_awaited_once_with(
        EMAIL, "new-password"
    )
    assert len(mock_setup_entry.mock_calls) == 1


async def test_reauth_flow_errors(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    api = mock_create_api.return_value
    api.async_login.side_effect = InvalidAuth("wrong password")
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    api.async_login.side_effect = CannotConnect("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    api.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "refresh-token-1"


async def test_reauth_flow_other_account(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    mock_create_api.return_value.async_login.return_value = make_token(sub="USER000002")
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "refresh-token-0"


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 45}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {CONF_POLL_INTERVAL: 45}
    assert init_integration.runtime_data.update_interval == timedelta(seconds=45)


async def test_options_flow_rejects_short_interval(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(vol.Invalid):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 10}
        )
