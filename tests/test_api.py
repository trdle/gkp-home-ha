"""Tests for the GKP Home cloud client against a mocked cloud.

Home Assistant's ``AiohttpClientMocker`` stands in for the identity server and
the API gateway; ``test_api_server.py`` covers the same login flow against a
real local HTTP server.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, Iterable
import json
import re
import time
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import load_fixture
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.gkp_home.api import (
    ApiError,
    CannotConnect,
    GkpHomeApi,
    InvalidAuth,
    TokenSet,
    build_login_url,
    decode_jwt_claims,
    generate_pkce,
    parse_callback,
    parse_login_page,
)
from custom_components.gkp_home.const import (
    CLIENT_ID,
    IDENTITY_HOST,
    MODE_BOOST,
    MODE_COMFORT,
    MODE_ECO,
    MODE_OFF,
    USER_AGENT_API,
    USER_AGENT_WEBVIEW,
    X_REQUESTED_WITH,
)

from .helpers import (
    APIM_KEY,
    CHALLENGE,
    CLIENT_SECRET,
    CONFIG_REPLACE_URL,
    EMAIL,
    EXPECTED_CODE_BODY,
    EXPECTED_LOGIN_BODY,
    EXPECTED_LOGIN_URL,
    INSTALLATION_CONTROL_URL,
    INSTALLATIONS_URL,
    NONCE,
    PASSWORD,
    STATE,
    SUB,
    TOKEN_URL,
    USER_CONNECTED_URL,
    content_url,
    make_jwt,
    token_response,
)

LOGIN_URL = f"{IDENTITY_HOST}/Account/Login"
AUTHORIZE_URL = f"{IDENTITY_HOST}/connect/authorize/callback"
CALLBACK_LOCATION = (
    f"gkp://callback#code=CODE123&id_token=abc&scope=openid&state={STATE}"
)
DEVICE_ID = "AABBCCDDEE01"
INSTALLATION_ID = "00000000-0000-0000-0000-000000000001"
BASIC_AUTH = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
API_HEADERS = {
    "Ocp-Apim-Subscription-Key": APIM_KEY,
    "Accept": "application/json, text/plain, */*",
    "User-Agent": USER_AGENT_API,
}


@pytest.fixture
def mocker() -> AiohttpClientMocker:
    """Provide the aiohttp request mocker."""
    return AiohttpClientMocker()


@pytest.fixture
async def session(mocker: AiohttpClientMocker) -> AsyncGenerator[aiohttp.ClientSession]:
    """Provide a client session bound to the mocker."""
    client_session = mocker.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def api(session: aiohttp.ClientSession, mocker: AiohttpClientMocker) -> GkpHomeApi:
    """Provide a client whose login sessions are bound to the mocker too."""
    return GkpHomeApi(
        session,
        CLIENT_SECRET,
        APIM_KEY,
        refresh_token="old",
        login_session_factory=lambda: mocker.create_session(asyncio.get_running_loop()),
    )


def calls_for(mocker: AiohttpClientMocker, method: str, path: str) -> list[tuple]:
    """Return the recorded (method, url, data, headers) calls for a URL path."""
    return [
        call
        for call in mocker.mock_calls
        if call[0].upper() == method and call[1].path == path
    ]


def sequence(
    mocker: AiohttpClientMocker, method: str, url: str, responses: Iterable[Any]
) -> None:
    """Answer successive requests to ``url`` with the given responses.

    Each item is either an exception to raise or keyword arguments for
    ``AiohttpClientMockResponse``.
    """
    items = iter(responses)

    async def side_effect(
        method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        await asyncio.sleep(0)  # yield so concurrent requests interleave like real I/O
        item = next(items)
        if isinstance(item, BaseException):
            return AiohttpClientMockResponse(method, url, exc=item)
        return AiohttpClientMockResponse(method, url, **item)

    mocker.request(method, url, side_effect=side_effect)


def mock_successful_login(mocker: AiohttpClientMocker) -> None:
    """Register the responses of a successful login."""
    mocker.get(
        LOGIN_URL,
        text=load_fixture("login_page.html"),
        headers={"Content-Type": "text/html"},
    )
    mocker.post(
        LOGIN_URL,
        status=302,
        headers={"Location": f"/connect/authorize/callback?client_id={CLIENT_ID}"},
    )
    mocker.get(AUTHORIZE_URL, status=302, headers={"Location": CALLBACK_LOCATION})
    mocker.post(TOKEN_URL, json=token_response())


# ----------------------------------------------------------------- helpers


def test_generate_pkce() -> None:
    verifier, challenge = generate_pkce()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", verifier)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
    assert verifier != challenge


def test_build_login_url_matches_app_template() -> None:
    assert build_login_url(CLIENT_SECRET, NONCE, CHALLENGE, STATE) == EXPECTED_LOGIN_URL


def test_build_login_url_encodes_secret() -> None:
    url = build_login_url("a b&c=d", "n", "c", "s")
    assert "client_secret%3Da%2520b%2526c%253Dd%26response_type" in url
    assert url.startswith(f"{IDENTITY_HOST}/Account/Login?ReturnUrl=%2Fconnect")


def test_decode_jwt_claims() -> None:
    assert decode_jwt_claims(make_jwt({"sub": "x", "exp": 1})) == {"sub": "x", "exp": 1}
    assert decode_jwt_claims("not-a-jwt") == {}
    assert decode_jwt_claims("a.!!!.c") == {}
    assert decode_jwt_claims(None) == {}
    assert decode_jwt_claims("") == {}


def test_parse_login_page() -> None:
    form, errors = parse_login_page(load_fixture("login_page.html"))
    assert errors == []
    assert form is not None
    assert form.action == (
        "/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D"
        "835295d3-75c5-470c-82e1-9919f74c00c2"
    )
    assert form.fields == [
        (
            "ReturnUrl",
            "/connect/authorize/callback?client_id=835295d3-75c5-470c-82e1-9919f74c00c2",
        ),
        ("Username", "{username}"),
        ("Password", "{password}"),
        ("__RequestVerificationToken", "CfDJ8dummyAntiForgeryToken"),
        ("RememberLogin", "false"),
        ("button", "login"),
    ]


def test_parse_login_page_errors() -> None:
    form, errors = parse_login_page(load_fixture("login_page_error.html"))
    assert form is not None
    assert errors == ["Invalid username or password"]


def test_parse_login_page_without_form() -> None:
    form, errors = parse_login_page(
        "<html><body><p>Down for maintenance</p></body></html>"
    )
    assert form is None
    assert errors == []


def test_parse_login_page_without_named_login_button() -> None:
    form, _ = parse_login_page(
        '<form action="/x"><input type="email" name="Email"><input type="password" name="Pw">'
        '<button type="button" name="other" value="1"></button>'
        '<button type="submit" name="submit" value="Sign in"></button></form>'
    )
    assert form is not None
    assert form.fields == [
        ("Email", "{username}"),
        ("Pw", "{password}"),
        ("submit", "Sign in"),
    ]


def test_parse_callback() -> None:
    assert parse_callback("gkp://callback#code=abc&state=s", "s") == "abc"
    assert parse_callback("gkp://callback?code=abc&state=s", "s") == "abc"
    assert parse_callback("gkp://callback?code=abc") == "abc"
    with pytest.raises(InvalidAuth, match="access_denied"):
        parse_callback("gkp://callback#error=access_denied&error_description=nope")
    with pytest.raises(ApiError, match="no authorization code"):
        parse_callback("gkp://callback#state=s", "s")
    with pytest.raises(ApiError, match="unexpected state"):
        parse_callback("gkp://callback#code=abc&state=other", "s")


# ------------------------------------------------------------------- login


async def test_login_success(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mock_successful_login(mocker)
    listener = MagicMock()
    api = GkpHomeApi(
        session,
        CLIENT_SECRET,
        APIM_KEY,
        token_listener=listener,
        login_session_factory=lambda: mocker.create_session(asyncio.get_running_loop()),
    )

    token = await api.async_login(EMAIL, PASSWORD)

    assert token.sub == SUB
    assert token.refresh_token == "refresh-token-1"
    assert not token.is_expired
    assert api.sub == SUB
    assert api.refresh_token == "refresh-token-1"
    assert api.token is token
    listener.assert_called_once_with(token)

    # 1. the login page is requested with the exact URL the app uses
    ((_, login_url, login_data, login_headers),) = calls_for(
        mocker, "GET", "/Account/Login"
    )
    assert str(login_url) == EXPECTED_LOGIN_URL
    assert login_data is None
    assert login_headers["User-Agent"] == USER_AGENT_WEBVIEW
    assert login_headers["X-Requested-With"] == X_REQUESTED_WITH
    assert "Authorization" not in login_headers

    # 2. the form is posted to its action with the credentials and antiforgery token
    ((_, post_url, post_data, post_headers),) = calls_for(
        mocker, "POST", "/Account/Login"
    )
    assert str(post_url) == (
        f"{LOGIN_URL}?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D{CLIENT_ID}"
    )
    assert post_data == EXPECTED_LOGIN_BODY
    assert post_headers["Origin"] == IDENTITY_HOST
    assert post_headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert post_headers["Referer"] == EXPECTED_LOGIN_URL
    assert post_headers["User-Agent"] == USER_AGENT_WEBVIEW
    assert post_headers["X-Requested-With"] == X_REQUESTED_WITH

    # 3. the redirect chain is followed manually up to gkp://callback
    ((_, authorize_url, _, authorize_headers),) = calls_for(
        mocker, "GET", "/connect/authorize/callback"
    )
    assert str(authorize_url) == f"{AUTHORIZE_URL}?client_id={CLIENT_ID}"
    assert authorize_headers["User-Agent"] == USER_AGENT_WEBVIEW
    assert authorize_headers["Referer"] == str(post_url)

    # 4. the code is exchanged with the exact body and headers
    ((_, _, token_data, token_headers),) = calls_for(mocker, "POST", "/connect/token")
    assert token_data == EXPECTED_CODE_BODY
    assert token_headers == {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {BASIC_AUTH}",
        **API_HEADERS,
    }


async def test_login_bad_password(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text=load_fixture("login_page.html"))
    mocker.post(LOGIN_URL, text=load_fixture("login_page_error.html"))

    with pytest.raises(InvalidAuth, match="Invalid username or password"):
        await api.async_login(EMAIL, "wrong")
    assert api.token is None
    assert calls_for(mocker, "POST", "/connect/token") == []


async def test_login_rejected_without_message(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text=load_fixture("login_page.html"))
    mocker.post(LOGIN_URL, text=load_fixture("login_page.html"))

    with pytest.raises(InvalidAuth, match="Login rejected"):
        await api.async_login(EMAIL, "wrong")


async def test_login_error_callback(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text=load_fixture("login_page.html"))
    mocker.post(
        LOGIN_URL,
        status=302,
        headers={
            "Location": "gkp://callback?error=access_denied&error_description=Denied"
        },
    )

    with pytest.raises(InvalidAuth, match="access_denied Denied"):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_state_mismatch(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text=load_fixture("login_page.html"))
    mocker.post(
        LOGIN_URL, status=302, headers={"Location": "gkp://callback#code=x&state=bad"}
    )

    with pytest.raises(ApiError, match="unexpected state"):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_page_without_form(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text="<html><body>Maintenance</body></html>")

    with pytest.raises(ApiError, match="No login form"):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_page_server_error(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, status=503, text="Service Unavailable")

    with pytest.raises(ApiError) as excinfo:
        await api.async_login(EMAIL, PASSWORD)
    assert excinfo.value.status == 503


async def test_login_unexpected_post_status(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, text=load_fixture("login_page.html"))
    mocker.post(LOGIN_URL, status=500, text="boom")

    with pytest.raises(ApiError, match="Unexpected response to login"):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_too_many_redirects(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, status=302, headers={"Location": "/Account/Login?loop=1"})

    with pytest.raises(CannotConnect, match="Too many redirects"):
        await api.async_login(EMAIL, PASSWORD)
    assert len(calls_for(mocker, "GET", "/Account/Login")) == 11


async def test_login_absolute_redirect_and_relative_action(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    page = load_fixture("login_page.html").replace(
        'action="/Account/Login?ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D835295d3-75c5-470c-82e1-9919f74c00c2"',
        'action="Login?x=1"',
    )
    # Register the more specific URL first: the mocker returns the first match.
    mocker.get(f"{LOGIN_URL}?moved=1", text=page)
    mocker.get(LOGIN_URL, status=302, headers={"Location": f"{LOGIN_URL}?moved=1"})
    mocker.post(f"{LOGIN_URL}?x=1", status=302, headers={"Location": CALLBACK_LOCATION})
    mocker.post(TOKEN_URL, json=token_response())

    await api.async_login(EMAIL, PASSWORD)

    assert [str(call[1]) for call in calls_for(mocker, "GET", "/Account/Login")] == [
        EXPECTED_LOGIN_URL,
        f"{LOGIN_URL}?moved=1",
    ]
    assert [str(call[1]) for call in calls_for(mocker, "POST", "/Account/Login")] == [
        f"{LOGIN_URL}?x=1"
    ]


async def test_login_connection_error(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, exc=aiohttp.ClientConnectionError("unreachable"))

    with pytest.raises(CannotConnect):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_timeout(
    api: GkpHomeApi, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, exc=TimeoutError())

    with pytest.raises(CannotConnect, match="Timeout"):
        await api.async_login(EMAIL, PASSWORD)


async def test_login_closes_login_session(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mock_successful_login(mocker)
    login_session = mocker.create_session(asyncio.get_running_loop())
    api = GkpHomeApi(
        session, CLIENT_SECRET, APIM_KEY, login_session_factory=lambda: login_session
    )

    await api.async_login(EMAIL, PASSWORD)

    assert login_session.closed
    assert not session.closed


async def test_login_closes_login_session_on_error(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker, fixed_random: None
) -> None:
    mocker.get(LOGIN_URL, exc=aiohttp.ClientConnectionError("unreachable"))
    login_session = mocker.create_session(asyncio.get_running_loop())
    api = GkpHomeApi(
        session, CLIENT_SECRET, APIM_KEY, login_session_factory=lambda: login_session
    )

    with pytest.raises(CannotConnect):
        await api.async_login(EMAIL, PASSWORD)

    assert login_session.closed


# ------------------------------------------------------------------ tokens


async def test_token_sub_falls_back_to_id_token(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    payload = token_response()
    payload["access_token"] = "opaque-reference-token"
    mocker.post(TOKEN_URL, json=payload)

    token = await api.async_refresh_token()

    assert token.sub == SUB
    assert token.expires_at == pytest.approx(time.time() + 3600, abs=5)


async def test_token_response_without_subject(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json={"access_token": "opaque", "expires_in": 10})

    with pytest.raises(ApiError, match="no subject"):
        await api.async_refresh_token()


async def test_token_response_without_access_token(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json={"token_type": "Bearer"})

    with pytest.raises(ApiError, match="no access token"):
        await api.async_refresh_token()


async def test_refresh_rotates_token(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response(refresh_token="refresh-token-2"))
    listener = MagicMock()
    api = GkpHomeApi(
        session, CLIENT_SECRET, APIM_KEY, refresh_token="old", token_listener=listener
    )

    token = await api.async_refresh_token()

    assert token.refresh_token == "refresh-token-2"
    assert api.refresh_token == "refresh-token-2"
    listener.assert_called_once_with(token)
    ((_, _, data, headers),) = calls_for(mocker, "POST", "/connect/token")
    assert data == "grant_type=refresh_token&refresh_token=old"
    assert headers == {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {BASIC_AUTH}",
        **API_HEADERS,
    }


async def test_refresh_keeps_old_refresh_token(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response(refresh_token=None))

    token = await api.async_refresh_token()

    assert token.refresh_token == "old"
    assert api.refresh_token == "old"


async def test_refresh_token_is_url_encoded(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    api = GkpHomeApi(session, CLIENT_SECRET, APIM_KEY, refresh_token="a+b/c=")

    await api.async_refresh_token()

    ((_, _, data, _),) = calls_for(mocker, "POST", "/connect/token")
    assert data == "grant_type=refresh_token&refresh_token=a%2Bb%2Fc%3D"


async def test_refresh_without_refresh_token(session: aiohttp.ClientSession) -> None:
    api = GkpHomeApi(session, CLIENT_SECRET, APIM_KEY)

    with pytest.raises(InvalidAuth):
        await api.async_refresh_token()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, '{"error":"invalid_grant"}'),
        (401, '{"error":"invalid_client"}'),
        (400, "not json"),
    ],
)
async def test_refresh_rejected(
    api: GkpHomeApi, mocker: AiohttpClientMocker, status: int, body: str
) -> None:
    mocker.post(TOKEN_URL, status=status, text=body)

    with pytest.raises(InvalidAuth):
        await api.async_refresh_token()


async def test_refresh_server_error(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, status=502, text="bad gateway")

    with pytest.raises(ApiError) as excinfo:
        await api.async_refresh_token()
    assert excinfo.value.status == 502


async def test_refresh_connection_error(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, exc=aiohttp.ClientConnectionError("down"))

    with pytest.raises(CannotConnect):
        await api.async_refresh_token()


async def test_refresh_timeout(api: GkpHomeApi, mocker: AiohttpClientMocker) -> None:
    mocker.post(TOKEN_URL, exc=TimeoutError())

    with pytest.raises(CannotConnect, match="Timeout"):
        await api.async_refresh_token()


def test_token_set_expiry() -> None:
    assert TokenSet("a", "r", time.time() + 3600, SUB).is_expired is False
    assert TokenSet("a", "r", time.time() + 30, SUB).is_expired is True


# --------------------------------------------------------------- device API


async def test_api_call_refreshes_before_first_request(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    installations = json.loads(load_fixture("installations.json"))
    mocker.get(INSTALLATIONS_URL, json=installations)

    result = await api.async_get_installations(SUB)

    assert result == installations
    ((_, _, data, headers),) = calls_for(
        mocker, "GET", f"/businessmodule/v1/installations/admins/{SUB}"
    )
    assert data is None
    assert headers == {
        "Authorization": f"Bearer {api.token.access_token}",
        **API_HEADERS,
    }


async def test_api_call_retries_once_after_401(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    sequence(
        mocker,
        "post",
        TOKEN_URL,
        [
            {"json": token_response(jti="first")},
            {"json": token_response(jti="second", refresh_token="refresh-2")},
        ],
    )
    sequence(
        mocker,
        "get",
        INSTALLATIONS_URL,
        [{"status": 401, "text": "Unauthorized"}, {"json": []}],
    )

    assert await api.async_get_installations(SUB) == []

    first, second = calls_for(
        mocker, "GET", f"/businessmodule/v1/installations/admins/{SUB}"
    )
    assert first[3]["Authorization"] != second[3]["Authorization"]
    assert second[3]["Authorization"] == f"Bearer {api.token.access_token}"
    assert api.refresh_token == "refresh-2"
    assert [call[2] for call in calls_for(mocker, "POST", "/connect/token")] == [
        "grant_type=refresh_token&refresh_token=old",
        "grant_type=refresh_token&refresh_token=refresh-token-1",
    ]


async def test_api_call_fails_after_second_401(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    mocker.get(INSTALLATIONS_URL, status=401, text="Unauthorized")

    with pytest.raises(ApiError) as excinfo:
        await api.async_get_installations(SUB)
    assert excinfo.value.status == 401
    assert (
        len(calls_for(mocker, "GET", f"/businessmodule/v1/installations/admins/{SUB}"))
        == 2
    )
    assert len(calls_for(mocker, "POST", "/connect/token")) == 2


async def test_concurrent_401s_refresh_once(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    sequence(
        mocker,
        "post",
        TOKEN_URL,
        [{"json": token_response(jti="first")}, {"json": token_response(jti="second")}],
    )
    content = {"At": 700}
    sequence(
        mocker,
        "get",
        content_url("AABBCCDDEE01"),
        [{"status": 401, "text": ""}, {"json": content}],
    )
    sequence(
        mocker,
        "get",
        content_url("AABBCCDDEE02"),
        [{"status": 401, "text": ""}, {"json": content}],
    )

    await api.async_refresh_token()
    results = await asyncio.gather(
        api.async_get_device_content("AABBCCDDEE01"),
        api.async_get_device_content("AABBCCDDEE02"),
    )

    assert results == [content, content]
    assert len(calls_for(mocker, "POST", "/connect/token")) == 2


async def test_api_call_uses_existing_token_until_expiry(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    sequence(
        mocker,
        "post",
        TOKEN_URL,
        [
            {"json": token_response(exp=int(time.time()) + 30)},
            {"json": token_response(jti="renewed")},
        ],
    )
    mocker.get(INSTALLATIONS_URL, json=[])

    await api.async_get_installations(SUB)  # token obtained, expires within margin
    await api.async_get_installations(SUB)  # refreshed again before the call
    await api.async_get_installations(SUB)  # still valid

    assert len(calls_for(mocker, "POST", "/connect/token")) == 2


async def test_api_error(api: GkpHomeApi, mocker: AiohttpClientMocker) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    mocker.get(INSTALLATIONS_URL, status=500, text="Internal Server Error")

    with pytest.raises(ApiError) as excinfo:
        await api.async_get_installations(SUB)
    assert excinfo.value.status == 500
    assert excinfo.value.body == "Internal Server Error"
    assert "500" in str(excinfo.value)


async def test_api_connection_errors(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    sequence(
        mocker,
        "get",
        INSTALLATIONS_URL,
        [aiohttp.ClientConnectionError("down"), TimeoutError()],
    )

    with pytest.raises(CannotConnect):
        await api.async_get_installations(SUB)
    with pytest.raises(CannotConnect, match="Timeout"):
        await api.async_get_installations(SUB)


async def test_api_unexpected_payloads(
    api: GkpHomeApi, mocker: AiohttpClientMocker
) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    mocker.get(INSTALLATIONS_URL, json={"not": "a list"})
    sequence(
        mocker, "get", content_url(DEVICE_ID), [{"json": [1, 2]}, {"text": "<html>"}]
    )

    with pytest.raises(ApiError, match="Unexpected installations payload"):
        await api.async_get_installations(SUB)
    with pytest.raises(ApiError, match="Unexpected device content payload"):
        await api.async_get_device_content(DEVICE_ID)
    with pytest.raises(ApiError, match="Invalid JSON"):
        await api.async_get_device_content(DEVICE_ID)


async def test_get_device_content(api: GkpHomeApi, mocker: AiohttpClientMocker) -> None:
    content = json.loads(load_fixture("content_kitchen.json"))
    mocker.post(TOKEN_URL, json=token_response())
    mocker.get(content_url(DEVICE_ID), json=content)

    assert await api.async_get_device_content(DEVICE_ID) == content
    ((_, url, data, _),) = calls_for(
        mocker,
        "GET",
        f"/iotmanagement/v1/configuration/{DEVICE_ID}/{DEVICE_ID}/v1/content/",
    )
    assert str(url) == content_url(DEVICE_ID)
    assert data is None


async def test_write_bodies(api: GkpHomeApi, mocker: AiohttpClientMocker) -> None:
    mocker.post(TOKEN_URL, json=token_response())
    mocker.put(CONFIG_REPLACE_URL, text="")
    mocker.put(INSTALLATION_CONTROL_URL, text="")
    mocker.put(USER_CONNECTED_URL, text="")

    assert await api.async_set_mode(DEVICE_ID, MODE_COMFORT) is None
    await api.async_set_mode(DEVICE_ID, MODE_OFF)
    await api.async_set_mode(DEVICE_ID, MODE_BOOST, 120)
    await api.async_set_mode(DEVICE_ID, MODE_BOOST)
    await api.async_set_setpoint(DEVICE_ID, MODE_ECO, 590)
    await api.async_set_setpoint(DEVICE_ID, MODE_COMFORT, 680)
    await api.async_set_installation_mode(INSTALLATION_ID, SUB, 3)
    await api.async_user_connected(SUB, INSTALLATION_ID)

    def envelope(data: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "Id_deviceId": DEVICE_ID,
            "S1": DEVICE_ID,
            "configurationVersion": "v1.0",
            "data": data,
        }

    puts = calls_for(
        mocker, "PUT", "/iotmanagement/v1/devices/twin/properties/config/replace"
    )
    assert [call[2] for call in puts] == [
        envelope([{"wattsType": "Dm", "wattsTypeValue": 3}]),
        envelope([{"wattsType": "Dm", "wattsTypeValue": 0}]),
        envelope(
            [
                {"wattsType": "Dm", "wattsTypeValue": 5},
                {"wattsType": "Bt", "wattsTypeValue": 120},
            ]
        ),
        envelope(
            [
                {"wattsType": "Dm", "wattsTypeValue": 5},
                {"wattsType": "Bt", "wattsTypeValue": 60},
            ]
        ),
        envelope(
            [
                {"wattsType": "Dm", "wattsTypeValue": 1},
                {"wattsType": "ec", "wattsTypeValue": 590},
            ]
        ),
        envelope(
            [
                {"wattsType": "Dm", "wattsTypeValue": 3},
                {"wattsType": "cf", "wattsTypeValue": 680},
            ]
        ),
    ]
    for call in puts:
        assert call[3] == {
            "Authorization": f"Bearer {api.token.access_token}",
            **API_HEADERS,
        }

    ((_, _, control, _),) = calls_for(
        mocker, "PUT", "/iotmanagement/v1/devices/installationcontroljob"
    )
    assert control == {
        "In": INSTALLATION_ID,
        "A1": SUB,
        "data": [{"timestamp": None, "wattsType": "Cg", "wattsTypeValue": [3, 0, 0]}],
    }
    ((_, _, connected, _),) = calls_for(
        mocker, "PUT", "/iotmanagement/v1/devices/userconnected"
    )
    assert connected == {"A1": SUB, "In": INSTALLATION_ID}


async def test_set_setpoint_rejects_modes_without_setpoint(api: GkpHomeApi) -> None:
    with pytest.raises(ValueError, match="no setpoint"):
        await api.async_set_setpoint(DEVICE_ID, MODE_OFF, 680)


async def test_custom_hosts(
    session: aiohttp.ClientSession, mocker: AiohttpClientMocker
) -> None:
    mocker.post("https://id.example/connect/token", json=token_response())
    mocker.get(
        f"https://api.example/businessmodule/v1/installations/admins/{SUB}", json=[]
    )
    api = GkpHomeApi(
        session,
        CLIENT_SECRET,
        APIM_KEY,
        refresh_token="old",
        identity_host="https://id.example/",
        api_host="https://api.example/",
    )

    assert await api.async_get_installations(SUB) == []
    assert [str(call[1]) for call in mocker.mock_calls] == [
        "https://id.example/connect/token",
        f"https://api.example/businessmodule/v1/installations/admins/{SUB}",
    ]
