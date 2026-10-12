"""Async client for the GKP Home cloud.

The GKP Home cloud (run by Watts Water Technologies on Azure) consists of a
Duende IdentityServer for authentication and an Azure API Management gateway
for the device APIs. Everything in this module was reverse-engineered from the
GKP Home Android app (v2.4.56).

Login replicates the app's WebView flow server-side: an authorization code +
PKCE (S256) request packed into the identity server's ``/Account/Login`` page,
the login form posted with the user's credentials, and the resulting
``gkp://callback`` redirect exchanged for tokens. The password is only used for
that single login; afterwards only the refresh token is needed.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
from html.parser import HTMLParser
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import aiohttp
from yarl import URL

from .const import (
    API_HOST,
    CLIENT_ID,
    DEFAULT_BOOST_MINUTES,
    IDENTITY_HOST,
    KEY_BOOST_MINUTES,
    KEY_DEMANDED_MODE,
    KEY_WATTS_TYPE,
    KEY_WATTS_VALUE,
    MODE_BOOST,
    REDIRECT_URI,
    SCOPE,
    SETPOINT_KEYS,
    USER_AGENT_API,
    USER_AGENT_WEBVIEW,
    X_REQUESTED_WITH,
)

_LOGGER = logging.getLogger(__name__)

LOGIN_PATH = "/Account/Login"
TOKEN_PATH = "/connect/token"  # noqa: S105 - URL path, not a secret
INSTALLATIONS_PATH = "/businessmodule/v1/installations/admins/{sub}"
CONTENT_PATH = "/iotmanagement/v1/configuration/{device_id}/{device_id}/v1/content/"
CONFIG_REPLACE_PATH = "/iotmanagement/v1/devices/twin/properties/config/replace"
INSTALLATION_CONTROL_PATH = "/iotmanagement/v1/devices/installationcontroljob"
USER_CONNECTED_PATH = "/iotmanagement/v1/devices/userconnected"

MAX_REDIRECTS = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Refresh the access token this many seconds before it actually expires.
TOKEN_EXPIRY_MARGIN = 60
DEFAULT_TOKEN_LIFETIME = 3600

ACCEPT_JSON = "application/json, text/plain, */*"
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_ERROR_CLASS_MARKERS = (
    "validation-summary-errors",
    "field-validation-error",
    "alert-danger",
    "text-danger",
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class GkpHomeError(Exception):
    """Base class for GKP Home client errors."""


class InvalidAuth(GkpHomeError):
    """The credentials, client secret or refresh token were rejected."""


class CannotConnect(GkpHomeError):
    """The cloud could not be reached."""


class ApiError(GkpHomeError):
    """The cloud returned an unexpected response."""

    def __init__(self, status: int, body: str, message: str | None = None) -> None:
        """Initialize the error with the HTTP status and response body."""
        super().__init__(message or f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class TokenSet:
    """Tokens obtained from the identity server."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    sub: str
    id_token: str | None = None

    @property
    def is_expired(self) -> bool:
        """Return True if the access token should be refreshed."""
        return time.time() >= self.expires_at - TOKEN_EXPIRY_MARGIN


@dataclass
class LoginForm:
    """The parsed identity server login form."""

    action: str | None
    fields: list[tuple[str, str]] = field(default_factory=list)


