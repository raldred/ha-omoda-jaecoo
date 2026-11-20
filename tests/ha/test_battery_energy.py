"""User-configured usable capacity estimates, using only offline HA fixtures."""

import json
from dataclasses import replace
from datetime import timedelta
from importlib import import_module

import pytest
from conftest import DOMAIN, EMAIL, PIN, VIN, VIN_2
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.omoda_jaecoo.const import CONF_USABLE_BATTERY_CAPACITIES

KEY = "estimated_battery_energy"


def entity_id(hass, vin=VIN):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"eu_{vin}_{KEY}")


async def setup(hass, entry, mock_api, capacities, soc=72, vins=None):
    mock_api.async_realtime.return_value = {
        "dumpEnergy": soc,
        "timestamp": (dt_util.utcnow() - timedelta(seconds=1)).isoformat(),
    }
    entry.add_to_hass(hass)
    if vins is not None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "selected_vins": vins}
        )
    hass.config_entries.async_update_entry(
        entry, options={CONF_USABLE_BATTERY_CAPACITIES: capacities}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def make_entity(coordinator, vin=VIN):
    module = import_module(f"custom_components.{DOMAIN}.sensor")
    description = next(item for item in module.DESCRIPTIONS if item.key == KEY)
    return module.OmodaJaecooSensor(coordinator, vin, description)


@pytest.mark.parametrize("soc, expected", [(72, 43.2), (0, 0), (100, 60)])
async def test_estimate_metadata_and_privacy(hass, entry, mock_api, soc, expected):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60}, soc)
    state = hass.states.get(entity_id(hass))
    assert float(state.state) == pytest.approx(expected)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["device_class"] == "energy_storage"
    assert state.attributes["state_class"] == "measurement"
    assert "estimate" in state.attributes["friendly_name"].lower()
    entity = make_entity(coordinator)
    assert entity.suggested_display_precision == 1
    assert entity.available
    attrs = entity.extra_state_attributes
    assert attrs["estimate_basis"] == "user_configured_usable_capacity"
    assert attrs["usable_battery_capacity_kwh"] == 60
    assert attrs["observed_at"] == coordinator.data[VIN].observed_at.isoformat()
    assert attrs["freshness"] == "current"
    assert attrs["estimated"] is True
    for secret in (VIN, EMAIL, PIN, "dumpEnergy", "private-access-token"):
        assert secret not in json.dumps(attrs)
    # Entity properties never fetch data, including repeated reads.
    for _ in range(3):
        assert entity.native_value == pytest.approx(expected)
        assert entity.extra_state_attributes == attrs
    mock_api.async_realtime.assert_awaited_once_with(VIN)
    battery_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_battery"
    )
    assert float(hass.states.get(battery_id).state) == soc


async def test_no_early_rounding(hass, entry, mock_api):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60.12345})
    coordinator.async_set_updated_data(
        {VIN: replace(coordinator.data[VIN], battery=72.34567)}
    )
    assert make_entity(coordinator).native_value == pytest.approx(
        60.12345 * 72.34567 / 100, abs=1e-12
    )


@pytest.mark.parametrize(
    "capacity",
    [None, True, False, "60", 0, -1, 0.09, 501, float("nan"), float("inf"), {}, []],
)
async def test_invalid_capacity_never_registers(hass, entry, mock_api, capacity):
    await setup(hass, entry, mock_api, {VIN: capacity})
    assert entity_id(hass) is None


@pytest.mark.parametrize("capacities", [{}, {VIN_2: 60}, None, [], "60"])
async def test_missing_or_malformed_configuration(hass, entry, mock_api, capacities):
    await setup(hass, entry, mock_api, capacities)
    assert entity_id(hass) is None


@pytest.mark.parametrize(
    "soc", [None, True, False, "72", -1, 101, float("nan"), float("inf")]
)
async def test_invalid_snapshot_soc_is_unknown(hass, entry, mock_api, soc):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60})
    coordinator.async_set_updated_data(
        {VIN: replace(coordinator.data[VIN], battery=soc)}
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass)).state == "unknown"


@pytest.mark.parametrize("soc", [10**1000, -(10**1000)])
async def test_huge_integer_soc_is_rejected_without_float_overflow(
    hass, entry, mock_api, soc
):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60})
    # Isolate the estimate property; the existing battery sensor is unchanged.
    coordinator.data[VIN] = replace(coordinator.data[VIN], battery=soc)
    assert make_entity(coordinator).native_value is None


