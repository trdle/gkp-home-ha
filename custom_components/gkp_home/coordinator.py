"""Data update coordinator for the GKP Home integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, CannotConnect, GkpHomeApi, GkpHomeError, InvalidAuth
from .const import (
    CONF_POLL_INTERVAL,
    CONF_WAKE_INTERVAL,
    CONTENT_CONCURRENCY,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WAKE_INTERVAL,
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
    MODE_BOOST,
    OFFLINE_AFTER,
    SETPOINT_KEYS,
    USER_CONNECTED_SETTLE_DELAY,
    WAKE_SETTLE_DELAY,
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


def _as_str(value: Any) -> str | None:
    """Return a non-empty scalar as a string, or None for anything else."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


# Keys under which a wrapped content value (``{"value": ...}``) holds the value.
_VALUE_WRAPPER_KEYS = ("wattsTypeValue", "value", "Value", "val", "v")


def normalize_content(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the content with wrapped values unwrapped.

    The reverse-engineered sample has plain values, but the live cloud has been
    seen delivering objects for some keys. Known wrappers and single-entry
    objects are unwrapped; anything else is left as is, and the raw payload stays
    available in the diagnostics.
    """
    content: dict[str, Any] = {}
    for key, raw_value in raw.items():
        value = raw_value
        if isinstance(raw_value, dict):
            wrapper = next((k for k in _VALUE_WRAPPER_KEYS if k in raw_value), None)
            if wrapper is not None:
                value = raw_value[wrapper]
            elif len(raw_value) == 1:
                value = next(iter(raw_value.values()))
        content[key] = value
    return content


def report_time(
    raw: dict[str, Any], content: dict[str, Any], zone: tzinfo
) -> datetime | None:
    """Return when the radiator last reported its state.

    The live cloud stamps every value with the UTC time it received it; the
    stamp on the device clock (``Dd``) marks the last full report. Payloads
    without stamps fall back to parsing the device clock string, which is in
    the radiator's local time.
    """
    stamps: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and (stamp := _as_int(value.get("timestamp"))):
            stamps[key] = stamp
    stamp = stamps.get(KEY_DEVICE_TIME) or (max(stamps.values()) if stamps else None)
    if stamp:
        return datetime.fromtimestamp(stamp, tz=UTC)
    return parse_device_time(content.get(KEY_DEVICE_TIME), zone)


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
    # The payload exactly as the cloud delivered it, for diagnostics.
    raw_content: dict[str, Any] = field(default_factory=dict)

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
        return _as_str(self.content.get(KEY_FIRMWARE))

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


def wake_interval(entry: ConfigEntry) -> timedelta | None:
    """Return how long a radiator may stay silent before it is woken, or None."""
    minutes = _as_int(entry.options.get(CONF_WAKE_INTERVAL))
    if minutes is None:
        minutes = DEFAULT_WAKE_INTERVAL
    return timedelta(minutes=minutes) if minutes > 0 else None


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
        # Radiators whose timestamp could not be parsed (warned about once).
        self._unparsed_time: set[str] = set()
        # Outcome of the last user-connected ping per installation (diagnostics).
        self.ping_results: dict[str, str] = {}
        self._warned_pings: set[str] = set()
        # When each radiator was last nudged to report.
        self._last_wake: dict[str, datetime] = {}

    async def _async_update_data(self) -> GkpHomeData:
        """Fetch installations, then the content of every radiator."""
        try:
            installations = await self.api.async_get_installations(self.sub)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed("GKP Home rejected the saved session") from err
        except (CannotConnect, ApiError) as err:
            raise UpdateFailed(f"Error fetching installations: {err}") from err

        homes, devices = parse_installations(installations)

        # Like the app, tell the cloud a user is watching each installation
        # before reading the radiators: they only report fresh values while a
        # user is connected, so without this they look frozen.
        if await self._async_ping_installations(homes) and devices:
            await asyncio.sleep(USER_CONNECTED_SETTLE_DELAY)

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
                content, raw_content, stale = known.content, known.raw_content, True
            else:
                _LOGGER.debug("Content of radiator %s: %s", meta.id, result)
                content, raw_content, stale = normalize_content(result), result, False
            radiators[meta.id] = self._build_radiator(meta, content, raw_content, stale)

        if devices and not radiators:
            raise UpdateFailed("Could not read the state of any radiator")

        await self._async_wake_silent_radiators(
            radiators, {device.id: device for device in devices}
        )

        return GkpHomeData(
            homes=homes, radiators=radiators, raw_installations=installations
        )

    def _build_radiator(
        self,
        meta: _DeviceMeta,
        content: dict[str, Any],
        raw_content: dict[str, Any],
        stale: bool,
    ) -> Radiator:
        """Build a radiator from its normalized content."""
        reported = content.get(KEY_DEVICE_TIME)
        last_report = report_time(raw_content, content, self.device_zone)
        if (
            last_report is None
            and reported is not None
            and meta.id not in self._unparsed_time
        ):
            self._unparsed_time.add(meta.id)
            _LOGGER.warning(
                "Radiator %s (%s) reports its timestamp in an unexpected format"
                " (%.200r); its online state cannot be determined",
                meta.id,
                meta.room_name,
                reported,
            )
        return Radiator(
            id=meta.id,
            home_id=meta.home_id,
            home_name=meta.home_name,
            room_id=meta.room_id,
            room_name=meta.room_name,
            content=content,
            model=_model(content, meta.model_hex),
            last_report=last_report,
            stale=stale,
            raw_content=raw_content,
        )

    def _needs_wake(
        self, radiator: Radiator, now: datetime, interval: timedelta
    ) -> bool:
        """Return True if a radiator has been silent long enough to be nudged.

        Radiators in boost, radiators still applying a mode change and radiators
        whose state could not be read are left alone.
        """
        mode = radiator.demanded_mode
        if (
            radiator.stale
            or mode is None
            or mode == MODE_BOOST
            or radiator.current_mode != mode
        ):
            return False
        if (
            radiator.last_report is not None
            and device_time_age(radiator.last_report, now) < interval
        ):
            return False
        last_wake = self._last_wake.get(radiator.id)
        return last_wake is None or now - last_wake >= interval

    async def _async_wake_silent_radiators(
        self, radiators: dict[str, Radiator], metas: dict[str, _DeviceMeta]
    ) -> None:
        """Nudge radiators that have not reported for a while and re-read them.

        The radiators report on their own only when the app wakes them or
        something changes. Re-sending a radiator's current mode changes nothing
        on the radiator but makes it report, room temperature included.
        """
        if (interval := wake_interval(self.config_entry)) is None:
            return
        now = dt_util.utcnow()
        woken: list[Radiator] = []
        for radiator in radiators.values():
            mode = radiator.demanded_mode
            if mode is None or not self._needs_wake(radiator, now, interval):
                continue
            try:
                await self.api.async_set_mode(radiator.id, mode)
            except InvalidAuth as err:
                raise ConfigEntryAuthFailed(
                    "GKP Home rejected the saved session"
                ) from err
            except GkpHomeError as err:
                _LOGGER.debug(
                    "Could not wake radiator %s (%s): %s",
                    radiator.id,
                    radiator.room_name,
                    err,
                )
                continue
            self._last_wake[radiator.id] = now
            woken.append(radiator)
        if not woken:
            return

        _LOGGER.debug("Woke %d silent radiator(s); reading them again", len(woken))
        await asyncio.sleep(WAKE_SETTLE_DELAY)
        results = await asyncio.gather(
            *(self._async_fetch_content(radiator.id) for radiator in woken),
            return_exceptions=True,
        )
        for radiator, result in zip(woken, results, strict=True):
            if isinstance(result, BaseException):
                _LOGGER.debug(
                    "Reading radiator %s again after waking it failed: %s",
                    radiator.id,
                    result,
                )
                continue
            radiators[radiator.id] = self._build_radiator(
                metas[radiator.id], normalize_content(result), result, False
            )

    async def _async_ping_installations(self, homes: dict[str, Home]) -> bool:
        """Send the app-open ping for every installation; True if any succeeded."""
        pinged = False
        for home in homes.values():
            try:
                await self.api.async_user_connected(self.sub, home.id)
            except InvalidAuth as err:
                raise ConfigEntryAuthFailed(
                    "GKP Home rejected the saved session"
                ) from err
            except GkpHomeError as err:
                self.ping_results[home.id] = f"failed: {err}"
                if home.id not in self._warned_pings:
                    self._warned_pings.add(home.id)
                    _LOGGER.warning(
                        "The user-connected ping for installation %s failed (%s);"
                        " the radiators may not report fresh values",
                        home.name,
                        err,
                    )
            else:
                self.ping_results[home.id] = "ok"
                pinged = True
        return pinged

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
    if model := _as_str(content.get(KEY_MODEL)):
        return model
    return hex_ascii_to_str(model_hex) if isinstance(model_hex, str) else None
