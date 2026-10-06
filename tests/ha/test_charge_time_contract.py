"""The EU app's API duration is minutes, not a configurable user interpretation."""

from datetime import timedelta

import pytest
from conftest import DOMAIN, VIN
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util


@pytest.mark.parametrize("legacy", [None, "unverified", "minutes", "seconds"])
async def test_165_minutes_works_by_default_and_migrates_legacy_option(
    hass, entry, mock_api, legacy
):
    sample_time = dt_util.utcnow() - timedelta(minutes=2)
    mock_api.async_realtime.return_value = {
        "chargeGunState": "1",
        "chargeState": "1",
        "remainChargeTime": "165",
        "timestamp": sample_time.isoformat(),
        "dumpEnergy": "71",
    }
    entry.add_to_hass(hass)
    options = {"poll_interval": 15, "climate_duration": 10, "enable_controls": False}
    original_data = dict(entry.data)
    hass.config_entries.async_update_entry(
        entry, options={**options, **({"charge_time_unit": legacy} if legacy else {})}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert dict(entry.options) == options
    assert dict(entry.data) == original_data
    registry = er.async_get(hass)
    remaining_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"eu_{VIN}_charge_time_remaining"
    )
    eta_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_charging_eta")
    remaining = hass.states.get(remaining_id)
    assert float(remaining.state) == 165
    assert remaining.attributes["unit_of_measurement"] == "min"
    expected = (sample_time + timedelta(hours=2, minutes=45)).replace(microsecond=0)
    assert dt_util.parse_datetime(hass.states.get(eta_id).state) == expected
    assert entry.runtime_data.update_interval == timedelta(minutes=15)
    mock_api.async_login.assert_not_awaited()
    mock_api.async_request_otp.assert_not_awaited()


async def test_options_do_not_expose_api_unit_calibration(hass, entry):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(key) for key in result["data_schema"].schema}
    assert "charge_time_unit" not in keys
    assert "poll_interval" in keys


async def test_no_remaining_value_is_not_zero(hass, entry, mock_api):
    mock_api.async_realtime.return_value = {"chargeGunState": 1, "chargeState": 0}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for suffix in ("charge_time_remaining", "charging_eta"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"eu_{VIN}_{suffix}")
        assert entity_id is not None
        assert hass.states.get(entity_id).state == "unknown"
