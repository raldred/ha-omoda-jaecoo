"""Offline usable-capacity validation and native editable options UI."""

from unittest.mock import patch

import pytest
import voluptuous as vol
from conftest import VIN, VIN_2
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import selector

from custom_components.omoda_jaecoo.battery_capacity import usable_battery_capacity
from custom_components.omoda_jaecoo.config_flow import OmodaJaecooOptionsFlow
from custom_components.omoda_jaecoo.const import (
    CONF_USABLE_BATTERY_CAPACITIES as CAPACITIES,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "18.3",
        True,
        False,
        0,
        -1,
        0.09,
        501,
        float("nan"),
        float("inf"),
        -float("inf"),
        {},
        [],
        10**1000,
    ],
)
def test_invalid_capacity(value):
    assert usable_battery_capacity({CAPACITIES: {VIN: value}}, VIN) is None


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        {},
        {CAPACITIES: None},
        {CAPACITIES: []},
        {CAPACITIES: "18"},
        {CAPACITIES: {VIN_2: 18}},
    ],
)
def test_missing_or_malformed_map(options):
    assert usable_battery_capacity(options, VIN) is None


@pytest.mark.parametrize("value", [0.1, 18, 18.3, 500])
def test_valid_capacity(value):
    result = usable_battery_capacity({CAPACITIES: {VIN: value}}, VIN)
    assert result == value
    assert isinstance(result, float)


async def init(hass, entry, options=None, multiple=False):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "selected_vins": [VIN, VIN_2] if multiple else [VIN]},
        options=options or {},
    )
    return await hass.config_entries.options.async_init(entry.entry_id)


def capacity_field(result):
    return next(
        (key, value)
        for key, value in result["data_schema"].schema.items()
        if str(key) == CAPACITIES
    )


async def submit(hass, result, capacities):
    data = {"poll_interval": 15}
    if capacities is not None:
        data[CAPACITIES] = capacities
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], data
        )
        await hass.async_block_till_done()
    return result


@pytest.mark.parametrize("name", ["Same car", ""])
async def test_duplicate_or_missing_names_have_distinct_labels(hass, entry, name):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "vehicles": [
                {**vehicle, "name": name} for vehicle in entry.data["vehicles"]
            ],
            "selected_vins": [VIN, VIN_2],
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    _, field = capacity_field(result)
    labels = [value["label"] for value in field.config["fields"].values()]
    base = name or "Vehicle"
    assert labels == [f"{base} (1)", f"{base} (2)"]
    assert all(VIN not in label and VIN_2 not in label for label in labels)


async def test_native_optional_vehicle_fields_no_capacity_default(
    hass, entry, mock_api
):
    result = await init(hass, entry, multiple=True)
    key, field = capacity_field(result)
    assert isinstance(key, vol.Optional)
    assert key.default is vol.UNDEFINED
    assert key.description["suggested_value"] == {}
    assert isinstance(field, selector.ObjectSelector)
    fields = field.config["fields"]
    assert set(fields) == {VIN, VIN_2}
    for vin, name in (
        (VIN, "Private car nickname"),
        (VIN_2, "Second private nickname"),
    ):
        assert fields[vin]["label"] == name
        assert VIN not in fields[vin]["label"]
        assert fields[vin]["required"] is False
        number = fields[vin]["selector"]
        assert isinstance(number, selector.NumberSelector)
        assert number.config == {
            "min": 0.1,
            "max": 500,
            "step": "any",
            "mode": "box",
            "unit_of_measurement": "kWh",
        }
    assert result["data_schema"]({"poll_interval": 15}).get(CAPACITIES) is None
    mock_api.async_realtime.assert_not_awaited()


@pytest.mark.parametrize(
    "capacities", [None, {}, {VIN: 21.5}, {VIN_2: 40}, {VIN: 18, VIN_2: 40.2}]
)
async def test_edit_clear_omit_and_multiple_vehicles(hass, entry, mock_api, capacities):
    result = await init(hass, entry, {CAPACITIES: {VIN: 18.3}}, multiple=True)
    key, _ = capacity_field(result)
    assert key.description["suggested_value"] == {VIN: 18.3}
    result = await submit(hass, result, capacities)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CAPACITIES] == (capacities or {})
    assert all(isinstance(value, float) for value in entry.options[CAPACITIES].values())
    assert entry.options["poll_interval"] == 15
    assert entry.options["enable_controls"] is False
    assert entry.options["climate_duration"] == 15
    mock_api.async_realtime.assert_not_awaited()
    mock_api.async_login.assert_not_awaited()


async def test_capacity_edit_keeps_other_native_option_values(hass, entry):
    options = {
        "poll_interval": 20,
        "enable_controls": True,
        "climate_duration": 12,
        "enable_location": True,
        "enable_charging_details": True,
        "charge_depth_is_target": True,
    }
    result = await init(hass, entry, {**options, CAPACITIES: {VIN: 18}})
    # The other controls retain their existing native defaults on submission.
    data = result["data_schema"]({"poll_interval": 20, CAPACITIES: {VIN: 19.2}})
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], data
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {**options, CAPACITIES: {VIN: 19.2}}


@pytest.mark.parametrize(
    "capacities",
    [
        {VIN: "18.3"},
        {VIN: True},
        {VIN: False},
        {VIN: 0},
        {VIN: -1},
        {VIN: float("nan")},
        {VIN: float("inf")},
        {VIN: 501},
        {VIN_2: 20},
        {"UNKNOWN": 20},
        [],
        "",
        None,
    ],
)
async def test_flow_defends_against_malformed_values(hass, entry, capacities):
    # Exercise the flow's own validation as well as the frontend selector.
    await init(hass, entry)
    flow = OmodaJaecooOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init({"poll_interval": 15, CAPACITIES: capacities})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CAPACITIES: "invalid_capacity"}
    assert not entry.options


@pytest.mark.parametrize(
    "capacities", [{VIN_2: 20}, {"UNKNOWN": 20}, {VIN: -1}, {VIN: 0}, {VIN: 501}]
)
async def test_native_schema_rejects_invalid_input(hass, entry, capacities):
    result = await init(hass, entry)
    with pytest.raises(InvalidData):
        await submit(hass, result, capacities)
