"""The GKP Home integration: GKP WiFi radiators through the GKP Home cloud."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
import logging

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util

from .api import GkpHomeApi, TokenSet
from .const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DEVICE_TIMEZONE,
)
from .coordinator import GkpHomeConfigEntry, GkpHomeCoordinator, poll_interval

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.SENSOR]


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

    api = create_api(
        hass,
        entry.data[CONF_CLIENT_SECRET],
        entry.data[CONF_APIM_KEY],
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
