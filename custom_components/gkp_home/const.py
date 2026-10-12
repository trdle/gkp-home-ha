"""Constants for the GKP Home integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "gkp_home"
MANUFACTURER: Final = "Gnosjö Klimatprodukter (Watts)"

# Cloud hosts
IDENTITY_HOST: Final = "https://gkp-id-prd.azurewebsites.net"
API_HOST: Final = "https://gkp-apim-prd.azure-api.net"

# OAuth constants embedded in the GKP Home app (the client secret and the APIM
# subscription key are NOT embedded here; the user enters them in the config flow).
CLIENT_ID: Final = "835295d3-75c5-470c-82e1-9919f74c00c2"
REDIRECT_URI: Final = "gkp://callback"
OEM_CLIENT: Final = "gkp"
SCOPE: Final = (
    "profile openid offline_access "
    "DataProcessing.Read DataProcessing.Write "
    "Device.Read Device.Write "
    "Installation.Read Installation.Write "
    "IOTManagement.Read IOTManagement.Write "
    "Room.Read Room.Write "
    "TermOfUse.Read TermOfUse.Write"
)

USER_AGENT_API: Final = "okhttp/4.9.2"
USER_AGENT_WEBVIEW: Final = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/UQ1A.240205.004; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/129.0.6668.100 Mobile Safari/537.36"
)
X_REQUESTED_WITH: Final = "com.watts.gkp.mobile.prd"

# Config entry data / options keys
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_APIM_KEY: Final = "apim_key"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SUB: Final = "sub"
CONF_POLL_INTERVAL: Final = "poll_interval"

DEFAULT_POLL_INTERVAL: Final = 60
MIN_POLL_INTERVAL: Final = 30

# Delay between a write and the follow-up refresh that confirms it.
WRITE_REFRESH_DELAY: Final = 3.0
# Maximum number of concurrent device content requests per refresh cycle.
CONTENT_CONCURRENCY: Final = 4

# Radiator modes (Dm = demanded mode, Cm = current mode)
MODE_OFF: Final = 0
MODE_ECO: Final = 1
MODE_AUTO: Final = 2
MODE_COMFORT: Final = 3
MODE_FROST: Final = 4
MODE_BOOST: Final = 5

# Content key holding the setpoint of each mode (off/auto have none).
SETPOINT_KEYS: Final[dict[int, str]] = {
    MODE_ECO: "ec",
    MODE_COMFORT: "cf",
    MODE_FROST: "df",
    MODE_BOOST: "bo",
}
DEFAULT_BOOST_MINUTES: Final = 60

# Device content keys
KEY_ROOM_TEMP: Final = "At"
KEY_BOOST_MINUTES: Final = "Bt"
KEY_CURRENT_MODE: Final = "Cm"
KEY_DEMANDED_MODE: Final = "Dm"
KEY_DEVICE_TIME: Final = "Dd"
KEY_HEATING: Final = "Hs"
KEY_RATED_POWER: Final = "Pw"
KEY_ROOM_NAME: Final = "Rn"
KEY_SETPOINT: Final = "Sp"
KEY_FIRMWARE: Final = "Sv"
KEY_MODEL: Final = "Ty"
KEY_WATTS_TYPE: Final = "wattsType"
KEY_WATTS_VALUE: Final = "wattsTypeValue"

# Installation payload keys
KEY_INSTALLATION_ID: Final = "In"
KEY_INSTALLATION_NAME: Final = "Il"
KEY_ROOMS: Final = "rooms"
KEY_ROOM_ID: Final = "Zn"
KEY_DEVICES: Final = "devices"
KEY_DEVICE_ID: Final = "Id_deviceId"

# A radiator reports its state when the cloud is polled; a device timestamp (Dd)
# older than this means the radiator is offline.
OFFLINE_AFTER: Final = timedelta(minutes=30)
DEVICE_TIMEZONE: Final = "Europe/Stockholm"
DEVICE_TIME_FORMAT: Final = "%d/%m/%Y %H:%M:%S"

# Climate entity limits
MIN_TEMP: Final = 5.0
MAX_TEMP: Final = 30.0
TEMP_STEP: Final = 0.5

PRESET_FROST_PROTECTION: Final = "frost_protection"
