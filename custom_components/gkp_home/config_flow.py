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
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from . import create_api
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

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
        vol.Required(CONF_CLIENT_SECRET): PASSWORD_SELECTOR,
        vol.Required(CONF_APIM_KEY): PASSWORD_SELECTOR,
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


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
        """Ask for the account and the app's API credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            try:
                token = await _async_sign_in(
                    self.hass,
                    email,
                    user_input[CONF_PASSWORD],
                    user_input[CONF_CLIENT_SECRET].strip(),
                    user_input[CONF_APIM_KEY].strip(),
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
                        CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET].strip(),
                        CONF_APIM_KEY: user_input[CONF_APIM_KEY].strip(),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_EMAIL: user_input.get(CONF_EMAIL)} if user_input else None,
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
            try:
                token = await _async_sign_in(
                    self.hass,
                    entry.data[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    entry.data[CONF_CLIENT_SECRET],
                    entry.data[CONF_APIM_KEY],
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
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
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
