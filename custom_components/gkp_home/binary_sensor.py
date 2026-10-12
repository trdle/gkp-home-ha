"""Binary sensors for GKP Home radiators."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import GkpHomeConfigEntry, GkpHomeCoordinator, Radiator
from .entity import GkpHomeEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


@dataclass(frozen=True, kw_only=True)
class GkpHomeBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a GKP Home binary sensor."""

    value_fn: Callable[[Radiator, datetime], bool | None]


BINARY_SENSORS: tuple[GkpHomeBinarySensorDescription, ...] = (
    GkpHomeBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radiator, now: radiator.is_online(now),
    ),
    GkpHomeBinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda radiator, _now: radiator.heating,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GkpHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors of every radiator."""
    coordinator = entry.runtime_data
    async_add_entities(
        GkpHomeBinarySensor(coordinator, device_id, description)
        for device_id in coordinator.data.radiators
        for description in BINARY_SENSORS
    )


class GkpHomeBinarySensor(GkpHomeEntity, BinarySensorEntity):
    """A yes/no state of a radiator."""

    entity_description: GkpHomeBinarySensorDescription

    def __init__(
        self,
        coordinator: GkpHomeCoordinator,
        device_id: str,
        description: GkpHomeBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, device_id, BINARY_SENSOR_DOMAIN, description.key)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.value_fn(self.radiator, dt_util.utcnow())
