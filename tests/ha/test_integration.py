"""Real HA entity/coordinator lifecycle with wholly mocked vehicle transport."""

import json
import os
from importlib import import_module
from unittest.mock import patch

import pytest
from conftest import DOMAIN, EMAIL, PASSWORD, PIN, VIN, VIN_2
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfLength
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


async def test_harness_uses_pinned_real_ha_and_blocks_tcp(hass):
    import socket
    from importlib.metadata import version

    import pytest_socket

    assert version("homeassistant") == os.environ.get("HA_TEST_VERSION", "2026.9.4")
    assert version("pytest-homeassistant-custom-component") == os.environ.get(
        "HA_TEST_PLUGIN_VERSION", "0.13.367"
    )
    # Inspect the replacement rather than attempting a socket: HA's plugin
    # considers even deliberately caught blocked-socket attempts a test failure.
    assert socket.socket is not pytest_socket._true_socket
    assert hass.config_entries is not None


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def test_setup_selected_vehicle_and_unload(hass, entry, mock_api):
    coordinator = await setup(hass, entry)
    assert isinstance(coordinator, DataUpdateCoordinator)
    assert entry.state is ConfigEntryState.LOADED
    mock_api.async_login.assert_not_awaited()
    mock_api.async_list_vehicles.assert_awaited_once()
    mock_api.async_realtime.assert_awaited_once_with(VIN)
    assert coordinator.update_interval.total_seconds() == 300
    assert hass.states.async_all("sensor")
    battery_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_battery"
    )
    battery = hass.states.get(battery_id)
    assert float(battery.state) == 70
    assert battery.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert not hass.states.async_all("button")
    assert not hass.states.async_all("lock")
    assert not hass.states.async_all("climate")
    # State repr includes random microseconds, which can coincidentally contain a
    # short synthetic PIN. Check published values/attributes, not log timestamps.
    published = json.dumps(
        [
            {"state": state.state, "attributes": dict(state.attributes)}
            for state in hass.states.async_all()
        ],
        default=str,
    )
    assert PASSWORD not in published
    assert PIN not in published
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert all(
        state.state == "unavailable" for state in hass.states.async_all("sensor")
    )
    assert not coordinator._listeners


async def test_battery_state_and_default_display_use_one_decimal(hass, entry, mock_api):
    mock_api.async_realtime.return_value = {"dumpEnergy": "50.12345678901234567890"}
    await setup(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_battery"
    )
    assert hass.states.get(entity_id).state == "50.1"
    sensor_module = import_module(f"custom_components.{DOMAIN}.sensor")
    description = next(d for d in sensor_module.DESCRIPTIONS if d.key == "battery")
    assert description.suggested_display_precision == 1


async def test_distance_defaults_to_miles_and_accepts_ha_override(
    hass, entry, mock_api
):
    await setup(hass, entry)
    states = [
        state
        for state in hass.states.async_all("sensor")
        if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == UnitOfLength.MILES
    ]
    assert states, (
        "Distance entities must default to miles, even with HA's metric defaults"
    )
    registry = er.async_get(hass)
    odometer_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_odometer")
    odometer = hass.states.get(odometer_id)
    assert odometer.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfLength.MILES
    assert float(odometer.state) == pytest.approx(62.1371, abs=0.01)
    registry.async_update_entity_options(
        odometer.entity_id, "sensor", {"unit_of_measurement": UnitOfLength.KILOMETERS}
    )
    await hass.async_block_till_done()
    converted = hass.states.get(odometer.entity_id)
    assert converted.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfLength.KILOMETERS
    assert float(converted.state) == pytest.approx(100.0, abs=0.01)


@pytest.mark.parametrize("exception", ["CannotConnect", "RateLimitError", "ApiError"])
async def test_coordinator_transport_errors_preserve_data_no_reauth(
    hass, entry, mock_api, api_types, exception
):
    coordinator = await setup(hass, entry)
    previous = coordinator.data
    mock_api.async_realtime.side_effect = getattr(api_types, exception)()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert coordinator.data == previous
    assert all(
        state.state == "unavailable" for state in hass.states.async_all("sensor")
    )
    assert not hass.config_entries.flow.async_progress()
    mock_api.async_login.assert_not_awaited()


async def test_coordinator_auth_error_starts_reauth(hass, entry, mock_api, api_types):
    coordinator = await setup(hass, entry)
    mock_api.async_realtime.side_effect = api_types.AuthenticationError()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    "exception,state",
    [
        ("CannotConnect", ConfigEntryState.SETUP_RETRY),
        ("AuthenticationError", ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_initial_poll_errors(hass, entry, mock_api, api_types, exception, state):
    entry.add_to_hass(hass)
    mock_api.async_realtime.side_effect = getattr(api_types, exception)()
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state
    assert not hass.states.async_all("sensor")


@pytest.mark.parametrize(
    "stored_tokens", [None, {}, {"access_token": ""}, "malformed-token-container"]
)
async def test_malformed_stored_tokens_start_reauth_without_network(
    hass, entry, mock_api, stored_tokens
):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "tokens": stored_tokens}
    )
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    mock_api.async_login.assert_not_awaited()
    mock_api.async_list_vehicles.assert_not_awaited()
    mock_api.async_realtime.assert_not_awaited()
    assert not hass.states.async_all("sensor")


