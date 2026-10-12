"""Login flow against a real local HTTP server.

aioresponses bypasses aiohttp's cookie handling and URL encoding, so this test
runs the full login flow against an in-process identity server and checks the
raw request line, cookies and redirects end to end.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from pytest_homeassistant_custom_component.common import load_fixture

from custom_components.gkp_home.api import GkpHomeApi, InvalidAuth
from custom_components.gkp_home.const import (
    CLIENT_ID,
    USER_AGENT_WEBVIEW,
    X_REQUESTED_WITH,
)

from .helpers import (
    APIM_KEY,
    CLIENT_SECRET,
    EMAIL,
    EXPECTED_CODE_BODY,
    EXPECTED_LOGIN_BODY,
    EXPECTED_RETURN_URL,
    PASSWORD,
    STATE,
    SUB,
    token_response,
)

AUTHORIZE_PATH = (
    f"/connect/authorize/callback?client_id={CLIENT_ID}&scope=openid%20profile"
)


class FakeIdentityServer:
    """A minimal Duende-like identity server."""

    def __init__(self) -> None:
        """Initialize the request log."""
        self.seen: dict[str, Any] = {}
        self.reject_password = False
        app = web.Application()
        app.router.add_get("/Account/Login", self.login_page)
        app.router.add_post("/Account/Login", self.login_post)
        app.router.add_get("/connect/authorize/callback", self.authorize)
        app.router.add_post("/connect/token", self.token)
        self.server = TestServer(app)

    async def login_page(self, request: web.Request) -> web.Response:
        """Serve the login form and set the antiforgery cookie."""
        self.seen["login_raw_path"] = request.raw_path
        self.seen["login_headers"] = dict(request.headers)
        response = web.Response(
            text=load_fixture("login_page.html"), content_type="text/html"
        )
        response.set_cookie("idsrv.antiforgery", "af-cookie-1", httponly=True)
        return response

    async def login_post(self, request: web.Request) -> web.Response:
        """Validate the posted form and sign the user in."""
        self.seen["post_raw_path"] = request.raw_path
        self.seen["post_cookies"] = dict(request.cookies)
        self.seen["post_body"] = await request.text()
        self.seen["post_headers"] = dict(request.headers)
        if self.reject_password:
            return web.Response(
                text=load_fixture("login_page_error.html"), content_type="text/html"
            )
        response = web.Response(status=302, headers={"Location": AUTHORIZE_PATH})
        response.set_cookie("idsrv.session", "session-cookie-1")
        return response

    async def authorize(self, request: web.Request) -> web.Response:
        """Redirect back to the app with the authorization code."""
        self.seen["authorize_raw_path"] = request.raw_path
        self.seen["authorize_cookies"] = dict(request.cookies)
        return web.Response(
            status=302,
            headers={
                "Location": f"gkp://callback#code=CODE123&id_token=abc&state={STATE}"
            },
        )

    async def token(self, request: web.Request) -> web.Response:
        """Exchange the code for tokens."""
        self.seen["token_body"] = await request.text()
        self.seen["token_headers"] = dict(request.headers)
        return web.json_response(token_response())

    @property
    def host(self) -> str:
        """Return the base URL of the server."""
        return str(self.server.make_url("")).rstrip("/")


@pytest.fixture
async def identity_server(socket_enabled: None) -> AsyncGenerator[FakeIdentityServer]:
    """Run the fake identity server (needs real sockets on 127.0.0.1)."""
    fake = FakeIdentityServer()
    await fake.server.start_server()
    try:
        yield fake
    finally:
        await fake.server.close()


def login_session() -> aiohttp.ClientSession:
    """Create a login session that accepts cookies from 127.0.0.1."""
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))


async def test_login_end_to_end(
    identity_server: FakeIdentityServer, fixed_random: None
) -> None:
    async with aiohttp.ClientSession() as session:
        api = GkpHomeApi(
            session,
            CLIENT_SECRET,
            APIM_KEY,
            identity_host=identity_server.host,
            login_session_factory=login_session,
        )
        token = await api.async_login(EMAIL, PASSWORD)

    assert token.sub == SUB
    seen = identity_server.seen
    # The login URL is sent byte for byte like the app does it.
    assert seen["login_raw_path"] == f"/Account/Login?ReturnUrl={EXPECTED_RETURN_URL}"
    assert seen["login_headers"]["User-Agent"] == USER_AGENT_WEBVIEW
    assert seen["login_headers"]["X-Requested-With"] == X_REQUESTED_WITH
    # The form is posted to its action with the antiforgery cookie.
    assert seen["post_raw_path"] == (
        "/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D"
        "835295d3-75c5-470c-82e1-9919f74c00c2"
    )
    assert seen["post_cookies"] == {"idsrv.antiforgery": "af-cookie-1"}
    assert seen["post_body"] == EXPECTED_LOGIN_BODY
    assert seen["post_headers"]["Origin"] == identity_server.host
    assert seen["post_headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert seen["post_headers"]["Referer"] == (
        f"{identity_server.host}/Account/Login?ReturnUrl={EXPECTED_RETURN_URL}"
    )
    # The redirect chain carries the session cookie and the query untouched.
    assert seen["authorize_raw_path"] == AUTHORIZE_PATH
    assert seen["authorize_cookies"] == {
        "idsrv.antiforgery": "af-cookie-1",
        "idsrv.session": "session-cookie-1",
    }
    # The code is exchanged with the exact body.
    assert seen["token_body"] == EXPECTED_CODE_BODY
    assert seen["token_headers"]["Ocp-Apim-Subscription-Key"] == APIM_KEY
    assert seen["token_headers"]["Authorization"].startswith("Basic ")
    assert seen["token_headers"]["User-Agent"] == "okhttp/4.9.2"


async def test_login_bad_password_end_to_end(
    identity_server: FakeIdentityServer, fixed_random: None
) -> None:
    identity_server.reject_password = True
    async with aiohttp.ClientSession() as session:
        api = GkpHomeApi(
            session,
            CLIENT_SECRET,
            APIM_KEY,
            identity_host=identity_server.host,
            login_session_factory=login_session,
        )
        with pytest.raises(InvalidAuth, match="Invalid username or password"):
            await api.async_login(EMAIL, "wrong")
    assert "token_body" not in identity_server.seen
