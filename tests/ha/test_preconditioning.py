"""Rapid heating and independent rear-window defrost inside real Home Assistant.

Nothing here sends a vehicle command: the command client and coordinator calls
are in-memory mocks, sockets are blocked, and no schedule or blueprint runs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import DOMAIN, PIN, VIN
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.omoda_jaecoo.capability import (
    REAR_WINDOW_HEATING,
    rear_window_heating_reported,
)
from custom_components.omoda_jaecoo.climate import OmodaJaecooClimate
from custom_components.omoda_jaecoo.commands import CommandClient
from custom_components.omoda_jaecoo.coordinator import COMMAND_KINDS
from custom_components.omoda_jaecoo.switch import OmodaJaecooRearDefrost

CONTROL_PLATFORMS = [Platform.CLIMATE, Platform.SWITCH]


def snapshot(rear_defrost=False, **changes):
    extras = {} if rear_defrost is None else {REAR_WINDOW_HEATING: rear_defrost}
    return SimpleNamespace(
        **{
            "door_locked": None,
            "climate_on": None,
            "cabin_temperature": None,
            "target_temperature": None,
            "doors": {},
            "extras": extras,
            **changes,
        }
    )


def vehicle(**changes):
    return SimpleNamespace(
        **{
            "vin": VIN,
            "name": "Synthetic vehicle",
            "model": "J7",
            "min_temperature": 16.0,
            "max_temperature": 30.0,
            "temperature_step": 0.5,
            "allowed_air_durations": (5, 10, 15),
            **changes,
        }
    )


class FakeCoordinator(DataUpdateCoordinator):
    """Real HA subscriptions, but no PIN, transport or vehicle client at all."""

    def __init__(self, hass, entry):
        super().__init__(
            hass, logging.getLogger(__name__), name=DOMAIN, config_entry=entry
        )
        self.selected_vins = [VIN]
        self.vehicles = {VIN: vehicle()}
        self.data = {VIN: snapshot()}
        self.options = {}
        self.controls_enabled = True
        self.controls_available = True
        self.last_command_status = {}
        self.async_climate = AsyncMock(side_effect=self._accepted)
        self.async_rapid_heat = AsyncMock(side_effect=self._accepted)
        self.async_rear_defrost = AsyncMock(side_effect=self._accepted)

    def configured_duration(self):
        return 15

    async def _accepted(self, vin, *args, **kwargs):
        self.last_command_status[vin] = "accepted_unconfirmed"

    async def _async_update_data(self):
        return self.data


@pytest.fixture
def coordinator(hass, entry):
    return FakeCoordinator(hass, entry)


async def setup(hass, entry, coordinator, platforms=CONTROL_PLATFORMS):
    entry.add_to_hass(hass)
    with (
        patch(
            f"custom_components.{DOMAIN}.OmodaJaecooCoordinator",
            return_value=coordinator,
        ),
        patch(f"custom_components.{DOMAIN}.PLATFORMS", platforms),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def entity_id(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"eu_{VIN}_{suffix}")


def state(hass, domain, suffix):
    return hass.states.get(entity_id(hass, domain, suffix))


async def test_rear_defrost_switch_reflects_reported_state_only(
    hass, entry, coordinator
):
    coordinator.data = {VIN: snapshot(rear_defrost=True)}
    await setup(hass, entry, coordinator)
    switch = state(hass, "switch", REAR_WINDOW_HEATING)
    assert switch.state == "on"
    assert switch.attributes["assumed_state"] is True
    assert switch.attributes["device_class"] == "switch"
    coordinator.async_set_updated_data({VIN: snapshot(rear_defrost=False)})
    await hass.async_block_till_done()
    assert state(hass, "switch", REAR_WINDOW_HEATING).state == "off"
    coordinator.async_rear_defrost.assert_not_awaited()


@pytest.mark.parametrize("reading", [None, "1", 1, 0, "unknown"])
async def test_unparsed_rear_window_reading_offers_no_switch(
    hass, entry, coordinator, reading
):
    """Absent or unparsed telemetry is never evidence that the hardware exists."""
    coordinator.data = {VIN: snapshot(rear_defrost=reading)}
    assert not rear_window_heating_reported(coordinator.data[VIN])
    await setup(hass, entry, coordinator)
    assert not hass.states.async_all("switch")
    assert len(hass.states.async_all("climate")) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_air_durations": ()},
        {"allowed_air_durations": None},
        {"allowed_air_durations": (0,)},
        {"allowed_air_durations": ("15",)},
        {"allowed_air_durations": (True,)},
    ],
)
async def test_no_reported_duration_set_offers_no_conditioning_controls(
    hass, entry, coordinator, changes
):
    coordinator.vehicles[VIN] = vehicle(**changes)
    await setup(hass, entry, coordinator)
    assert not hass.states.async_all("switch")
    assert not hass.states.async_all("climate")


async def test_controls_not_offered_until_explicitly_enabled(hass, entry, coordinator):
    coordinator.controls_enabled = False
    coordinator.controls_available = False
    await setup(hass, entry, coordinator)
    assert not hass.states.async_all("switch")
    assert not hass.states.async_all("climate")
    # The action stays registered, but has no entity to act on.
    assert hass.services.has_service(DOMAIN, "rapid_heat")
    coordinator.async_rapid_heat.assert_not_awaited()
    coordinator.async_rear_defrost.assert_not_awaited()


@pytest.mark.parametrize("service,on", [("turn_on", True), ("turn_off", False)])
async def test_rear_defrost_requests_are_accepted_not_confirmed(
    hass, entry, coordinator, service, on
):
    coordinator.data = {VIN: snapshot(rear_defrost=not on)}
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        "switch",
        service,
        {"entity_id": entity_id(hass, "switch", REAR_WINDOW_HEATING)},
        blocking=True,
    )
    coordinator.async_rear_defrost.assert_awaited_once_with(VIN, on)
    switch = state(hass, "switch", REAR_WINDOW_HEATING)
    # The reported state is unchanged: acceptance is not actuation.
    assert switch.state == ("off" if on else "on")
    assert switch.attributes["last_command_status"] == "accepted_unconfirmed"


async def test_rapid_heat_action_defaults_to_local_preference_and_option_duration(
    hass, entry, coordinator
):
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        DOMAIN,
        "rapid_heat",
        {"entity_id": entity_id(hass, "climate", "climate")},
        blocking=True,
    )
    # No duration is passed, so the coordinator applies the configured option.
    coordinator.async_rapid_heat.assert_awaited_once_with(VIN, True, 21, None)
    assert state(hass, "climate", "climate").state == "unknown"
    coordinator.async_climate.assert_not_awaited()


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"enable": False}, (VIN, False, 21, None)),
        ({"temperature": 24.5}, (VIN, True, 24.5, None)),
        ({"duration": 10}, (VIN, True, 21, 10)),
        ({"duration": 10.0}, (VIN, True, 21, 10)),
        ({"temperature": 16, "duration": 5}, (VIN, True, 16.0, 5)),
        ({"enable": "no", "temperature": "30", "duration": "15"}, (VIN, False, 30.0, 15)),
    ],
)
async def test_rapid_heat_action_passes_validated_values(
    hass, entry, coordinator, data, expected
):
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        DOMAIN,
        "rapid_heat",
        {"entity_id": entity_id(hass, "climate", "climate"), **data},
        blocking=True,
    )
    coordinator.async_rapid_heat.assert_awaited_once_with(*expected)


@pytest.mark.parametrize(
    "data",
    [
        {"temperature": 15},
        {"temperature": 31},
        {"temperature": 21.25},
        {"temperature": "nan"},
        {"duration": 0},
        {"duration": 20},
        {"duration": 10.5},
        {"duration": 60},
    ],
)
async def test_rapid_heat_rejects_values_the_vehicle_never_reported(
    hass, entry, coordinator, data
):
    await setup(hass, entry, coordinator)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "rapid_heat",
            {"entity_id": entity_id(hass, "climate", "climate"), **data},
            blocking=True,
        )
    coordinator.async_rapid_heat.assert_not_awaited()
    assert state(hass, "climate", "climate").attributes["selected_temperature"] == 21


@pytest.mark.parametrize(
    "requested,sent",
    [
        # 70 °F is 21.1 °C, and the vehicle's grid is half a degree Celsius.
        (70, 21.0),
        (70.5, 21.5),
        (61, 16.0),
        (86, 30.0),  # Exactly the reported maximum once converted.
    ],
)
async def test_rapid_heat_converts_display_units_to_the_vehicles_celsius_grid(
    hass, entry, coordinator, requested, sent
):
    """The action reads the HA unit, like climate.set_temperature, never raw Celsius."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        DOMAIN,
        "rapid_heat",
        {"entity_id": entity_id(hass, "climate", "climate"), "temperature": requested},
        blocking=True,
    )
    coordinator.async_rapid_heat.assert_awaited_once_with(VIN, True, sent, None)


