"""The GKP Home integration: GKP WiFi radiators through the GKP Home cloud."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
import logging
from typing import Any

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util

from .api import GkpHomeApi, TokenSet
from .const import (
    APP_CLIENT_SECRET,
    APP_SUBSCRIPTION_KEY,
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DEVICE_TIMEZONE,
)
from .coordinator import GkpHomeConfigEntry, GkpHomeCoordinator, poll_interval

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.SENSOR]


def embedded_credentials_available() -> bool:
    """Return True if this build carries the GKP Home app's API credentials."""
    return bool(APP_CLIENT_SECRET and APP_SUBSCRIPTION_KEY)


def resolve_app_credentials(data: Mapping[str, Any]) -> tuple[str, str]:
    """Return the client secret and subscription key to use.

    Values the user entered (stored in the config entry) win over the built-in
    ones, so a changed app credential can be worked around without an update.
    """
    client_secret = str(data.get(CONF_CLIENT_SECRET) or "").strip() or APP_CLIENT_SECRET
    apim_key = str(data.get(CONF_APIM_KEY) or "").strip() or APP_SUBSCRIPTION_KEY
    return client_secret, apim_key


@callback
def _create_login_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Create a throwaway session with its own cookie jar for the login flow.

    The shared Home Assistant session must not collect the identity server's
    cookies. ``connector_owner=False`` keeps the shared connector open when the
    client closes this session after the login.
    """
    return async_create_clientsession(
        hass,
        auto_cleanup=False,
        cookie_jar=aiohttp.CookieJar(),
        connector_owner=False,
    )


@callback
def create_api(
    hass: HomeAssistant,
    client_secret: str,
    apim_key: str,
    *,
    refresh_token: str | None = None,
    token_listener: Callable[[TokenSet], None] | None = None,
) -> GkpHomeApi:
    """Create a cloud client that uses Home Assistant's aiohttp sessions."""
    return GkpHomeApi(
        async_get_clientsession(hass),
        client_secret,
        apim_key,
        refresh_token=refresh_token,
        login_session_factory=partial(_create_login_session, hass),
        token_listener=token_listener,
    )


async def async_setup_entry(hass: HomeAssistant, entry: GkpHomeConfigEntry) -> bool:
    """Set up GKP Home from a config entry."""

    @callback
    def _store_refresh_token(token: TokenSet) -> None:
        """Persist a rotated refresh token (the only credential that is kept)."""
        if token.refresh_token and token.refresh_token != entry.data.get(
            CONF_REFRESH_TOKEN
        ):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token.refresh_token}
            )

    client_secret, apim_key = resolve_app_credentials(entry.data)
    if not client_secret or not apim_key:
        # Neither built in nor stored: let the user enter them via reauth.
        raise ConfigEntryAuthFailed("The GKP Home API credentials are missing")
    api = create_api(
        hass,
        client_secret,
        apim_key,
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_listener=_store_refresh_token,
    )
    device_zone = await dt_util.async_get_time_zone(DEVICE_TIMEZONE) or dt_util.UTC
    coordinator = GkpHomeCoordinator(
        hass, entry, api, entry.data[CONF_SUB], device_zone
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: GkpHomeConfigEntry
) -> None:
    """Apply a changed poll interval without reloading."""
    entry.runtime_data.update_interval = poll_interval(entry)


async def async_unload_entry(hass: HomeAssistant, entry: GkpHomeConfigEntry) -> bool:
    """Unload a config entry (the coordinator shuts itself down on unload)."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
