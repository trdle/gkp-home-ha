"""Climate entity for GKP Home radiators."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import GkpHomeError, InvalidAuth
from .const import (
    DEFAULT_BOOST_MINUTES,
    DOMAIN,
    KEY_BOOST_MINUTES,
    KEY_DEMANDED_MODE,
    KEY_SETPOINT,
    MAX_TEMP,
    MIN_TEMP,
    MODE_AUTO,
    MODE_BOOST,
    MODE_COMFORT,
    MODE_ECO,
    MODE_FROST,
    MODE_OFF,
    PRESET_FROST_PROTECTION,
    SETPOINT_KEYS,
    TEMP_STEP,
)
from .coordinator import GkpHomeConfigEntry, GkpHomeCoordinator
from .entity import GkpHomeEntity
from .util import celsius_to_tenths_f

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

PRESET_TO_MODE: dict[str, int] = {
    PRESET_COMFORT: MODE_COMFORT,
    PRESET_ECO: MODE_ECO,
    PRESET_FROST_PROTECTION: MODE_FROST,
    PRESET_BOOST: MODE_BOOST,
}
MODE_TO_PRESET: dict[int, str] = {
    mode: preset for preset, mode in PRESET_TO_MODE.items()
}
HVAC_TO_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_COMFORT,
    HVACMode.AUTO: MODE_AUTO,
}
# Heuristic used only when the radiator does not report the heating flag (Hs).
HEATING_HYSTERESIS = 0.2


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GkpHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one climate entity per radiator."""
    coordinator = entry.runtime_data
    async_add_entities(
        GkpHomeClimate(coordinator, device_id)
        for device_id in coordinator.data.radiators
    )


class GkpHomeClimate(GkpHomeEntity, ClimateEntity):
    """A GKP WiFi radiator."""

    _attr_name = None
    _attr_translation_key = "radiator"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = [
        PRESET_COMFORT,
        PRESET_ECO,
        PRESET_FROST_PROTECTION,
        PRESET_BOOST,
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP

    def __init__(self, coordinator: GkpHomeCoordinator, device_id: str) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, device_id, CLIMATE_DOMAIN)
        self._attr_unique_id = device_id

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        return self.radiator.current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the setpoint of the demanded mode."""
        return self.radiator.target_temperature

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return off, auto (weekly program) or heat (any manual mode)."""
        mode = self.radiator.demanded_mode
        if mode is None:
            return None
        if mode == MODE_OFF:
            return HVACMode.OFF
        if mode == MODE_AUTO:
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return whether the radiator is heating right now."""
        radiator = self.radiator
        mode = radiator.demanded_mode
        if mode is None:
            return None
        if mode == MODE_OFF:
            return HVACAction.OFF
        if (heating := radiator.heating) is not None:
            return HVACAction.HEATING if heating else HVACAction.IDLE
        # Older firmware without Hs: assume heating while below the setpoint.
        current = radiator.current_temperature
        target = radiator.target_temperature
        if current is None or target is None:
            return None
        return (
            HVACAction.HEATING
            if current < target - HEATING_HYSTERESIS
            else HVACAction.IDLE
        )

    @property
    def preset_mode(self) -> str | None:
        """Return the manual mode as a preset; none in auto/off."""
        mode = self.radiator.demanded_mode
        return MODE_TO_PRESET.get(mode) if mode is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw modes for automations."""
        radiator = self.radiator
        return {
            "demanded_mode": radiator.demanded_mode,
            "current_mode": radiator.current_mode,
            "boost_minutes": radiator.boost_minutes,
            "room": radiator.room_name,
            "home": radiator.home_name,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set off, auto (program) or heat (comfort)."""
        await self._async_write_mode(HVAC_TO_MODE[hvac_mode])

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch to comfort, eco, frost protection or boost."""
        mode = PRESET_TO_MODE[preset_mode]
        boost_minutes = None
        if mode == MODE_BOOST:
            boost_minutes = self.radiator.boost_minutes or DEFAULT_BOOST_MINUTES
        await self._async_write_mode(mode, boost_minutes)

    async def async_turn_on(self) -> None:
        """Turn the radiator on (comfort)."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn the radiator off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write the setpoint of the active mode.

        In off or auto (and when the mode has no setpoint) the radiator is
        switched to comfort and the comfort setpoint is written, which matches
        how a thermostat behaves in Apple Home: changing the target turns the
        heating on.
        """
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        mode = self.radiator.demanded_mode
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            mode = HVAC_TO_MODE[HVACMode(hvac_mode)]
        if mode not in SETPOINT_KEYS:
            mode = MODE_COMFORT
        value = celsius_to_tenths_f(float(temperature))
        try:
            await self.coordinator.api.async_set_setpoint(self.device_id, mode, value)
        except GkpHomeError as err:
            raise _write_error(err) from err
        self.coordinator.async_update_radiator(
            self.device_id,
            **{
                KEY_DEMANDED_MODE: mode,
                SETPOINT_KEYS[mode]: value,
                KEY_SETPOINT: value,
            },
        )
        self.coordinator.async_schedule_refresh()

    async def _async_write_mode(
        self, mode: int, boost_minutes: int | None = None
    ) -> None:
        """Write the demanded mode and update the local state."""
        try:
            await self.coordinator.api.async_set_mode(
                self.device_id, mode, boost_minutes
            )
        except GkpHomeError as err:
            raise _write_error(err) from err
        changes: dict[str, Any] = {KEY_DEMANDED_MODE: mode}
        if (key := SETPOINT_KEYS.get(mode)) and key in self.radiator.content:
            changes[KEY_SETPOINT] = self.radiator.content[key]
        if boost_minutes is not None:
            changes[KEY_BOOST_MINUTES] = boost_minutes
        self.coordinator.async_update_radiator(self.device_id, **changes)
        self.coordinator.async_schedule_refresh()


def _write_error(err: GkpHomeError) -> HomeAssistantError:
    """Translate a cloud error into a user-facing error."""
    if isinstance(err, InvalidAuth):
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed"
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="write_failed",
        translation_placeholders={"error": str(err)},
    )
