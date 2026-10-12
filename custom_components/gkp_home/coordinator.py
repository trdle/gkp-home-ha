"""Data update coordinator for the GKP Home integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApiError, CannotConnect, GkpHomeApi, GkpHomeError, InvalidAuth
from .const import (
    CONF_POLL_INTERVAL,
    CONTENT_CONCURRENCY,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    KEY_BOOST_MINUTES,
    KEY_CURRENT_MODE,
    KEY_DEMANDED_MODE,
    KEY_DEVICE_ID,
    KEY_DEVICE_TIME,
    KEY_DEVICES,
    KEY_FIRMWARE,
    KEY_HEATING,
    KEY_INSTALLATION_ID,
    KEY_INSTALLATION_NAME,
    KEY_MODEL,
    KEY_RATED_POWER,
    KEY_ROOM_ID,
    KEY_ROOM_NAME,
    KEY_ROOM_TEMP,
    KEY_ROOMS,
    KEY_SETPOINT,
    MIN_POLL_INTERVAL,
    OFFLINE_AFTER,
    SETPOINT_KEYS,
    WRITE_REFRESH_DELAY,
)
from .util import (
    device_time_age,
    hex_ascii_to_str,
    parse_device_time,
    tenths_f_to_celsius,
)

_LOGGER = logging.getLogger(__name__)

type GkpHomeConfigEntry = ConfigEntry[GkpHomeCoordinator]


def _as_int(value: Any) -> int | None:
    """Return the value as an int if it looks like one."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_temperature(value: Any) -> float | None:
    """Convert a tenths-of-°F content value to °C."""
    number = _as_int(value)
    return tenths_f_to_celsius(number) if number is not None else None


@dataclass
class Radiator:
    """A GKP WiFi radiator with its latest reported state (content)."""

    id: str
    home_id: str
    home_name: str
    room_id: str
    room_name: str
    content: dict[str, Any]
    model: str | None
    last_report: datetime | None
    # True when this cycle's content request failed and the data is from an
    # earlier cycle.
    stale: bool = False

    @property
    def name(self) -> str:
        """Return the name shown for the radiator (its room)."""
        return self.room_name

    @property
    def demanded_mode(self) -> int | None:
        """Return the demanded mode (``Dm``)."""
        return _as_int(self.content.get(KEY_DEMANDED_MODE))

    @property
    def current_mode(self) -> int | None:
        """Return the mode the radiator is currently running (``Cm``)."""
        return _as_int(self.content.get(KEY_CURRENT_MODE))

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature in °C (``At``)."""
        return _as_temperature(self.content.get(KEY_ROOM_TEMP))

    @property
    def active_setpoint(self) -> float | None:
        """Return the active setpoint in °C (``Sp``)."""
        return _as_temperature(self.content.get(KEY_SETPOINT))

    def setpoint_for_mode(self, mode: int | None) -> float | None:
        """Return the stored setpoint of a mode in °C, if the mode has one."""
        if mode is None or (key := SETPOINT_KEYS.get(mode)) is None:
            return None
        return _as_temperature(self.content.get(key))

    @property
    def target_temperature(self) -> float | None:
        """Return the setpoint of the demanded mode, falling back to ``Sp``."""
        setpoint = self.setpoint_for_mode(self.demanded_mode)
        return setpoint if setpoint is not None else self.active_setpoint

    @property
    def heating(self) -> bool | None:
        """Return whether the heating element is on (``Hs``), if reported."""
        value = _as_int(self.content.get(KEY_HEATING))
        return None if value is None else value == 1

    @property
    def boost_minutes(self) -> int | None:
        """Return the boost duration in minutes (``Bt``)."""
        return _as_int(self.content.get(KEY_BOOST_MINUTES))

    @property
    def rated_power(self) -> int | None:
        """Return the rated power in W (``Pw``)."""
        return _as_int(self.content.get(KEY_RATED_POWER))

    @property
    def firmware(self) -> str | None:
        """Return the firmware version (``Sv``)."""
        value = self.content.get(KEY_FIRMWARE)
        return str(value) if value else None

    def is_online(self, now: datetime) -> bool:
        """Return True if the radiator reported recently."""
        if self.last_report is None:
            return False
        return device_time_age(self.last_report, now) <= OFFLINE_AFTER


@dataclass
class Room:
    """A room of an installation."""

    id: str
    name: str
    home_id: str
    radiator_ids: list[str] = field(default_factory=list)


@dataclass
class Home:
    """An installation (home) of the GKP account."""

    id: str
    name: str
    rooms: dict[str, Room] = field(default_factory=dict)


@dataclass
class GkpHomeData:
    """Everything the coordinator fetched in one cycle."""

    homes: dict[str, Home]
    radiators: dict[str, Radiator]
    raw_installations: list[dict[str, Any]]


@dataclass
class _DeviceMeta:
    """Where a device lives, from the installations response."""

    id: str
    home_id: str
    home_name: str
    room_id: str
    room_name: str
    model_hex: str | None


def parse_installations(
    installations: list[dict[str, Any]],
) -> tuple[dict[str, Home], list[_DeviceMeta]]:
    """Map the installations response to homes, rooms and device metadata."""
    homes: dict[str, Home] = {}
    devices: list[_DeviceMeta] = []
    for installation in installations:
        home_id = installation.get(KEY_INSTALLATION_ID) or installation.get("id")
        if not home_id:
            continue
        home_name = str(installation.get(KEY_INSTALLATION_NAME) or "GKP Home").strip()
        home = Home(id=str(home_id), name=home_name)
        for index, room_data in enumerate(installation.get(KEY_ROOMS) or []):
            room_name = (
                str(room_data.get(KEY_ROOM_NAME) or "").strip() or f"Room {index + 1}"
            )
            room_id = str(room_data.get(KEY_ROOM_ID) or f"{home.id}-{index}")
            room = Room(id=room_id, name=room_name, home_id=home.id)
            for device in room_data.get(KEY_DEVICES) or []:
                device_id = device.get(KEY_DEVICE_ID)
                if not device_id:
                    continue
                room.radiator_ids.append(str(device_id))
                devices.append(
                    _DeviceMeta(
                        id=str(device_id),
                        home_id=home.id,
                        home_name=home.name,
                        room_id=room.id,
                        room_name=room.name,
                        model_hex=device.get(KEY_MODEL),
                    )
                )
            home.rooms[room.id] = room
        homes[home.id] = home
    return homes, devices


def poll_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured poll interval."""
    seconds = _as_int(entry.options.get(CONF_POLL_INTERVAL)) or DEFAULT_POLL_INTERVAL
    return timedelta(seconds=max(MIN_POLL_INTERVAL, seconds))


