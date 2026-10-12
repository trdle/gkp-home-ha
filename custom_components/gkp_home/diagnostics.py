"""Diagnostics support for GKP Home."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_APIM_KEY, CONF_CLIENT_SECRET, CONF_REFRESH_TOKEN, CONF_SUB
from .coordinator import GkpHomeConfigEntry

TO_REDACT = {
    CONF_REFRESH_TOKEN,
    CONF_CLIENT_SECRET,
    CONF_APIM_KEY,
    CONF_EMAIL,
    CONF_SUB,
    "unique_id",
    "title",
    # Installation ids and the user id as they appear in the cloud payloads.
    "id",
    "In",
    "It",
    "A1",
    "Zt",
    "home_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GkpHomeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    now = dt_util.utcnow()
    diagnostics = {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "poll_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
        "last_update_success": coordinator.last_update_success,
        "homes": [asdict(home) for home in data.homes.values()],
        "radiators": [
            {
                "id": radiator.id,
                "home_id": radiator.home_id,
                "home_name": radiator.home_name,
                "room_id": radiator.room_id,
                "room_name": radiator.room_name,
                "model": radiator.model,
                "last_report": radiator.last_report.isoformat()
                if radiator.last_report
                else None,
                "online": radiator.is_online(now),
                "stale": radiator.stale,
                "content": radiator.content,
            }
            for radiator in data.radiators.values()
        ],
        "raw_installations": data.raw_installations,
    }
    return async_redact_data(diagnostics, TO_REDACT)
