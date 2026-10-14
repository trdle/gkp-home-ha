"""Config flow for the GKP Home integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import create_api, resolve_app_credentials
from .api import ApiError, CannotConnect, InvalidAuth, TokenSet
from .const import (
    CONF_APIM_KEY,
    CONF_CLIENT_SECRET,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SUB,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

SECTION_CREDENTIALS = "api_credentials"
CREDENTIAL_KEYS = (CONF_CLIENT_SECRET, CONF_APIM_KEY)
EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
CREDENTIAL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _credential_section(*, available: bool) -> dict[vol.Marker, Any]:
    """Return the collapsible section holding the app's API credentials.

    Optional and collapsed while credentials are available (built into this
    release, or stored in the entry), so they can still be overridden; required
    and expanded when nothing is available at all.
    """
    marker = vol.Optional if available else vol.Required
    fields = {marker(key): CREDENTIAL_SELECTOR for key in CREDENTIAL_KEYS}
    return {
        marker(SECTION_CREDENTIALS): section(
            vol.Schema(fields), {"collapsed": available}
        )
    }


def _credential_overrides(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return the credentials the user typed, without empty values."""
    values = user_input.get(SECTION_CREDENTIALS) or {}
    return {
        key: value
        for key in CREDENTIAL_KEYS
        if (value := str(values.get(key) or "").strip())
    }


def _credentials_available(data: Mapping[str, Any]) -> bool:
    """Return True if both API credentials can be resolved for the data."""
    return all(resolve_app_credentials(data))


async def _async_sign_in(
    hass: HomeAssistant, email: str, password: str, client_secret: str, apim_key: str
) -> TokenSet:
    """Sign in and verify the credentials give access to the API."""
    api = create_api(hass, client_secret, apim_key)
    token = await api.async_login(email, password)
    # The identity server does not check the subscription key; the API does.
    await api.async_get_installations(token.sub)
    return token


class GkpHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the GKP Home config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account, and for the app's API credentials when needed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            overrides = _credential_overrides(user_input)
            client_secret, apim_key = resolve_app_credentials(overrides)
            try:
                token = await _async_sign_in(
                    self.hass, email, user_input[CONF_PASSWORD], client_secret, apim_key
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except ApiError as err:
                errors["base"] = (
                    "invalid_auth" if err.status in (401, 403) else "cannot_connect"
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while signing in to GKP Home")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(token.sub)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_SUB: token.sub,
                        CONF_REFRESH_TOKEN: token.refresh_token,
                        **overrides,
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                **_credential_section(available=_credentials_available({})),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_EMAIL: user_input.get(CONF_EMAIL)} if user_input else None
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after the refresh token was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and store the new refresh token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            overrides = _credential_overrides(user_input)
            client_secret, apim_key = resolve_app_credentials(
                {**entry.data, **overrides}
            )
            try:
                token = await _async_sign_in(
                    self.hass,
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    client_secret,
                    apim_key,
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except ApiError as err:
                errors["base"] = (
                    "invalid_auth" if err.status in (401, 403) else "cannot_connect"
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error while signing in to GKP Home")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(token.sub)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_REFRESH_TOKEN: token.refresh_token,
                        CONF_SUB: token.sub,
                        **overrides,
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                **_credential_section(available=_credentials_available(entry.data)),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GkpHomeOptionsFlow:
        """Return the options flow."""
        return GkpHomeOptionsFlow()


class GkpHomeOptionsFlow(OptionsFlow):
    """Let the user change the poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL,
                            max=3600,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