class GkpHomeCoordinator(DataUpdateCoordinator[GkpHomeData]):
    """Poll the GKP Home cloud for the state of every radiator."""

    config_entry: GkpHomeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GkpHomeConfigEntry,
        api: GkpHomeApi,
        sub: str,
        device_zone: tzinfo,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=poll_interval(entry),
        )
        self.api = api
        self.sub = sub
        self.device_zone = device_zone
        self._semaphore = asyncio.Semaphore(CONTENT_CONCURRENCY)
        self._cancel_refresh: CALLBACK_TYPE | None = None

    async def _async_update_data(self) -> GkpHomeData:
        """Fetch installations, then the content of every radiator."""
        try:
            installations = await self.api.async_get_installations(self.sub)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed("GKP Home rejected the saved session") from err
        except (CannotConnect, ApiError) as err:
            raise UpdateFailed(f"Error fetching installations: {err}") from err

        homes, devices = parse_installations(installations)
        results = await asyncio.gather(
            *(self._async_fetch_content(device.id) for device in devices),
            return_exceptions=True,
        )

        previous = self.data.radiators if self.data else {}
        radiators: dict[str, Radiator] = {}
        for meta, result in zip(devices, results, strict=True):
            if isinstance(result, InvalidAuth):
                raise ConfigEntryAuthFailed(
                    "GKP Home rejected the saved session"
                ) from result
            if isinstance(result, BaseException):
                if (known := previous.get(meta.id)) is None:
                    _LOGGER.warning(
                        "Could not read radiator %s (%s): %s",
                        meta.id,
                        meta.room_name,
                        result,
                    )
                    continue
                _LOGGER.debug(
                    "Could not read radiator %s (%s), keeping previous state: %s",
                    meta.id,
                    meta.room_name,
                    result,
                )
                content, stale = known.content, True
            else:
                content, stale = result, False
            radiators[meta.id] = Radiator(
                id=meta.id,
                home_id=meta.home_id,
                home_name=meta.home_name,
                room_id=meta.room_id,
                room_name=meta.room_name,
                content=content,
                model=_model(content, meta.model_hex),
                last_report=parse_device_time(
                    content.get(KEY_DEVICE_TIME), self.device_zone
                ),
                stale=stale,
            )

        if devices and not radiators:
            raise UpdateFailed("Could not read the state of any radiator")

        # The app tells the cloud a user is watching each installation; do the same.
        for home in homes.values():
            try:
                await self.api.async_user_connected(self.sub, home.id)
            except GkpHomeError as err:
                _LOGGER.debug("User-connected ping failed for %s: %s", home.id, err)

        return GkpHomeData(
            homes=homes, radiators=radiators, raw_installations=installations
        )

    async def _async_fetch_content(self, device_id: str) -> dict[str, Any]:
        """Fetch one radiator's content, limiting concurrency."""
        async with self._semaphore:
            return await self.api.async_get_device_content(device_id)

    @callback
    def async_update_radiator(self, device_id: str, **changes: Any) -> None:
        """Apply the expected result of a write locally until the next refresh."""
        if (
            self.data is None
            or (radiator := self.data.radiators.get(device_id)) is None
        ):
            return
        radiator.content = {**radiator.content, **changes}
        self.async_update_listeners()

    @callback
    def async_schedule_refresh(self, delay: float = WRITE_REFRESH_DELAY) -> None:
        """Refresh shortly after a write so the real state replaces the local guess."""
        self._cancel_pending_refresh()
        self._cancel_refresh = async_call_later(
            self.hass, delay, self._async_delayed_refresh
        )

    async def _async_delayed_refresh(self, _now: datetime) -> None:
        self._cancel_refresh = None
        await self.async_request_refresh()

    @callback
    def _cancel_pending_refresh(self) -> None:
        if self._cancel_refresh is not None:
            self._cancel_refresh()
            self._cancel_refresh = None

    async def async_shutdown(self) -> None:
        """Cancel pending work when the entry is unloaded."""
        self._cancel_pending_refresh()
        await super().async_shutdown()


def _model(content: dict[str, Any], model_hex: str | None) -> str | None:
    """Return the model name from the content, else decoded from the device list."""
    if model := content.get(KEY_MODEL):
        return str(model)
    return hex_ascii_to_str(model_hex) if model_hex else None