def b64url(data: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def decode_jwt_claims(token: str | None) -> dict[str, Any]:
    """Return the (unverified) claims of a JWT, or an empty dict."""
    if not token:
        return {}
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def build_login_url(
    client_secret: str,
    nonce: str,
    code_challenge: str,
    state: str,
    *,
    identity_host: str = IDENTITY_HOST,
) -> str:
    """Build the identity server login URL exactly like the app does.

    The authorize request is packed into ``ReturnUrl``. The inner authorize URL is
    URL-encoded once (``code id_token`` -> ``code%20id_token``) and the whole
    inner URL once more as the ReturnUrl value (-> ``%2520``), except that the
    app leaves ``gkp://callback`` unencoded.
    """
    # Encode the secret like every other inner value: once for the authorize
    # query string and once more for the ReturnUrl wrapper. For the usual
    # alphanumeric secrets this is identical to a single encoding.
    secret = quote(quote(client_secret, safe=""), safe="")
    scope = SCOPE.replace(" ", "%2520")
    return_url = (
        "%2Fconnect%2Fauthorize%2Fcallback%3F"
        f"client_id%3D{CLIENT_ID}"
        f"%26client_secret%3D{secret}"
        "%26response_type%3Dcode%2520id_token"
        f"%26scope%3D{scope}"
        f"%26redirect_uri%3D{REDIRECT_URI}"
        f"%26nonce%3D{nonce}"
        f"%26code_challenge%3D{code_challenge}"
        "%26code_challenge_method%3DS256"
        f"%26state%3D{state}"
        "%26oemclient%3Dgkp"
    )
    return f"{identity_host}{LOGIN_PATH}?ReturnUrl={return_url}"


class _LoginPageParser(HTMLParser):
    """Extract forms and validation messages from the login page."""

    def __init__(self) -> None:
        """Initialize the parser."""
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._form: dict[str, Any] | None = None
        self._error_depth = 0
        self._error_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track forms, inputs, buttons and validation containers."""
        attributes = {key: value or "" for key, value in attrs}
        if self._error_depth:
            if tag not in _VOID_TAGS:
                self._error_depth += 1
        elif any(
            marker in attributes.get("class", "") for marker in _ERROR_CLASS_MARKERS
        ):
            self._error_depth = 1
            self._error_text = []

        if tag == "form":
            self._form = {
                "action": attributes.get("action"),
                "inputs": [],
                "buttons": [],
            }
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input":
            self._form["inputs"].append(
                {
                    "name": attributes.get("name", ""),
                    "type": attributes.get("type", "text").lower(),
                    "value": attributes.get("value", ""),
                }
            )
        elif tag == "button":
            self._form["buttons"].append(
                {
                    "name": attributes.get("name", ""),
                    "type": attributes.get("type", "submit").lower(),
                    "value": attributes.get("value", ""),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        """Close validation containers and forms."""
        if self._error_depth and tag not in _VOID_TAGS:
            self._error_depth -= 1
            if self._error_depth == 0:
                text = " ".join("".join(self._error_text).split())
                if text:
                    self.errors.append(text)
        if tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        """Collect text inside validation containers."""
        if self._error_depth:
            self._error_text.append(data)


def parse_login_page(html: str) -> tuple[LoginForm | None, list[str]]:
    """Parse the login page.

    Returns the login form (the first form with a password field) with the
    hidden inputs and the names of the username/password fields and the submit
    button, plus any validation messages found on the page.
    """
    parser = _LoginPageParser()
    parser.feed(html)
    parser.close()
    for form in parser.forms:
        if not any(inp["type"] == "password" for inp in form["inputs"]):
            continue
        return (
            LoginForm(
                action=form["action"] or None,
                fields=_login_fields(form),
            ),
            parser.errors,
        )
    return None, parser.errors


def _login_fields(form: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the form fields as (name, value) with placeholders for credentials."""
    fields: list[tuple[str, str]] = []
    username_done = False
    password_done = False
    for inp in form["inputs"]:
        name, kind, value = inp["name"], inp["type"], inp["value"]
        if not name:
            continue
        if kind == "hidden":
            fields.append((name, value))
        elif kind in ("text", "email") and not username_done:
            fields.append((name, "{username}"))
            username_done = True
        elif kind == "password" and not password_done:
            fields.append((name, "{password}"))
            password_done = True
    buttons = [
        btn for btn in form["buttons"] if btn["name"] and btn["type"] == "submit"
    ]
    login_buttons = [btn for btn in buttons if btn["value"].lower() == "login"]
    if button := next(iter(login_buttons or buttons), None):
        fields.append((button["name"], button["value"]))
    return fields


def parse_callback(location: str, expected_state: str | None = None) -> str:
    """Extract the authorization code from a ``gkp://callback`` redirect."""
    parts = urlsplit(location)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params.update(parse_qsl(parts.fragment, keep_blank_values=True))
    if error := params.get("error"):
        description = params.get("error_description", "")
        raise InvalidAuth(f"Login failed: {error} {description}".strip())
    code = params.get("code")
    if not code:
        raise ApiError(302, location, "Callback redirect carries no authorization code")
    if expected_state is not None and params.get("state") != expected_state:
        raise ApiError(302, location, "Callback redirect has an unexpected state")
    return code


@dataclass
class _Navigation:
    """Where a chain of manually followed redirects ended."""

    callback: str | None = None
    url: URL | None = None
    status: int = 0
    text: str = ""


def _default_login_session() -> aiohttp.ClientSession:
    """Create a throwaway session with its own cookie jar for the login flow."""
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(), timeout=REQUEST_TIMEOUT
    )