@pytest.mark.parametrize("requested", [21, 59, 60.5, 86.2, 100, 200])
async def test_rapid_heat_refuses_fahrenheit_outside_the_reported_range(
    hass, entry, coordinator, requested
):
    """A Celsius number typed into a Fahrenheit install is refused, not misread."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await setup(hass, entry, coordinator)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "rapid_heat",
            {
                "entity_id": entity_id(hass, "climate", "climate"),
                "temperature": requested,
            },
            blocking=True,
        )
    coordinator.async_rapid_heat.assert_not_awaited()


async def test_rapid_heat_and_set_temperature_agree_in_fahrenheit(
    hass, entry, coordinator
):
    """Both actions on the same entity must read one unit and send one value."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator.data = {VIN: snapshot(climate_on=True)}
    await setup(hass, entry, coordinator)
    climate = entity_id(hass, "climate", "climate")
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate, "temperature": 70}, True
    )
    coordinator.async_climate.assert_awaited_once_with(VIN, True, 21.0)
    assert state(hass, "climate", "climate").attributes["selected_temperature"] == 21.0
    # The stored preference is Celsius, and the default must not be re-converted.
    await hass.services.async_call(
        DOMAIN, "rapid_heat", {"entity_id": climate}, blocking=True
    )
    coordinator.async_rapid_heat.assert_awaited_once_with(VIN, True, 21.0, None)