@pytest.mark.parametrize(
    "change, freshness",
    [
        ({"observed_at": None}, "unknown"),
        ({"has_payload": False}, "no_data"),
        ({"degraded": True}, "unreliable"),
        ({"age_minutes": 120}, "stale"),
        ({"age_minutes": -1}, "unknown"),
    ],
)
async def test_only_fresh_current_samples_are_estimated(
    hass, entry, mock_api, change, freshness
):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60})
    if "age_minutes" in change:
        change = {
            "observed_at": dt_util.utcnow() - timedelta(minutes=change["age_minutes"])
        }
    coordinator.async_set_updated_data({VIN: replace(coordinator.data[VIN], **change)})
    await hass.async_block_till_done()
    state = hass.states.get(entity_id(hass))
    assert state.state == "unknown"
    assert state.attributes["freshness"] == freshness
    # Last-reported battery semantics are intentionally unchanged.
    battery_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_battery"
    )
    assert float(hass.states.get(battery_id).state) == 72


async def test_missing_snapshot_and_failed_coordinator(hass, entry, mock_api):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60})
    entity = make_entity(coordinator)
    coordinator.async_set_updated_data({})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass)).state == "unknown"
    assert entity.extra_state_attributes["freshness"] == "no_data"
    coordinator.async_set_update_error(RuntimeError("synthetic offline failure"))
    await hass.async_block_till_done()
    assert not entity.available
    assert entity.native_value is None
    assert hass.states.get(entity_id(hass)).state == "unavailable"


async def test_failed_coordinator_does_not_estimate_retained_sample(
    hass, entry, mock_api
):
    coordinator = await setup(hass, entry, mock_api, {VIN: 60})
    coordinator.async_set_update_error(RuntimeError("synthetic offline failure"))
    await hass.async_block_till_done()
    assert make_entity(coordinator).native_value is None
    assert hass.states.get(entity_id(hass)).state == "unavailable"


async def test_adding_and_removing_capacity_preserves_battery_and_api_reads(
    hass, entry, mock_api
):
    await setup(hass, entry, mock_api, {})
    registry = er.async_get(hass)
    battery_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_battery")
    battery_registry_id = registry.async_get(battery_id).id
    baseline_reads = mock_api.async_realtime.await_count
    assert entity_id(hass) is None
    hass.config_entries.async_update_entry(
        entry, options={CONF_USABLE_BATTERY_CAPACITIES: {VIN: 60}}
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id(hass)).state) == pytest.approx(43.2)
    # Reload performs its normal single vehicle refresh, not a per-sensor request.
    assert mock_api.async_realtime.await_count == baseline_reads + 1
    assert registry.async_get(battery_id).id == battery_registry_id
    assert float(hass.states.get(battery_id).state) == 72
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()
    assert mock_api.async_realtime.await_count == baseline_reads + 2
    assert registry.async_get(battery_id).id == battery_registry_id
    assert float(hass.states.get(battery_id).state) == 72
    mock_api.async_location.assert_not_awaited()
    mock_api.async_charge_schedule.assert_not_awaited()
    mock_api.async_charge_depth.assert_not_awaited()


async def test_per_vehicle_capacities_and_options_reload(hass, entry, mock_api):
    await setup(hass, entry, mock_api, {VIN: 60, VIN_2: 30}, vins=[VIN, VIN_2])
    assert float(hass.states.get(entity_id(hass, VIN)).state) == pytest.approx(43.2)
    second_id = entity_id(hass, VIN_2)
    assert float(hass.states.get(second_id).state) == pytest.approx(21.6)
    hass.config_entries.async_update_entry(
        entry, options={CONF_USABLE_BATTERY_CAPACITIES: {VIN: 50}}
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id(hass, VIN)).state) == pytest.approx(36)
    # HA retains a restored/unavailable placeholder, not a live numeric estimate.
    assert hass.states.get(second_id).state == "unavailable"
    assert hass.states.get(second_id).attributes["restored"] is True
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, VIN)).state == "unavailable"
    assert hass.states.get(entity_id(hass, VIN)).attributes["restored"] is True
