"""Opt-in GPS and passive telemetry through real HA, with synthetic offline replies."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import DOMAIN, EMAIL, PIN, VIN, VIN_2
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfSpeed
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import StoredState
from homeassistant.helpers.restore_state import async_get as restore_state
from homeassistant.util import dt as dt_util


async def settle(hass, coordinator):
    """Optional reads are entry-owned background work, not core-refresh work."""
    task = coordinator._optional_refresh_task
    if task is not None:
        await task
    await hass.async_block_till_done()


async def setup(hass, entry, options=None):
    entry.add_to_hass(hass)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    await settle(hass, coordinator)
    return coordinator


def entity_id(hass, platform, key):
    result = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"eu_{VIN}_{key}")
    assert result is not None, f"Missing {platform} entity for {key}"
    return result


def state(hass, platform, key):
    result = hass.states.get(entity_id(hass, platform, key))
    assert result is not None
    return result


async def test_optional_routes_and_gps_storage_off_by_default(hass, entry, mock_api):
    coordinator = await setup(hass, entry)
    for _ in range(3):
        await coordinator.async_refresh()
        await settle(hass, coordinator)
    assert not hass.states.async_all("device_tracker")
    assert all(position is None for position in coordinator.positions.values())
    mock_api.async_location.assert_not_awaited()
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()
    mock_api.async_realtime.assert_awaited()


@pytest.mark.parametrize("source_known", [True, False])
async def test_opt_in_tracker_metadata_cached_properties_and_privacy(
    hass, entry, mock_api, caplog, source_known
):
    observed = dt_util.utcnow() - timedelta(minutes=17)
    payload = {
        "latitude": 51.123456,
        "longitude": -0.456789,
        "timestamp": observed.isoformat(),
        "email": EMAIL,
        "vin": VIN,
        "token": "private-location-token",
        "address": "private street address",
    }
    if source_known:
        payload["gpsTime"] = observed.isoformat()
    mock_api.async_location.return_value = payload
    coordinator = await setup(hass, entry, {"enable_location": True})
    tracker = state(hass, "device_tracker", "location")
    assert tracker.state not in ("unknown", "unavailable")
    assert tracker.attributes["latitude"] == 51.123456
    assert tracker.attributes["longitude"] == -0.456789
    assert tracker.attributes["source_type"] == "gps"
    assert tracker.attributes["source"] == "cloud_snapshot"
    assert tracker.attributes["source_time_known"] is source_known
    assert tracker.attributes["observed_at"] == (
        observed.isoformat() if source_known else None
    )
    assert tracker.attributes["fetched_at"] == (
        coordinator.positions[VIN].fetched_at.isoformat()
    )
    registry = er.async_get(hass)
    tracker_entry = registry.async_get(tracker.entity_id)
    battery_entry = registry.async_get(entity_id(hass, "sensor", "battery"))
    assert tracker_entry.device_id == battery_entry.device_id
    device = dr.async_get(hass).async_get(tracker_entry.device_id)
    assert device.identifiers == {(DOMAIN, f"eu_{VIN}")}
    mock_api.async_location.assert_awaited_once_with(VIN)
    assert (
        registry.async_get_entity_id("device_tracker", DOMAIN, f"eu_{VIN_2}_location")
        is None
    )

    module = import_module(f"custom_components.{DOMAIN}.device_tracker")
    entity = module.OmodaJaecooLocation(coordinator, VIN)
    for _ in range(5):
        assert entity.available
        assert entity.latitude == 51.123456
        assert entity.longitude == -0.456789
        assert entity.extra_state_attributes["source"] == "cloud_snapshot"
    mock_api.async_location.assert_awaited_once()
    mock_api.async_realtime.assert_awaited_once_with(VIN)
    for secret in (EMAIL, PIN, VIN, "private-location-token", "private street address"):
        assert secret not in repr(entity.extra_state_attributes)
    for secret in (
        "51.123456",
        "-0.456789",
        "private-location-token",
        "private street address",
    ):
        assert secret not in caplog.text
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()
    assert "Last reported location" in tracker.attributes["friendly_name"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"lat": 0, "lon": 0},
        {"lat": 91, "lon": 1},
        {"lat": 1, "lon": 181},
        {"lat": True, "lon": 1},
        {"lat": "nan", "lon": 1},
        {"lat": 1, "lon": "inf"},
        {"lat": 1},
    ],
)
async def test_invalid_location_is_unavailable_not_zero(hass, entry, mock_api, payload):
    mock_api.async_location.return_value = payload
    coordinator = await setup(hass, entry, {"enable_location": True})
    tracker = state(hass, "device_tracker", "location")
    assert tracker.state == "unavailable"
    assert "latitude" not in tracker.attributes
    assert "longitude" not in tracker.attributes
    assert coordinator.positions.get(VIN) is None
    assert coordinator.last_update_success
    assert state(hass, "sensor", "battery").state == "70.0"


@pytest.mark.parametrize("error", ["ApiError", "CannotConnect", "RateLimitError"])
async def test_optional_failure_does_not_take_primary_sensors_down(
    hass, entry, mock_api, api_types, error
):
    mock_api.async_location.side_effect = getattr(api_types, error)(
        "private upstream detail"
    )
    coordinator = await setup(hass, entry, {"enable_location": True})
    assert coordinator.last_update_success
    assert state(hass, "device_tracker", "location").state == "unavailable"
    assert state(hass, "sensor", "battery").state == "70.0"
    for _ in range(3):
        await coordinator.async_refresh()
        await settle(hass, coordinator)
    mock_api.async_location.assert_awaited_once_with(VIN)


async def test_tracker_unavailable_when_primary_refresh_fails(
    hass, entry, mock_api, api_types
):
    mock_api.async_location.return_value = {"lat": 0, "lon": 1}
    coordinator = await setup(hass, entry, {"enable_location": True})
    assert state(hass, "device_tracker", "location").attributes["latitude"] == 0
    mock_api.async_realtime.side_effect = api_types.CannotConnect()
    await coordinator.async_refresh()
    await settle(hass, coordinator)
    assert state(hass, "device_tracker", "location").state == "unavailable"


async def test_tracker_never_restores_coordinates(hass, entry, mock_api):
    mock_api.async_location.return_value = {"lat": 51.123456, "lon": -0.456789}
    coordinator = await setup(hass, entry, {"enable_location": True})
    tracker_id = entity_id(hass, "device_tracker", "location")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not coordinator._listeners
    restore_state(hass).last_states[tracker_id] = StoredState(
        State(tracker_id, "home", {"latitude": 51.123456, "longitude": -0.456789}),
        None,
        dt_util.utcnow(),
    )
    mock_api.async_location.return_value = None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass, entry.runtime_data)
    tracker = hass.states.get(tracker_id)
    assert tracker.state == "unavailable"
    assert "latitude" not in tracker.attributes
    assert "longitude" not in tracker.attributes


async def test_unload_cancels_inflight_optional_read(hass, entry, mock_api):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_location(vin):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock_api.async_location.side_effect = pending_location
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"enable_location": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await started.wait()
    coordinator = entry.runtime_data
    task = coordinator._optional_refresh_task
    assert not task.done()
    assert state(hass, "sensor", "battery").state == "70.0"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert cancelled.is_set()
    assert task.cancelled()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator._listeners
    assert state(hass, "device_tracker", "location").state == "unavailable"


async def test_diagnostics_omit_opted_in_gps_and_optional_sensitive_data(
    hass, entry, mock_api
):
    mock_api.async_location.return_value = {
        "lat": 51.123456,
        "lon": -0.456789,
        "gpsTime": "2026-10-02T10:11:12+00:00",
        "token": "private-location-token",
    }
    mock_api.async_charge_schedule.return_value = {
        "mainSwitch": 0,
        "chargeAppointPlans": [],
        "email": EMAIL,
        "address": "private street address",
        "vin": VIN,
    }
    mock_api.async_charge_depth.return_value = {
        "depth": 80,
        "token": "private-depth-token",
    }
    await setup(hass, entry, {"enable_location": True, "enable_charging_details": True})
    diagnostics = import_module(f"custom_components.{DOMAIN}.diagnostics")
    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(result, default=str)
    for secret in (
        EMAIL,
        PIN,
        VIN,
        VIN_2,
        "51.123456",
        "-0.456789",
        "2026-10-02T10:11:12",
        "private-location-token",
        "private-depth-token",
        "private street address",
    ):
        assert secret not in serialized
    assert result


async def test_speed_units_and_extra_entities_disabled_by_default(
    hass, entry, mock_api
):
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 70,
        "vehicleSpeed": 100,
        "totalVoltage": 350,
        "totalCurrent": -4.5,
        "dSeatHeatingState": 3,
        "frontLeftWindowState": 1,
        "sunroofState": 0,
    }
    coordinator = await setup(hass, entry)
    speed = state(hass, "sensor", "speed")
    assert speed.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfSpeed.MILES_PER_HOUR
    assert float(speed.state) == pytest.approx(62.1371, abs=0.1)
    registry = er.async_get(hass)
    for platform, keys in (
        ("sensor", ("hv_voltage", "hv_current", "driver_seat_heat")),
        ("binary_sensor", ("window_front_left", "sunroof_open")),
    ):
        for key in keys:
            registered = registry.async_get(entity_id(hass, platform, key))
            assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert hass.states.get(registered.entity_id) is None

    sensors = import_module(f"custom_components.{DOMAIN}.sensor")
    expected = {
        "speed": (100, "km/h"),
        "hv_voltage": (350, "V"),
        "hv_current": (-4.5, "A"),
        "driver_seat_heat": (3, None),
    }
    for key, (value, unit) in expected.items():
        description = next(d for d in sensors.DESCRIPTIONS if d.key == key)
        entity = sensors.OmodaJaecooSensor(coordinator, VIN, description)
        assert entity.native_value == value
        assert entity.native_unit_of_measurement == unit
    binaries = import_module(f"custom_components.{DOMAIN}.binary_sensor")
    for key, expected_on in (("window_front_left", True), ("sunroof_open", False)):
        description = next(d for d in binaries.DESCRIPTIONS if d.key == key)
        entity = binaries.OmodaJaecooBinarySensor(coordinator, VIN, description)
        assert entity.is_on is expected_on
    assert mock_api.async_realtime.await_count == 1


@pytest.mark.parametrize(
    "plug,charge,expected",
    [
        (0, 0, "unplugged"),
        (1, 0, "plugged_in"),
        (1, 1, "charging"),
        (0, 1, "unknown"),
        (1, 2, "unknown"),
        (None, None, "unknown"),
        (True, 1, "unknown"),
        (1, "charging", "unknown"),
    ],
)
async def test_charging_three_state_enum_no_inferred_completion(
    hass, entry, mock_api, plug, charge, expected
):
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 70,
        "chargeGunState": plug,
        "chargeState": charge,
        "chargingPower": 7.2,
        "remainChargeTime": 120,
    }
    await setup(hass, entry)
    charging = state(hass, "sensor", "charging_status")
    assert charging.state == expected
    assert charging.attributes["options"] == ["unplugged", "plugged_in", "charging"]
    assert charging.attributes["device_class"] == "enum"
    for key in ("plug_code", "charge_code", "fast_connector_code", "schedule_code"):
        value = charging.attributes[key]
        assert value is None or type(value) is int
    if expected != "unknown":
        assert charging.attributes["plug_code"] == plug
        assert charging.attributes["charge_code"] == charge
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"eu_{VIN}_charge_time_remaining"
        )
        is None
    )
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_charging_eta") is None
    )
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_charge_target")
        is None
    )
    for key in ("charging_power", "charge_time_remaining_raw"):
        registered = registry.async_get(entity_id(hass, "sensor", key))
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    mock_api.async_location.assert_not_awaited()
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()


@pytest.mark.parametrize("unit,raw", [("minutes", 120), ("seconds", 7200)])
async def test_verified_charge_duration_eta_and_native_power(
    hass, entry, mock_api, unit, raw
):
    observed = dt_util.utcnow() - timedelta(minutes=2)
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 70,
        "chargeGunState": 1,
        "chargeState": 1,
        "chargingPower": 7.2,
        "remainChargeTime": raw,
        "timestamp": observed.isoformat(),
        "totalVoltage": 350,
        "totalCurrent": 20,
    }
    coordinator = await setup(hass, entry, {"charge_time_unit": unit})
    remaining = state(hass, "sensor", "charge_time_remaining")
    assert float(remaining.state) == 120
    assert remaining.attributes[ATTR_UNIT_OF_MEASUREMENT] == "min"
    eta = state(hass, "sensor", "charging_eta")
    # HA timestamp states deliberately serialize to whole seconds.
    assert dt_util.parse_datetime(eta.state) == (
        observed + timedelta(minutes=120)
    ).replace(microsecond=0)
    assert coordinator.data[VIN].charging.estimated_finish == (
        observed + timedelta(minutes=120)
    )
    sensors = import_module(f"custom_components.{DOMAIN}.sensor")
    description = next(d for d in sensors.DESCRIPTIONS if d.key == "charging_power")
    power = sensors.OmodaJaecooSensor(coordinator, VIN, description)
    assert power.native_value == 7.2
    assert power.native_unit_of_measurement == "kW"
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 70,
        "chargeGunState": 1,
        "chargeState": 1,
        "totalVoltage": 350,
        "totalCurrent": 20,
        "remainChargeTime": raw,
        "timestamp": (observed - timedelta(hours=1)).isoformat(),
    }
    await coordinator.async_refresh()
    await settle(hass, coordinator)
    assert power.native_value is None, "Do not derive charging power from HV current"
    assert state(hass, "sensor", "charging_eta").state == "unknown"
    mock_api.async_location.assert_not_awaited()
    mock_api.async_charge_schedule.assert_not_awaited()


async def test_empty_frame_forgets_charging_and_extras_not_primary_battery(
    hass, entry, mock_api
):
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 70,
        "chargeGunState": 1,
        "chargeState": 1,
        "vehicleSpeed": 10,
    }
    coordinator = await setup(hass, entry)
    assert state(hass, "sensor", "charging_status").state == "charging"
    mock_api.async_realtime.return_value = {}
    await coordinator.async_refresh()
    await settle(hass, coordinator)
    assert state(hass, "sensor", "battery").state == "70.0"
    assert state(hass, "sensor", "charging_status").state == "unknown"
    assert state(hass, "sensor", "speed").state == "unknown"


async def test_hv_placeholder_does_not_erase_independent_charging_and_speed(
    hass, entry, mock_api
):
    mock_api.async_realtime.return_value = {
        "dumpEnergy": 0,
        "electricRange": 0,
        "totalVoltage": 0,
        "totalCurrent": -1000,
        "chargeGunState": 1,
        "chargeState": 0,
        "vehicleSpeed": 10,
    }
    await setup(hass, entry)
    assert state(hass, "sensor", "battery").state == "unknown"
    assert state(hass, "sensor", "charging_status").state == "plugged_in"
    assert float(state(hass, "sensor", "speed").state) == pytest.approx(
        6.21371, abs=0.1
    )


@pytest.mark.parametrize("target_confirmed", [False, True])
async def test_optional_charge_schedule_and_explicit_target_metadata(
    hass, entry, mock_api, target_confirmed
):
    mock_api.async_charge_schedule.return_value = {
        "mainSwitch": 1,
        "chargeAppointPlans": [
            {
                "switchStatus": 1,
                "startTime": 90,
                "timeConsuming": 120,
                "cycleData": [1, 7],
            }
        ],
    }
    mock_api.async_charge_depth.return_value = {"depth": 80}
    await setup(
        hass,
        entry,
        {
            "enable_charging_details": True,
            "charge_depth_is_target": target_confirmed,
        },
    )
    schedule = state(hass, "sensor", "charge_schedule")
    assert schedule.state == "enabled"
    assert schedule.attributes["plan_count"] == 1
    assert schedule.attributes["plans"] == [
        {
            "enabled": True,
            "start_time": "01:30",
            "duration_minutes": 120,
            "repeat_day_codes": [1, 7],
        }
    ]
    for unsupported in ("Monday", "Sunday", "UTC", "timestamp"):
        assert unsupported not in repr(schedule.attributes)
    depth = er.async_get(hass).async_get(
        entity_id(hass, "sensor", "charging_depth_raw")
    )
    assert depth.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    target_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_charge_target"
    )
    if target_confirmed:
        target = hass.states.get(target_id)
        assert float(target.state) == 80
        assert target.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    else:
        assert target_id is None
    mock_api.async_charge_schedule.assert_awaited_once_with(VIN)
    mock_api.async_charge_depth.assert_awaited_once_with(VIN)
    mock_api.async_location.assert_not_awaited()


async def test_optional_rate_limit_stops_remaining_routes(
    hass, entry, mock_api, api_types
):
    mock_api.async_location.side_effect = api_types.RateLimitError()
    coordinator = await setup(
        hass,
        entry,
        {
            "enable_location": True,
            "enable_charging_details": True,
        },
    )
    assert coordinator.last_update_success
    assert state(hass, "sensor", "battery").state == "70.0"
    mock_api.async_location.assert_awaited_once_with(VIN)
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()


async def test_optional_refreshes_not_multiplied_by_primary_burst(
    hass, entry, mock_api
):
    mock_api.async_location.return_value = {"lat": 51.123456, "lon": -0.456789}
    mock_api.async_charge_schedule.return_value = {
        "mainSwitch": 0,
        "chargeAppointPlans": [],
    }
    mock_api.async_charge_depth.return_value = {"depth": 80}
    coordinator = await setup(
        hass,
        entry,
        {
            "enable_location": True,
            "enable_charging_details": True,
        },
    )
    for _ in range(5):
        await coordinator.async_refresh()
        await settle(hass, coordinator)
    mock_api.async_realtime.assert_awaited()
    assert mock_api.async_realtime.await_count == 6
    mock_api.async_location.assert_awaited_once_with(VIN)
    mock_api.async_charge_schedule.assert_awaited_once_with(VIN)
    mock_api.async_charge_depth.assert_awaited_once_with(VIN)


@pytest.mark.parametrize("depth", [101, -1, "nan", None])
async def test_confirmed_target_still_rejects_invalid_percentage(
    hass, entry, mock_api, depth
):
    mock_api.async_charge_schedule.return_value = {
        "mainSwitch": 0,
        "chargeAppointPlans": [],
    }
    mock_api.async_charge_depth.return_value = {"depth": depth}
    await setup(
        hass,
        entry,
        {
            "enable_charging_details": True,
            "charge_depth_is_target": True,
        },
    )
    assert state(hass, "sensor", "charge_target").state == "unknown"
    assert state(hass, "sensor", "charge_schedule").state == "disabled"


async def test_target_option_alone_does_not_enable_optional_reads(
    hass, entry, mock_api
):
    await setup(hass, entry, {"charge_depth_is_target": True})
    registry = er.async_get(hass)
    for key in ("charge_target", "charge_schedule", "charging_depth_raw"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_{key}") is None
    mock_api.async_location.assert_not_awaited()
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()


async def test_optional_success_ttls_are_per_kind(hass, entry, mock_api):
    clock = 1000.0
    mock_api.async_location.return_value = {"lat": 51.123456, "lon": -0.456789}
    mock_api.async_charge_schedule.return_value = {
        "mainSwitch": 0,
        "chargeAppointPlans": [],
    }
    mock_api.async_charge_depth.return_value = {"depth": 80}
    with patch(
        f"custom_components.{DOMAIN}.coordinator.time",
        SimpleNamespace(monotonic=lambda: clock),
    ):
        coordinator = await setup(
            hass,
            entry,
            {
                "enable_location": True,
                "enable_charging_details": True,
            },
        )
        clock = 1299.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        mock_api.async_location.assert_awaited_once()
        clock = 1300.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        assert mock_api.async_location.await_count == 2
        mock_api.async_charge_schedule.assert_awaited_once()
        mock_api.async_charge_depth.assert_awaited_once()
        clock = 1899.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        assert mock_api.async_location.await_count == 3
        mock_api.async_charge_schedule.assert_awaited_once()
        mock_api.async_charge_depth.assert_awaited_once()
        clock = 1900.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        assert mock_api.async_location.await_count == 3
        assert mock_api.async_charge_schedule.await_count == 2
        assert mock_api.async_charge_depth.await_count == 2


@pytest.mark.parametrize("payload", [None, {}, {"lat": 0, "lon": 0}])
async def test_no_location_data_retries_at_normal_ttl(hass, entry, mock_api, payload):
    # Leader's revised policy: absent/invalid reports are not endpoint failures.
    clock = 1000.0
    mock_api.async_location.return_value = payload
    with patch(
        f"custom_components.{DOMAIN}.coordinator.time",
        SimpleNamespace(monotonic=lambda: clock),
    ):
        coordinator = await setup(hass, entry, {"enable_location": True})
        clock = 1299.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        mock_api.async_location.assert_awaited_once_with(VIN)
        clock = 1300.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        assert mock_api.async_location.await_count == 2
        assert state(hass, "device_tracker", "location").state == "unavailable"


@pytest.mark.parametrize(
    "error,retry_at", [("ApiError", 4600.0), ("CannotConnect", 1300.0)]
)
async def test_optional_endpoint_error_and_transient_transport_retry_ttls(
    hass, entry, mock_api, api_types, error, retry_at
):
    clock = 1000.0
    mock_api.async_location.side_effect = getattr(api_types, error)()
    with patch(
        f"custom_components.{DOMAIN}.coordinator.time",
        SimpleNamespace(monotonic=lambda: clock),
    ):
        coordinator = await setup(hass, entry, {"enable_location": True})
        clock = retry_at - 1
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        mock_api.async_location.assert_awaited_once_with(VIN)
        clock = retry_at
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        assert mock_api.async_location.await_count == 2
        assert coordinator.last_update_success
        assert state(hass, "device_tracker", "location").state == "unavailable"


async def test_failed_location_query_discards_previously_valid_coordinates(
    hass, entry, mock_api
):
    clock = 1000.0
    mock_api.async_location.return_value = {"lat": 51.123456, "lon": -0.456789}
    with patch(
        f"custom_components.{DOMAIN}.coordinator.time",
        SimpleNamespace(monotonic=lambda: clock),
    ):
        coordinator = await setup(hass, entry, {"enable_location": True})
        assert (
            state(hass, "device_tracker", "location").attributes["latitude"]
            == 51.123456
        )
        mock_api.async_location.return_value = None
        clock = 1300.0
        await coordinator.async_refresh()
        await settle(hass, coordinator)
        tracker = state(hass, "device_tracker", "location")
        assert tracker.state == "unavailable"
        assert "latitude" not in tracker.attributes
        assert "longitude" not in tracker.attributes
        assert coordinator.positions[VIN] is None
        assert coordinator.last_update_success