class GkpHomeApi:
    """Client for the GKP Home identity server and device API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_secret: str,
        apim_key: str,
        *,
        refresh_token: str | None = None,
        login_session_factory: Callable[[], aiohttp.ClientSession] | None = None,
        token_listener: Callable[[TokenSet], None] | None = None,
        identity_host: str = IDENTITY_HOST,
        api_host: str = API_HOST,
    ) -> None:
        """Initialize the client.

        ``session`` is used for the token endpoint and the device API.
        ``login_session_factory`` must return a fresh session with its own
        cookie jar; it is created and closed for every login.
        """
        self._session = session
        self._client_secret = client_secret
        self._apim_key = apim_key
        self._refresh_token = refresh_token
        self._login_session_factory = login_session_factory or _default_login_session
        self._token_listener = token_listener
        self._identity_host = identity_host.rstrip("/")
        self._api_host = api_host.rstrip("/")
        self._token: TokenSet | None = None
        self._refresh_lock = asyncio.Lock()

    @property
    def token(self) -> TokenSet | None:
        """Return the current token set, if any."""
        return self._token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def sub(self) -> str | None:
        """Return the user id (``sub`` claim) of the signed-in user."""
        return self._token.sub if self._token else None

    # ------------------------------------------------------------------ login

    async def async_login(self, username: str, password: str) -> TokenSet:
        """Sign in with the user's GKP account and obtain tokens.

        The password is only sent to the identity server's login form; it is
        not stored anywhere.
        """
        verifier, challenge = generate_pkce()
        nonce = b64url(secrets.token_bytes(16))
        state = b64url(secrets.token_bytes(16))
        login_url = URL(
            build_login_url(
                self._client_secret,
                nonce,
                challenge,
                state,
                identity_host=self._identity_host,
            ),
            encoded=True,
        )

        session = self._login_session_factory()
        try:
            try:
                nav = await self._async_navigate(session, login_url)
                if nav.callback is None:
                    nav = await self._async_submit_login(
                        session, nav, username, password
                    )
            except TimeoutError as err:
                raise CannotConnect("Timeout while signing in") from err
            except aiohttp.ClientError as err:
                raise CannotConnect(
                    f"Connection error while signing in: {err}"
                ) from err
        finally:
            await session.close()

        if nav.callback is None:
            raise CannotConnect("Login did not redirect back to the app")
        code = parse_callback(nav.callback, state)
        body = (
            "grant_type=authorization_code"
            f"&code={quote(code, safe='')}"
            f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
            f"&code_verifier={verifier}"
        )
        return await self._async_token_request(body)

    async def _async_submit_login(
        self,
        session: aiohttp.ClientSession,
        page: _Navigation,
        username: str,
        password: str,
    ) -> _Navigation:
        """Post the credentials to the login form and follow the redirects."""
        if page.status != 200 or page.url is None:
            raise ApiError(page.status, page.text, "Unexpected response for login page")
        form, errors = parse_login_page(page.text)
        if form is None:
            raise ApiError(page.status, page.text, "No login form found on login page")
        fields = [
            (name, value.format(username=username, password=password))
            if value in ("{username}", "{password}")
            else (name, value)
            for name, value in form.fields
        ]
        action = (
            page.url.join(URL(form.action, encoded=True)) if form.action else page.url
        )
        nav = await self._async_navigate(
            session,
            action,
            data=urlencode(fields),
            referer=str(page.url),
        )
        if nav.callback is None:
            # The identity server re-renders the login page on bad credentials.
            _, errors = parse_login_page(nav.text) if nav.status == 200 else (None, [])
            if nav.status == 200:
                raise InvalidAuth(
                    "; ".join(errors) or "Login rejected by identity server"
                )
            raise ApiError(nav.status, nav.text, "Unexpected response to login")
        return nav

    async def _async_navigate(
        self,
        session: aiohttp.ClientSession,
        url: URL,
        *,
        data: str | None = None,
        referer: str | None = None,
    ) -> _Navigation:
        """Request a page and follow HTTP redirects manually.

        Stops at a redirect to the app's ``gkp://callback`` URI, which aiohttp
        could not follow and which carries the authorization code.
        """
        method = "POST" if data is not None else "GET"
        for _ in range(MAX_REDIRECTS + 1):
            headers = {
                "User-Agent": USER_AGENT_WEBVIEW,
                "X-Requested-With": X_REQUESTED_WITH,
                "Accept": ACCEPT_HTML,
                "Accept-Language": "en-US,en;q=0.9",
            }
            if referer:
                headers["Referer"] = referer
            if method == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                headers["Origin"] = self._identity_host
            async with session.request(
                method,
                url,
                data=data,
                headers=headers,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                location = resp.headers.get("Location")
                if resp.status in _REDIRECT_STATUSES and location:
                    if location.startswith(REDIRECT_URI):
                        return _Navigation(callback=location)
                    referer = str(url)
                    url = url.join(URL(location, encoded=True))
                    method, data = "GET", None
                    continue
                return _Navigation(url=url, status=resp.status, text=await resp.text())
        raise CannotConnect("Too many redirects while signing in")

    # ----------------------------------------------------------------- tokens

    async def async_refresh_token(self) -> TokenSet:
        """Obtain a new access token with the stored refresh token."""
        if not self._refresh_token:
            raise InvalidAuth("No refresh token available")
        body = f"grant_type=refresh_token&refresh_token={quote(self._refresh_token, safe='')}"
        return await self._async_token_request(body)

    async def _async_token_request(self, body: str) -> TokenSet:
        """Call the token endpoint with a form-encoded body."""
        basic = base64.b64encode(f"{CLIENT_ID}:{self._client_secret}".encode()).decode(
            "ascii"
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "Ocp-Apim-Subscription-Key": self._apim_key,
            "Accept": ACCEPT_JSON,
            "User-Agent": USER_AGENT_API,
        }
        try:
            async with self._session.post(
                f"{self._identity_host}{TOKEN_PATH}",
                data=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except TimeoutError as err:
            raise CannotConnect("Timeout while requesting tokens") from err
        except aiohttp.ClientError as err:
            raise CannotConnect(
                f"Connection error while requesting tokens: {err}"
            ) from err

        if status in (400, 401, 403):
            error = _json_or_empty(text).get("error", "")
            raise InvalidAuth(
                f"Token request rejected ({status}): {error or text[:100]}"
            )
        if status >= 400:
            raise ApiError(status, text, f"Token request failed with HTTP {status}")

        payload = _json_or_empty(text)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ApiError(status, text, "Token response has no access token")
        claims = decode_jwt_claims(access_token)
        id_token = payload.get("id_token")
        sub = claims.get("sub") or decode_jwt_claims(id_token).get("sub")
        if not sub:
            raise ApiError(status, text, "Token response has no subject claim")
        expires_at = claims.get("exp")
        if not isinstance(expires_at, int | float):
            expires_at = time.time() + payload.get("expires_in", DEFAULT_TOKEN_LIFETIME)
        # Keep the previous refresh token when the response does not rotate it.
        refresh_token = payload.get("refresh_token") or self._refresh_token
        self._token = TokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(expires_at),
            sub=str(sub),
            id_token=id_token,
        )
        self._refresh_token = refresh_token
        if self._token_listener is not None:
            self._token_listener(self._token)
        return self._token

    async def _async_ensure_token(self) -> TokenSet:
        """Return a valid access token, refreshing it when needed."""
        async with self._refresh_lock:
            if self._token is None or self._token.is_expired:
                await self.async_refresh_token()
            assert self._token is not None  # noqa: S101 - narrowed above
            return self._token

    async def _async_refresh_after_unauthorized(self, used_token: str) -> None:
        """Refresh after a 401 unless another request already did so."""
        async with self._refresh_lock:
            if self._token is not None and self._token.access_token != used_token:
                return
            await self.async_refresh_token()

    # -------------------------------------------------------------- device API

    def _api_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Ocp-Apim-Subscription-Key": self._apim_key,
            "Accept": ACCEPT_JSON,
            "User-Agent": USER_AGENT_API,
        }

    async def _async_request(
        self, method: str, path: str, json_body: Any | None = None
    ) -> Any:
        """Call the device API; on 401 refresh the token once and retry."""
        token = await self._async_ensure_token()
        access_token = token.access_token
        for attempt in range(2):
            try:
                async with self._session.request(
                    method,
                    f"{self._api_host}{path}",
                    json=json_body,
                    headers=self._api_headers(access_token),
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            except TimeoutError as err:
                raise CannotConnect(f"Timeout calling {path}") from err
            except aiohttp.ClientError as err:
                raise CannotConnect(f"Connection error calling {path}: {err}") from err

            _LOGGER.debug("%s %s -> %s", method, path, status)
            if status == 401 and attempt == 0:
                await self._async_refresh_after_unauthorized(access_token)
                assert self._token is not None  # noqa: S101 - set by refresh
                access_token = self._token.access_token
                continue
            if status >= 400:
                raise ApiError(status, text)
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError as err:
                raise ApiError(status, text, "Invalid JSON in response") from err
        raise ApiError(401, "", "Unauthorized after token refresh")  # pragma: no cover

    async def async_get_installations(self, sub: str) -> list[dict[str, Any]]:
        """Return the installations (homes) with their rooms and devices."""
        result = await self._async_request("GET", INSTALLATIONS_PATH.format(sub=sub))
        if not isinstance(result, list):
            raise ApiError(200, json.dumps(result), "Unexpected installations payload")
        return result

    async def async_get_device_content(self, device_id: str) -> dict[str, Any]:
        """Return the current state (content) of a radiator."""
        result = await self._async_request(
            "GET", CONTENT_PATH.format(device_id=device_id)
        )
        if not isinstance(result, dict):
            raise ApiError(200, json.dumps(result), "Unexpected device content payload")
        return result

    async def async_write_config(
        self, device_id: str, data: list[dict[str, Any]]
    ) -> Any:
        """Write configuration values (``wattsType``/``wattsTypeValue``) to a radiator."""
        body = {
            "Id_deviceId": device_id,
            "S1": device_id,
            "configurationVersion": "v1.0",
            "data": data,
        }
        return await self._async_request("PUT", CONFIG_REPLACE_PATH, body)

    async def async_set_mode(
        self, device_id: str, mode: int, boost_minutes: int | None = None
    ) -> Any:
        """Set the demanded mode (``Dm``); boost also writes its duration (``Bt``)."""
        data = [{KEY_WATTS_TYPE: KEY_DEMANDED_MODE, KEY_WATTS_VALUE: mode}]
        if mode == MODE_BOOST:
            data.append(
                {
                    KEY_WATTS_TYPE: KEY_BOOST_MINUTES,
                    KEY_WATTS_VALUE: boost_minutes or DEFAULT_BOOST_MINUTES,
                }
            )
        return await self.async_write_config(device_id, data)

    async def async_set_setpoint(self, device_id: str, mode: int, value: int) -> Any:
        """Set the setpoint (tenths of °F) of ``mode`` and make that mode active."""
        try:
            key = SETPOINT_KEYS[mode]
        except KeyError:
            raise ValueError(f"Mode {mode} has no setpoint") from None
        data = [
            {KEY_WATTS_TYPE: KEY_DEMANDED_MODE, KEY_WATTS_VALUE: mode},
            {KEY_WATTS_TYPE: key, KEY_WATTS_VALUE: value},
        ]
        return await self.async_write_config(device_id, data)

    async def async_set_installation_mode(
        self, installation_id: str, sub: str, mode: int
    ) -> Any:
        """Set the mode of every radiator in an installation at once."""
        body = {
            "In": installation_id,
            "A1": sub,
            "data": [
                {"timestamp": None, KEY_WATTS_TYPE: "Cg", KEY_WATTS_VALUE: [mode, 0, 0]}
            ],
        }
        return await self._async_request("PUT", INSTALLATION_CONTROL_PATH, body)

    async def async_user_connected(self, sub: str, installation_id: str) -> Any:
        """Tell the cloud a user is looking at the installation (app open ping)."""
        return await self._async_request(
            "PUT", USER_CONNECTED_PATH, {"A1": sub, "In": installation_id}
        )


def _json_or_empty(text: str) -> dict[str, Any]:
    """Parse a JSON object leniently."""
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
