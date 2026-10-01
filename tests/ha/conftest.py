"""Real HA fixtures; every account/vehicle call is an in-memory mock.

Run independently: uv run --project tests/ha pytest tests/ha
There are no real credentials, capture files, HTTP responses or command calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Import before HA initializes its temporary configuration directory: otherwise
# HA may bind the custom_components namespace to that empty directory.
from custom_components.omoda_jaecoo import api as integration_api
from custom_components.omoda_jaecoo.config_flow import account_unique_id

DOMAIN = "omoda_jaecoo"
EMAIL = "owner@example.invalid"
PASSWORD = "never-persist-this-password"
PIN = "8642"
VIN = "LTEST000000000001"
VIN_2 = "LTEST000000000002"


@pytest.fixture(autouse=True)
def offline_only(enable_custom_integrations):
    """Disallow even localhost TCP; asyncio's AF_UNIX self-pipe is permitted."""
    pytest_socket.socket_allow_hosts([])
    pytest_socket.disable_socket(allow_unix_socket=True)


@pytest.fixture
def api_types():
    return integration_api


@pytest.fixture
def tokens(api_types):
    return api_types.TokenSet(
        "private-access-token", "private-refresh-token", 4102444800.0
    )


@pytest.fixture
def vehicles(api_types):
    return [
        api_types.Vehicle(VIN, "Private car nickname", "J7", 2),
        api_types.Vehicle(VIN_2, "Second private nickname", "J8", 2),
    ]


@pytest.fixture
def mock_api(api_types, tokens, vehicles):
    """Patch constructor aliases, not HA internals or the actual API methods."""
    client = AsyncMock(spec=api_types.JaecooApi)
    client.tokens = tokens
    client.async_login.return_value = tokens

    async def login(*args, **kwargs):
        # Real async_login also updates the property used when persisting tokens.
        client.tokens = client.async_login.return_value
        return client.tokens

    client.async_login.side_effect = login
    client.async_list_vehicles.return_value = vehicles
    client.async_realtime.return_value = {
        "odometer": 100.0,
        "electricRange": 80.0,
        "dumpEnergy": 70,
    }
    callbacks = []

    def construct(*args, **kwargs):
        if callback := kwargs.get("on_tokens"):
            callbacks.append(callback)
        return client

    client.token_callbacks = callbacks
    with (
        patch(
            f"custom_components.{DOMAIN}.config_flow.JaecooApi", side_effect=construct
        ),
        patch(f"custom_components.{DOMAIN}.JaecooApi", side_effect=construct),
    ):
        yield client


@pytest.fixture
def entry(tokens, vehicles):
    """One account, two discovered vehicles, one explicitly selected vehicle."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=account_unique_id(EMAIL),
        data={
            "email": EMAIL,
            "country_code": "44",
            "tokens": tokens.to_dict(),
            "vehicles": [vehicle.to_dict() for vehicle in vehicles],
            "selected_vins": [VIN],
            "control_pin": PIN,
        },
    )
