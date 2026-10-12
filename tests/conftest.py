"""Fixtures for the GKP Home tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest

from .helpers import NONCE_BYTES, STATE_BYTES, VERIFIER_BYTES


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture
def fixed_random() -> Generator[None]:
    """Make the PKCE verifier, nonce and state deterministic."""
    with patch(
        "custom_components.gkp_home.api.secrets.token_bytes",
        side_effect=[VERIFIER_BYTES, NONCE_BYTES, STATE_BYTES],
    ):
        yield
