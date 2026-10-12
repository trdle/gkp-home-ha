"""Base entity for the GKP Home integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import GkpHomeCoordinator, Radiator
from .util import device_id_to_mac


class GkpHomeEntity(CoordinatorEntity[GkpHomeCoordinator]):
    """An entity belonging to one radiator (one Home Assistant device)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GkpHomeCoordinator, device_id: str) -> None:
        """Initialize the entity and its device."""
        super().__init__(coordinator)
        self._device_id = device_id
        radiator = coordinator.data.radiators[device_id]
        device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            model=radiator.model,
            sw_version=radiator.firmware,
            name=radiator.room_name,
            suggested_area=radiator.room_name,
        )
        if (mac := device_id_to_mac(device_id)) is not None:
            device_info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        self._attr_device_info = device_info

    @property
    def device_id(self) -> str:
        """Return the radiator's device id."""
        return self._device_id

    @property
    def radiator(self) -> Radiator:
        """Return the radiator's latest state."""
        return self.coordinator.data.radiators[self._device_id]

    @property
    def available(self) -> bool:
        """Return True while the coordinator has data for this radiator."""
        return super().available and self._device_id in self.coordinator.data.radiators
