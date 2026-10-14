"""Tests for the GKP Home config flow."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.gkp_home.api import (
    ApiError,
    CannotConnect,
    InvalidAuth,
    TokenSet,
)
from custom_components.gkp_home.config_flow import SECTION_CREDENTIALS
from custom_components.gkp_home.const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DOMAIN,
)

from .helpers import APIM_KEY, CLIENT_SECRET, EMAIL, PASSWORD, SUB

# Dummy values standing in for the credentials built into a release.
EMBEDDED_SECRET = "embedded-dummy-secret"
EMBEDDED_KEY = "embedded-dummy-key"

ACCOUNT_INPUT = {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
CREDENTIALS_INPUT = {CONF_CLIENT_SECRET: CLIENT_SECRET, CONF_APIM_KEY: APIM_KEY}
USER_INPUT = {**ACCOUNT_INPUT, SECTION_CREDENTIALS: CREDENTIALS_INPUT}
ACCOUNT_DATA = {
    CONF_EMAIL: EMAIL,
    CONF_SUB: SUB,
    CONF_REFRESH_TOKEN: "refresh-token-1",
}
ENTRY_DATA = {**ACCOUNT_DATA, **CREDENTIALS_INPUT}


def make_token(sub: str = SUB, refresh_token: str = "refresh-token-1") -> TokenSet:
    return TokenSet("access-token", refresh_token, time.time() + 3600, sub)


def form_fields(result: FlowResult) -> list[str]:
    """Return the names of the top-level fields in the shown form."""
    return [str(key) for key in result["data_schema"].schema]


def credential_section(result: FlowResult) -> tuple[bool, bool, list[str]]:
    """Return (required, collapsed, required field names) of the credential section."""
    for key, value in result["data_schema"].schema.items():
        if str(key) == SECTION_CREDENTIALS:
            required = [
                str(field)
                for field in value.schema.schema
                if isinstance(field, vol.Required)
            ]
            return isinstance(key, vol.Required), value.options["collapsed"], required
    raise AssertionError("no credential section in form")


def account_only_entry() -> MockConfigEntry:
    """Return an entry created by a build with built-in credentials."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=SUB,
        data={CONF_EMAIL: EMAIL, CONF_SUB: SUB, CONF_REFRESH_TOKEN: "refresh-token-0"},
    )


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


@pytest.fixture
def embedded_credentials() -> Generator[None]:
    """Make this build carry the app's credentials (dummy values)."""
    with (
        patch("custom_components.gkp_home.APP_CLIENT_SECRET", EMBEDDED_SECRET),
        patch("custom_components.gkp_home.APP_SUBSCRIPTION_KEY", EMBEDDED_KEY),
    ):
        yield


@pytest.fixture
def no_embedded_credentials() -> Generator[None]:
    """Make this build carry no app credentials, so the user must enter them."""
    with (
        patch("custom_components.gkp_home.APP_CLIENT_SECRET", ""),
        patch("custom_components.gkp_home.APP_SUBSCRIPTION_KEY", ""),
    ):
        yield


# ------------------------------------------------- built-in credentials


async def test_user_flow(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert form_fields(result) == [CONF_EMAIL, CONF_PASSWORD, SECTION_CREDENTIALS]
    # The override section is optional and collapsed.
    assert credential_section(result) == (False, True, [])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == ACCOUNT_DATA
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == SUB
    mock_create_api.assert_called_once_with(hass, EMBEDDED_SECRET, EMBEDDED_KEY)
    api = mock_create_api.return_value
    api.async_login.assert_awaited_once_with(EMAIL, PASSWORD)
    api.async_get_installations.assert_awaited_once_with(SUB)
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_flow_override(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: f"  {EMAIL} ",
            CONF_PASSWORD: PASSWORD,
            SECTION_CREDENTIALS: {
                CONF_CLIENT_SECRET: f"{CLIENT_SECRET}\n",
                CONF_APIM_KEY: f" {APIM_KEY}",
            },
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA  # the overrides are stored
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)
    mock_create_api.return_value.async_login.assert_awaited_once_with(EMAIL, PASSWORD)


async def test_user_flow_empty_override_uses_builtin(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **ACCOUNT_INPUT,
            SECTION_CREDENTIALS: {CONF_CLIENT_SECRET: "  ", CONF_APIM_KEY: ""},
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ACCOUNT_DATA
    mock_create_api.assert_called_once_with(hass, EMBEDDED_SECRET, EMBEDDED_KEY)


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
    embedded_credentials: None,
    side_effect: Exception,
    error: str,
) -> None:
    api = mock_create_api.return_value
    api.async_login.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}

    # The user can correct the input and finish the flow.
    api.async_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ACCOUNT_DATA


async def test_user_flow_subscription_key_rejected(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    api = mock_create_api.return_value
    api.async_get_installations.side_effect = ApiError(401, "invalid subscription key")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    api.async_login.assert_awaited_once()


async def test_user_flow_duplicate_account(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    embedded_credentials: None,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ----------------------------------------------- bring-your-own credentials


async def test_user_flow_without_builtin_credentials(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    no_embedded_credentials: None,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    # The section is required and expanded, with both fields required.
    assert credential_section(result) == (
        True,
        False,
        [CONF_CLIENT_SECRET, CONF_APIM_KEY],
    )

    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(result["flow_id"], ACCOUNT_INPUT)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)
    mock_create_api.return_value.async_login.assert_awaited_once_with(EMAIL, PASSWORD)


# ------------------------------------------------------------------ reauth


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    entry = account_only_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_EMAIL] == EMAIL
    assert form_fields(result) == [CONF_PASSWORD, SECTION_CREDENTIALS]
    assert credential_section(result) == (False, True, [])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == ACCOUNT_DATA
    mock_create_api.assert_called_once_with(hass, EMBEDDED_SECRET, EMBEDDED_KEY)
    mock_create_api.return_value.async_login.assert_awaited_once_with(
        EMAIL, "new-password"
    )
    assert len(mock_setup_entry.mock_calls) == 1


async def test_reauth_flow_uses_stored_credentials(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    no_embedded_credentials: None,
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert credential_section(result) == (False, True, [])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == ENTRY_DATA
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)


async def test_reauth_flow_asks_for_missing_credentials(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    no_embedded_credentials: None,
) -> None:
    entry = account_only_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert credential_section(result) == (
        True,
        False,
        [CONF_CLIENT_SECRET, CONF_APIM_KEY],
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: PASSWORD, SECTION_CREDENTIALS: CREDENTIALS_INPUT},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == ENTRY_DATA
    mock_create_api.assert_called_once_with(hass, CLIENT_SECRET, APIM_KEY)


async def test_reauth_flow_partial_override(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    embedded_credentials: None,
) -> None:
    entry = account_only_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PASSWORD: PASSWORD, SECTION_CREDENTIALS: {CONF_APIM_KEY: APIM_KEY}},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {**ACCOUNT_DATA, CONF_APIM_KEY: APIM_KEY}
    mock_create_api.assert_called_once_with(hass, EMBEDDED_SECRET, APIM_KEY)


async def test_reauth_flow_errors(
    hass: HomeAssistant,
    mock_create_api: MagicMock,
    mock_setup_entry: AsyncMock,
    mock_config_entry: MockConfigEntry,
    embedded_credentials: None,
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
    embedded_credentials: None,
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


# ----------------------------------------------------------------- options


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