@pytest.mark.parametrize("value", ["maybe", 2, None])
async def test_rapid_heat_rejects_an_ambiguous_switch_request(coordinator, value):
    climate = OmodaJaecooClimate(coordinator, VIN)
    with pytest.raises(ServiceValidationError):
        await climate.async_rapid_heat(enable=value)
    coordinator.async_rapid_heat.assert_not_awaited()


@pytest.mark.parametrize(
    "condition", ["disabled", "blocked", "unavailable", "missing_snapshot"]
)
async def test_conditioning_controls_fail_closed_without_sending(coordinator, condition):
    if condition == "disabled":
        coordinator.controls_enabled = False
    elif condition == "blocked":
        coordinator.controls_available = False
    elif condition == "unavailable":
        coordinator.last_update_success = False
    else:
        coordinator.data = {}
    switch = OmodaJaecooRearDefrost(coordinator, VIN)
    climate = OmodaJaecooClimate(coordinator, VIN)
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()
    with pytest.raises(HomeAssistantError):
        await climate.async_rapid_heat()
    assert switch.is_on in (None, False)
    coordinator.async_rear_defrost.assert_not_awaited()
    coordinator.async_rapid_heat.assert_not_awaited()


async def test_conditioning_cancellation_is_not_retried(coordinator):
    coordinator.async_rapid_heat.side_effect = asyncio.CancelledError
    coordinator.async_rear_defrost.side_effect = asyncio.CancelledError
    climate = OmodaJaecooClimate(coordinator, VIN)
    switch = OmodaJaecooRearDefrost(coordinator, VIN)
    with pytest.raises(asyncio.CancelledError):
        await climate.async_rapid_heat()
    with pytest.raises(asyncio.CancelledError):
        await switch.async_turn_on()
    assert coordinator.async_rapid_heat.await_count == 1
    assert coordinator.async_rear_defrost.await_count == 1
    assert switch.is_on is False
    assert coordinator.last_command_status == {}


async def test_rejected_conditioning_keeps_reported_state_and_does_not_retry(
    hass, entry, coordinator
):
    async def rejected(vin, *args):
        coordinator.last_command_status[vin] = "rejected"
        coordinator.async_update_listeners()
        raise HomeAssistantError("Vehicle command was rejected.")

    async def unknown(vin, *args):
        coordinator.last_command_status[vin] = "unknown_outcome"
        coordinator.async_update_listeners()
        raise HomeAssistantError("Command outcome unknown; do not retry.")

    coordinator.data = {VIN: snapshot(rear_defrost=True)}
    coordinator.async_rear_defrost.side_effect = rejected
    coordinator.async_rapid_heat.side_effect = unknown
    await setup(hass, entry, coordinator)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id(hass, "switch", REAR_WINDOW_HEATING)},
            blocking=True,
        )
    switch = state(hass, "switch", REAR_WINDOW_HEATING)
    assert switch.state == "on"
    assert switch.attributes["last_command_status"] == "rejected"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "rapid_heat",
            {"entity_id": entity_id(hass, "climate", "climate")},
            blocking=True,
        )
    assert coordinator.async_rear_defrost.await_count == 1
    assert coordinator.async_rapid_heat.await_count == 1
    assert (
        state(hass, "switch", REAR_WINDOW_HEATING).attributes["last_command_status"]
        == "unknown_outcome"
    )