async def test_sleeping_vehicle_is_unknown_not_reauth(hass, entry, mock_api):
    mock_api.async_realtime.return_value = {}
    coordinator = await setup(hass, entry)
    assert coordinator.last_update_success
    assert not hass.config_entries.flow.async_progress()
    registry = er.async_get(hass)
    for key in ("battery", "electric_range", "odometer", "observed_at"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_{key}")
        assert hass.states.get(entity_id).state == "unknown"
    freshness_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_freshness")
    assert hass.states.get(freshness_id).state == "no_data"


async def test_sleep_reply_retains_previous_values_without_claiming_freshness(
    hass, entry, mock_api
):
    coordinator = await setup(hass, entry)
    original = coordinator.data[VIN]
    mock_api.async_realtime.return_value = {}
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    snapshot = coordinator.data[VIN]
    assert snapshot.battery == original.battery == 70
    assert snapshot.electric_range == original.electric_range == 80
    assert snapshot.odometer == original.odometer == 100
    assert snapshot.observed_at == original.observed_at
    assert snapshot.freshness() == "no_data"
    assert coordinator.last_update_success
    assert not hass.config_entries.flow.async_progress()


@pytest.mark.parametrize("age_minutes,expected", [(1, "current"), (30, "stale")])
async def test_observation_timestamp_drives_freshness(
    hass, entry, mock_api, age_minutes, expected
):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    observed_at = dt_util.utcnow() - timedelta(minutes=age_minutes)
    mock_api.async_realtime.return_value = {
        "dumpEnergy": "70",
        "timestamp": observed_at.isoformat(),
    }
    coordinator = await setup(hass, entry)
    assert coordinator.data[VIN].observed_at == observed_at
    assert coordinator.data[VIN].freshness() == expected
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_freshness")
    assert hass.states.get(entity_id).state == expected


async def test_rate_limit_backoff_resets_after_success(
    hass, entry, mock_api, api_types
):
    coordinator = await setup(hass, entry)
    mock_api.async_realtime.side_effect = api_types.RateLimitError()
    await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() == 600
    for _ in range(5):
        await coordinator.async_refresh()
    assert coordinator.update_interval.total_seconds() == 3600
    mock_api.async_realtime.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.update_interval.total_seconds() == 300


async def test_token_callback_persists_without_reloading(
    hass, entry, mock_api, api_types
):
    coordinator = await setup(hass, entry)
    old_data = dict(entry.data)
    tokens = api_types.TokenSet(
        "rotated-access-secret", "rotated-refresh-secret", 4200000000.0
    )
    assert mock_api.token_callbacks
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        mock_api.token_callbacks[-1](tokens)
        await hass.async_block_till_done()
        reload.assert_not_called()
    assert entry.data["tokens"] == tokens.to_dict()
    assert entry.runtime_data is coordinator
    for key in ("email", "country_code", "vehicles", "selected_vins", "control_pin"):
        assert entry.data[key] == old_data[key]


async def test_selected_cars_are_polled_sequentially(hass, entry, mock_api):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "selected_vins": [VIN, VIN_2]}
    )
    active = 0
    seen = []

    async def telemetry(vin):
        nonlocal active
        import asyncio

        active += 1
        assert active == 1, "Vehicle polls must not overlap"
        seen.append(vin)
        await asyncio.sleep(0)
        active -= 1
        return {"soc": 70}

    mock_api.async_realtime.side_effect = telemetry
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert seen == [VIN, VIN_2]


async def test_poll_interval_option_and_reload_listener(hass, entry, mock_api):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"poll_interval": 12})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval.total_seconds() == 720
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        hass.config_entries.async_update_entry(entry, options={"poll_interval": 20})
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)


async def test_diagnostics_exclude_all_secrets_and_personal_data(
    hass, entry, mock_api, tokens
):
    """Diagnostics should redact/allowlist recursively, including unknown telemetry."""
    mock_api.async_realtime.return_value = {
        "soc": 70,
        "latitude": 51.123456,
        "longitude": -0.456789,
        "location": "private street address",
        "userToken": "private-tsp-token",
        "nested": {"email": EMAIL, "access_token": tokens.access_token},
    }
    await setup(hass, entry)
    diagnostics = import_module(f"custom_components.{DOMAIN}.diagnostics")
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result, default=str)
    for secret in (
        EMAIL,
        PASSWORD,
        PIN,
        VIN,
        VIN_2,
        tokens.access_token,
        tokens.refresh_token,
        "Private car nickname",
        "Second private nickname",
        "private-tsp-token",
        "51.123456",
        "-0.456789",
        "private street address",
    ):
        assert secret not in serialized
    assert result, "Diagnostics should still contain non-sensitive health information"
    assert result["read_only"] is True
    assert result["control_pin_stored"] is True
    assert result["control_pin_verified"] is False
