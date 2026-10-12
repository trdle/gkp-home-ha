"""Shared helpers for the GKP Home tests (dummy credentials only)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from custom_components.gkp_home.api import b64url
from custom_components.gkp_home.const import API_HOST, IDENTITY_HOST, SCOPE

CLIENT_SECRET = "dummy-client-secret"
APIM_KEY = "dummy-apim-key"
EMAIL = "user@example.com"
PASSWORD = "hunter2"
SUB = "USER000001"

TOKEN_URL = f"{IDENTITY_HOST}/connect/token"
INSTALLATIONS_URL = f"{API_HOST}/businessmodule/v1/installations/admins/{SUB}"
CONFIG_REPLACE_URL = (
    f"{API_HOST}/iotmanagement/v1/devices/twin/properties/config/replace"
)
INSTALLATION_CONTROL_URL = f"{API_HOST}/iotmanagement/v1/devices/installationcontroljob"
USER_CONNECTED_URL = f"{API_HOST}/iotmanagement/v1/devices/userconnected"


def content_url(device_id: str) -> str:
    """Return the device content URL."""
    return (
        f"{API_HOST}/iotmanagement/v1/configuration/{device_id}/{device_id}/v1/content/"
    )


# Deterministic "random" values used by the fixed_random fixture.
VERIFIER_BYTES = b"\x01" * 32
NONCE_BYTES = b"\x02" * 16
STATE_BYTES = b"\x03" * 16
VERIFIER = b64url(VERIFIER_BYTES)
CHALLENGE = b64url(hashlib.sha256(VERIFIER.encode("ascii")).digest())
NONCE = b64url(NONCE_BYTES)
STATE = b64url(STATE_BYTES)

# The ReturnUrl the app packs into the login URL, written out literally so the
# test checks the production builder against the reverse-engineered template.
EXPECTED_RETURN_URL = (
    "%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D835295d3-75c5-470c-82e1-9919f74c00c2"
    f"%26client_secret%3D{CLIENT_SECRET}"
    "%26response_type%3Dcode%2520id_token"
    f"%26scope%3D{SCOPE.replace(' ', '%2520')}"
    "%26redirect_uri%3Dgkp://callback"
    f"%26nonce%3D{NONCE}"
    f"%26code_challenge%3D{CHALLENGE}"
    "%26code_challenge_method%3DS256"
    f"%26state%3D{STATE}"
    "%26oemclient%3Dgkp"
)
EXPECTED_LOGIN_URL = f"{IDENTITY_HOST}/Account/Login?ReturnUrl={EXPECTED_RETURN_URL}"

# What the parsed login form posts (document order, credentials filled in).
EXPECTED_LOGIN_BODY = (
    "ReturnUrl=%2Fconnect%2Fauthorize%2Fcallback%3Fclient_id%3D835295d3-75c5-470c-82e1-9919f74c00c2"
    "&Username=user%40example.com&Password=hunter2"
    "&__RequestVerificationToken=CfDJ8dummyAntiForgeryToken&RememberLogin=false&button=login"
)
EXPECTED_CODE_BODY = (
    "grant_type=authorization_code&code=CODE123&redirect_uri=gkp%3A%2F%2Fcallback"
    f"&code_verifier={VERIFIER}"
)


def make_jwt(claims: dict[str, Any]) -> str:
    """Build an unsigned JWT-shaped token with the given claims."""
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.dummysignature"


def token_response(
    *,
    sub: str = SUB,
    refresh_token: str | None = "refresh-token-1",  # noqa: S107 - dummy value
    exp: int | None = None,
    jti: str = "jti-1",
) -> dict[str, Any]:
    """Build a token endpoint response."""
    exp = exp if exp is not None else int(time.time()) + 3600
    data: dict[str, Any] = {
        "access_token": make_jwt({"sub": sub, "exp": exp, "jti": jti}),
        "token_type": "Bearer",
        "expires_in": 3600,
        "id_token": make_jwt({"sub": sub, "nonce": NONCE}),
        "scope": SCOPE,
    }
    if refresh_token is not None:
        data["refresh_token"] = refresh_token
    return data