async def test_switch_exposes_no_secrets_and_no_io(coordinator):
    coordinator.last_command_status[VIN] = f"server secret {PIN}"
    switch = OmodaJaecooRearDefrost(coordinator, VIN)
    assert switch.unique_id == f"eu_{VIN}_{REAR_WINDOW_HEATING}"
    assert switch.device_info["identifiers"] == {(DOMAIN, f"eu_{VIN}")}
    assert switch.has_entity_name and switch.translation_key == REAR_WINDOW_HEATING
    assert PIN not in repr(switch.extra_state_attributes)
    coordinator.async_rear_defrost.assert_not_awaited()


# --- Real coordinator, mocked command client -------------------------------------


async def real_setup(hass, entry, *, duration=None):
    entry.add_to_hass(hass)
    options = {"enable_controls": True}
    if duration is not None:
        options["climate_duration"] = duration
    hass.config_entries.async_update_entry(entry, options=options)
    client = AsyncMock(spec=CommandClient)
    with patch(f"custom_components.{DOMAIN}.CommandClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coord = entry.runtime_data
    coord.async_request_refresh = AsyncMock()
    return coord, client


async def test_coordinator_rapid_heat_uses_configured_duration(hass, entry, mock_api):
    coord, client = await real_setup(hass, entry, duration=10)
    with patch.object(coord, "_start_climate_followup") as followup:
        await coord.async_rapid_heat(VIN, True, 21.5)
        followup.assert_called_once_with(VIN)
    client.async_rapid_heat.assert_awaited_once_with(VIN, PIN, True, 21.5, 10)
    client.async_climate.assert_not_awaited()
    client.async_lock.assert_not_awaited()
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    assert coord.pending_lock_target(VIN) is None


async def test_coordinator_rear_defrost_keeps_explicit_duration(hass, entry, mock_api):
    coord, client = await real_setup(hass, entry, duration=10)
    await coord.async_rear_defrost(VIN, False, 5)
    client.async_rear_defrost.assert_awaited_once_with(VIN, PIN, False, 5)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"


async def test_second_conditioning_command_is_refused_not_queued(
    hass, entry, mock_api
):
    coord, client = await real_setup(hass, entry)
    await coord.async_rapid_heat(VIN, True, 21.0)
    with pytest.raises(HomeAssistantError, match="30 seconds"):
        await coord.async_rear_defrost(VIN, True)
    assert client.async_rapid_heat.await_count == 1
    client.async_rear_defrost.assert_not_awaited()


@pytest.mark.parametrize("kind", ["wake", "", "lock_control", "rapid heat"])
async def test_unknown_command_kinds_are_refused_before_any_handling(
    hass, entry, mock_api, kind
):
    coord, client = await real_setup(hass, entry)
    with pytest.raises(ServiceValidationError):
        await coord._async_command(VIN, kind, on=True)
    for method in (
        client.async_lock,
        client.async_climate,
        client.async_rapid_heat,
        client.async_rear_defrost,
    ):
        method.assert_not_awaited()
    assert COMMAND_KINDS == {"lock", "climate", "rapid_heat", "rear_defrost"}


@pytest.mark.parametrize(
    "settings,match",
    [({"enable_controls": False}, "options"), ({}, "options")],
)
async def test_conditioning_requires_opt_in(hass, entry, mock_api, settings, match):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=settings)
    client = AsyncMock(spec=CommandClient)
    with patch(f"custom_components.{DOMAIN}.CommandClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coord = entry.runtime_data
    with pytest.raises(ServiceValidationError, match=match):
        await coord.async_rapid_heat(VIN, True, 21.0)
    with pytest.raises(ServiceValidationError, match=match):
        await coord.async_rear_defrost(VIN, True)
    client.async_rapid_heat.assert_not_awaited()
    client.async_rear_defrost.assert_not_awaited()


async def test_pin_block_pauses_conditioning_commands(hass, entry, mock_api):
    coord, client = await real_setup(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "pin_blocked": True}
    )
    with pytest.raises(ServiceValidationError, match="paused"):
        await coord.async_rear_defrost(VIN, True)
    with pytest.raises(ServiceValidationError, match="paused"):
        await coord.async_rapid_heat(VIN, True, 21.0)
    client.async_rear_defrost.assert_not_awaited()
    client.async_rapid_heat.assert_not_awaited()
