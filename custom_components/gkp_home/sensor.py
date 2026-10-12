"""Diagnostic sensors for GKP Home radiators."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import StateType

from .const import MODE_COMFORT, MODE_ECO, MODE_FROST
from .coordinator import GkpHomeConfigEntry, GkpHomeCoordinator, Radiator
from .entity import GkpHomeEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


@dataclass(frozen=True, kw_only=True)
class GkpHomeSensorDescription(SensorEntityDescription):
    """Describes a GKP Home sensor."""

    value_fn: Callable[[Radiator], StateType | datetime]


SENSORS: tuple[GkpHomeSensorDescription, ...] = (
    GkpHomeSensorDescription(
        key="rated_power",
        translation_key="rated_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radiator: radiator.rated_power,
    ),
    GkpHomeSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radiator: radiator.firmware,
    ),
    GkpHomeSensorDescription(
        key="last_report",
        translation_key="last_report",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radiator: radiator.last_report,
    ),
    GkpHomeSensorDescription(
        key="eco_setpoint",
        translation_key="eco_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda radiator: radiator.setpoint_for_mode(MODE_ECO),
    ),
    GkpHomeSensorDescription(
        key="comfort_setpoint",
        translation_key="comfort_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda radiator: radiator.setpoint_for_mode(MODE_COMFORT),
    ),
    GkpHomeSensorDescription(
        key="frost_setpoint",
        translation_key="frost_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda radiator: radiator.setpoint_for_mode(MODE_FROST),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GkpHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors of every radiator."""
    coordinator = entry.runtime_data
    async_add_entities(
        GkpHomeSensor(coordinator, device_id, description)
        for device_id in coordinator.data.radiators
        for description in SENSORS
    )


class GkpHomeSensor(GkpHomeEntity, SensorEntity):
    """A diagnostic value of a radiator."""

    entity_description: GkpHomeSensorDescription

    def __init__(
        self,
        coordinator: GkpHomeCoordinator,
        device_id: str,
        description: GkpHomeSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id, SENSOR_DOMAIN, description.key)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def native_value(self) -> StateType | datetime:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.radiator)
