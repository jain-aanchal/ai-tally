# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures for the gateway test suite.

Many test files flip ``require_api_key`` (and, for Initiative 1, ``gateway_service_token``) on the
process-wide settings singleton AFTER ``TestClient(app)`` has already run the lifespan, so the
mutation outlives the test and leaks into the next file's startup. That was harmless until the
control-plane fail-closed boot guard landed (Initiative 1 §6): a leftover ``require_api_key=True``
with an empty service token now makes the NEXT ``TestClient`` refuse to start, exactly as a real
misconfigured deployment would. This autouse fixture resets those auth knobs to their safe defaults
before every test so each file boots from a clean, valid config; a test that wants auth on still
sets it for itself after startup.
"""

from __future__ import annotations

import pytest

from gateway.config import get_settings


@pytest.fixture(autouse=True)
def _reset_auth_settings() -> None:
    settings = get_settings()
    settings.require_api_key = False
    settings.gateway_service_token = ""
