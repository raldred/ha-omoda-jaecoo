"""Native HA body/control entities with an in-memory coordinator, never transport."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import DOMAIN, PIN, VIN, VIN_2
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import async_get as async_get_restore_state
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.omoda_jaecoo.binary_sensor import (
    DESCRIPTIONS,
    OmodaJaecooBinarySensor,
)
from custom_components.omoda_jaecoo.climate import (
    OmodaJaecooClimate,
    temperature_bounds,
)
from custom_components.omoda_jaecoo.lock import OmodaJaecooLock


def snapshot(**changes):
    return SimpleNamespace(
        **{
            "door_locked": None,
            "climate_on": None,
            "cabin_temperature": None,
            "target_temperature": None,
            "doors": {},
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
    """Real HA subscriptions but no API/session/PIN handling or network calls."""

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
        self.async_lock = AsyncMock(side_effect=self._accepted)
        self.async_climate = AsyncMock(side_effect=self._accepted)

    def pending_lock_target(self, vin):
        return None

    def lock_state_uncertain(self, vin):
        return False

    async def _accepted(self, vin, *args):
        self.last_command_status[vin] = "accepted_unconfirmed"

    async def _async_update_data(self):
        return self.data


@pytest.fixture
def coordinator(hass, entry):
    return FakeCoordinator(hass, entry)


async def setup(hass, entry, coordinator):
    """Exercise HA entity registration and services without a vehicle client."""
    entry.add_to_hass(hass)
    with (
        patch(
            f"custom_components.{DOMAIN}.OmodaJaecooCoordinator",
            return_value=coordinator,
        ),
        patch(
            f"custom_components.{DOMAIN}.PLATFORMS",
            [Platform.BINARY_SENSOR, Platform.LOCK, Platform.CLIMATE],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def entity_id(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"eu_{VIN}_{suffix}")


def state(hass, domain, suffix):
    return hass.states.get(entity_id(hass, domain, suffix))


async def test_real_coordinator_snapshot_and_patched_actions(
    hass, entry, mock_api, vehicles
):
    from dataclasses import replace

    mock_api.async_list_vehicles.return_value = [
        replace(
            vehicles[0],
            min_temperature=16,
            max_temperature=30,
            temperature_step=0.5,
            allowed_air_durations=(5, 10, 15),
        )
    ]
    mock_api.async_realtime.return_value = {
        "doorLock": 1,
        "frontHVACState": 0,
        "inCarTemperature": 18.5,
        "frontSetTempLeft": 22,
        "frontLeftDoor": 1,
        "trunkDoor": 0,
    }
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={"enable_controls": True})
    with patch(
        f"custom_components.{DOMAIN}.PLATFORMS",
        [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LOCK, Platform.CLIMATE],
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.data[VIN].door_locked is False
    assert state(hass, "lock", "lock").state == "unlocked"
    assert state(hass, "binary_sensor", "door_lock").state == "on"
    assert state(hass, "binary_sensor", "door_front_left").state == "on"
    assert state(hass, "binary_sensor", "boot").state == "off"
    assert state(hass, "climate", "climate").state == "off"
    assert state(hass, "climate", "climate").attributes["current_temperature"] == 18.5
    with (
        patch.object(coordinator, "async_lock", new_callable=AsyncMock) as lock_action,
        patch.object(
            coordinator, "async_climate", new_callable=AsyncMock
        ) as climate_action,
    ):
        await hass.services.async_call(
            "lock",
            "lock",
            {"entity_id": entity_id(hass, "lock", "lock")},
            blocking=True,
        )
        await hass.services.async_call(
            "climate",
            "turn_on",
            {"entity_id": entity_id(hass, "climate", "climate")},
            blocking=True,
        )
        lock_action.assert_awaited_once_with(VIN, True)
        climate_action.assert_awaited_once_with(VIN, True, 21)
    assert state(hass, "lock", "lock").state == "unlocked"
    assert state(hass, "climate", "climate").state == "off"
    assert state(hass, "climate", "climate").attributes["temperature"] == 22
    mock_api.async_login.assert_not_awaited()
    mock_api.async_control_session.assert_not_awaited()


async def test_read_only_by_default_and_only_selected_vins(hass, entry, coordinator):
    coordinator.controls_enabled = False
    coordinator.controls_available = False
    coordinator.vehicles[VIN_2] = vehicle(vin=VIN_2)
    await setup(hass, entry, coordinator)
    assert len(hass.states.async_all("binary_sensor")) == 7
    assert not hass.states.async_all("lock")
    assert not hass.states.async_all("climate")
    coordinator.async_lock.assert_not_awaited()
    coordinator.async_climate.assert_not_awaited()
    assert all(
        VIN_2 not in item.unique_id for item in er.async_get(hass).entities.values()
    )


async def test_unknown_cloud_state_is_not_off_closed_or_locked(
    hass, entry, coordinator
):
    await setup(hass, entry, coordinator)
    assert state(hass, "lock", "lock").state == "unknown"
    assert state(hass, "climate", "climate").state == "unknown"
    assert all(
        item.state == "unknown" for item in hass.states.async_all("binary_sensor")
    )
    assert state(hass, "lock", "lock").attributes["assumed_state"] is True
    assert state(hass, "climate", "climate").attributes["assumed_state"] is True
    coordinator.async_lock.assert_not_awaited()
    coordinator.async_climate.assert_not_awaited()


@pytest.mark.parametrize(
    "locked,on,expected_lock,expected_climate",
    [(True, True, "off", "on"), (False, False, "on", "off")],
)
async def test_binary_sensor_semantics_and_cloud_updates(
    hass, entry, coordinator, locked, on, expected_lock, expected_climate
):
    await setup(hass, entry, coordinator)
    coordinator.async_set_updated_data(
        {
            VIN: snapshot(
                door_locked=locked,
                climate_on=on,
                doors={
                    "front_left": True,
                    "front_right": False,
                    "rear_left": None,
                    "rear_right": True,
                    "boot": False,
                },
                cabin_temperature=19.5,
                target_temperature=22.5,
            )
        }
    )
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", "door_lock").state == expected_lock
    assert state(hass, "binary_sensor", "climate_running").state == expected_climate
    assert state(hass, "binary_sensor", "door_front_left").state == "on"
    assert state(hass, "binary_sensor", "door_front_right").state == "off"
    assert state(hass, "binary_sensor", "door_rear_left").state == "unknown"
    assert state(hass, "binary_sensor", "door_rear_right").state == "on"
    assert state(hass, "binary_sensor", "boot").state == "off"
    assert (
        state(hass, "binary_sensor", "door_lock").attributes["device_class"] == "lock"
    )
    assert (
        state(hass, "binary_sensor", "climate_running").attributes["device_class"]
        == "running"
    )
    assert state(hass, "lock", "lock").state == ("locked" if locked else "unlocked")
    climate = state(hass, "climate", "climate")
    assert climate.state == ("heat_cool" if on else "off")
    assert climate.attributes["current_temperature"] == 19.5
    assert climate.attributes["temperature"] == 22.5
    assert climate.attributes["min_temp"] == 16
    assert climate.attributes["max_temp"] == 30
    assert climate.attributes["target_temp_step"] == 0.5


@pytest.mark.parametrize("service,locked", [("lock", True), ("unlock", False)])
async def test_native_lock_service_acceptance_is_not_confirmation(
    hass, entry, coordinator, service, locked
):
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        "lock", service, {"entity_id": entity_id(hass, "lock", "lock")}, blocking=True
    )
    coordinator.async_lock.assert_awaited_once_with(VIN, locked)
    lock = state(hass, "lock", "lock")
    assert lock.state == "unknown"
    assert lock.attributes["last_command_status"] == "accepted_unconfirmed"
    assert "is_locking" not in lock.attributes


@pytest.mark.parametrize("on", [False, None])
async def test_set_temperature_off_or_unknown_is_local_only(
    hass, entry, coordinator, on
):
    coordinator.data = {VIN: snapshot(climate_on=on)}
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id(hass, "climate", "climate"), "temperature": 23.5},
        blocking=True,
    )
    coordinator.async_climate.assert_not_awaited()
    climate = state(hass, "climate", "climate")
    assert climate.attributes["temperature"] == 23.5
    assert climate.state == ("off" if on is False else "unknown")
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": climate.entity_id}, blocking=True
    )
    coordinator.async_climate.assert_awaited_once_with(VIN, True, 23.5)
    assert state(hass, "climate", "climate").state == climate.state


async def test_confirmed_on_set_temperature_sends_once_but_keeps_cloud_target(
    hass, entry, coordinator
):
    coordinator.data = {VIN: snapshot(climate_on=True, target_temperature=20)}
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id(hass, "climate", "climate"), "temperature": 24},
        blocking=True,
    )
    coordinator.async_climate.assert_awaited_once_with(VIN, True, 24)
    climate = state(hass, "climate", "climate")
    assert climate.state == "heat_cool"
    assert climate.attributes["temperature"] == 20
    assert climate.attributes["selected_temperature"] == 24
    assert climate.attributes["last_command_status"] == "accepted_unconfirmed"


@pytest.mark.parametrize(
    "service,data,on",
    [
        ("turn_on", {}, True),
        ("turn_off", {}, False),
        ("set_hvac_mode", {"hvac_mode": "heat_cool"}, True),
        ("set_hvac_mode", {"hvac_mode": "off"}, False),
    ],
)
async def test_climate_explicit_modes_use_coordinator_without_optimism(
    hass, entry, coordinator, service, data, on
):
    await setup(hass, entry, coordinator)
    await hass.services.async_call(
        "climate",
        service,
        {"entity_id": entity_id(hass, "climate", "climate"), **data},
        blocking=True,
    )
    coordinator.async_climate.assert_awaited_once_with(VIN, on, 21)
    assert state(hass, "climate", "climate").state == "unknown"


@pytest.mark.parametrize(
    "cloud_on,requested_mode",
    [(True, "off"), (False, "heat_cool"), (None, "heat_cool")],
)
async def test_combined_mode_temperature_rejected_without_ignored_off_or_start(
    hass, entry, coordinator, cloud_on, requested_mode
):
    coordinator.data = {VIN: snapshot(climate_on=cloud_on)}
    await setup(hass, entry, coordinator)
    with pytest.raises(ServiceValidationError, match="climate.set_hvac_mode"):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {
                "entity_id": entity_id(hass, "climate", "climate"),
                "temperature": 24,
                "hvac_mode": requested_mode,
            },
            blocking=True,
        )
    coordinator.async_climate.assert_not_awaited()
    climate = state(hass, "climate", "climate")
    assert climate.state == (
        "unknown" if cloud_on is None else "heat_cool" if cloud_on else "off"
    )
    assert climate.attributes["selected_temperature"] == 21


@pytest.mark.parametrize(
    "temperature", [15, 31, 21.25, float("nan"), float("inf"), True, None, "22"]
)
async def test_invalid_temperature_never_sends_or_changes_preference(
    coordinator, temperature
):
    climate = OmodaJaecooClimate(coordinator, VIN)
    with pytest.raises(ServiceValidationError):
        await climate.async_set_temperature(temperature=temperature)
    assert climate.target_temperature == 21
    coordinator.async_climate.assert_not_awaited()


async def test_unsupported_hvac_mode_never_sends(coordinator):
    with pytest.raises(ServiceValidationError):
        await OmodaJaecooClimate(coordinator, VIN).async_set_hvac_mode(HVACMode.AUTO)
    coordinator.async_climate.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"min_temperature": None},
        {"max_temperature": None},
        {"temperature_step": None},
        {"temperature_step": 0},
        {"temperature_step": -1},
        {"temperature_step": 20},
        {"min_temperature": 30},
        {"max_temperature": float("nan")},
        {"min_temperature": True},
        {"min_temperature": 13},
        {"max_temperature": 34},
        {"temperature_step": 2},
        {"allowed_air_durations": ()},
        {"allowed_air_durations": None},
        {"allowed_air_durations": (0,)},
        {"allowed_air_durations": (61,)},
        {"allowed_air_durations": (True,)},
        {"allowed_air_durations": ("15",)},
    ],
)
async def test_invalid_metadata_skips_climate_not_binary_or_lock(
    hass, entry, coordinator, changes
):
    coordinator.vehicles[VIN] = vehicle(**changes)
    assert temperature_bounds(coordinator.vehicles[VIN]) is None
    await setup(hass, entry, coordinator)
    assert not hass.states.async_all("climate")
    assert len(hass.states.async_all("lock")) == 1
    assert len(hass.states.async_all("binary_sensor")) == 7
    coordinator.async_climate.assert_not_awaited()


@pytest.mark.parametrize(
    "minimum,maximum,step,expected",
    [(22, 30, 0.5, 22), (16, 20, 0.5, 20), (16.25, 30, 0.5, 21.25), (16, 20.7, 1, 20)],
)
async def test_default_local_target_clamps_and_snaps_to_grid(
    coordinator, minimum, maximum, step, expected
):
    coordinator.vehicles[VIN] = vehicle(
        min_temperature=minimum, max_temperature=maximum, temperature_step=step
    )
    assert OmodaJaecooClimate(coordinator, VIN).target_temperature == expected


@pytest.mark.parametrize(
    "condition", ["disabled", "blocked", "unavailable", "missing_snapshot"]
)
async def test_controls_fail_closed_without_sending(coordinator, condition):
    if condition == "disabled":
        coordinator.controls_enabled = False
    elif condition == "blocked":
        coordinator.controls_available = False
    elif condition == "unavailable":
        coordinator.last_update_success = False
    else:
        coordinator.data = {}
    with pytest.raises(HomeAssistantError):
        await OmodaJaecooLock(coordinator, VIN).async_lock()
    with pytest.raises(HomeAssistantError):
        await OmodaJaecooClimate(coordinator, VIN).async_turn_on()
    with pytest.raises(HomeAssistantError):
        await OmodaJaecooClimate(coordinator, VIN).async_turn_off()
    coordinator.async_lock.assert_not_awaited()
    coordinator.async_climate.assert_not_awaited()


async def test_cancellation_propagates_without_retry_or_optimistic_state(coordinator):
    import asyncio

    coordinator.async_lock.side_effect = asyncio.CancelledError
    coordinator.async_climate.side_effect = asyncio.CancelledError
    lock = OmodaJaecooLock(coordinator, VIN)
    climate = OmodaJaecooClimate(coordinator, VIN)
    with pytest.raises(asyncio.CancelledError):
        await lock.async_lock()
    with pytest.raises(asyncio.CancelledError):
        await climate.async_turn_on()
    assert lock.is_locked is None
    assert climate.hvac_mode is None
    assert (
        coordinator.async_lock.await_count == coordinator.async_climate.await_count == 1
    )
    assert coordinator.last_command_status == {}


async def test_service_rejection_propagates_no_retry_or_local_state_change(
    hass, entry, coordinator
):
    coordinator.data = {
        VIN: snapshot(door_locked=False, climate_on=True, target_temperature=20)
    }

    async def rejected(vin, *args):
        coordinator.last_command_status[vin] = "rejected"
        coordinator.async_update_listeners()
        raise HomeAssistantError("Command rejected.")

    async def unknown_outcome(vin, *args):
        coordinator.last_command_status[vin] = "unknown_outcome"
        coordinator.async_update_listeners()
        raise HomeAssistantError("Command outcome unknown; do not retry.")

    coordinator.async_lock.side_effect = rejected
    coordinator.async_climate.side_effect = unknown_outcome
    await setup(hass, entry, coordinator)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "lock",
            "lock",
            {"entity_id": entity_id(hass, "lock", "lock")},
            blocking=True,
        )
    assert state(hass, "lock", "lock").attributes["last_command_status"] == "rejected"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id(hass, "climate", "climate"), "temperature": 24},
            blocking=True,
        )
    assert (
        coordinator.async_lock.await_count == coordinator.async_climate.await_count == 1
    )
    assert state(hass, "lock", "lock").state == "unlocked"
    assert (
        state(hass, "lock", "lock").attributes["last_command_status"]
        == "unknown_outcome"
    )
    assert (
        state(hass, "climate", "climate").attributes["last_command_status"]
        == "unknown_outcome"
    )
    assert state(hass, "climate", "climate").attributes["selected_temperature"] == 21
    assert state(hass, "climate", "climate").attributes["temperature"] == 20


@pytest.mark.parametrize(
    "status",
    [
        "not_requested",
        "not_sent",
        "submitting",
        "accepted_unconfirmed",
        "rejected",
        "unknown_outcome",
        "pin_blocked",
    ],
)
async def test_only_safe_command_statuses_are_exposed(coordinator, status):
    coordinator.last_command_status[VIN] = status
    for entity in (
        OmodaJaecooLock(coordinator, VIN),
        OmodaJaecooClimate(coordinator, VIN),
    ):
        assert entity.extra_state_attributes["last_command_status"] == status


async def test_properties_are_memory_only_and_expose_no_secrets(coordinator):
    coordinator.last_command_status[VIN] = f"server secret {PIN}"
    entities = [OmodaJaecooLock(coordinator, VIN), OmodaJaecooClimate(coordinator, VIN)]
    entities += [
        OmodaJaecooBinarySensor(coordinator, VIN, description)
        for description in DESCRIPTIONS
    ]
    for entity in entities:
        assert entity.unique_id.startswith(f"eu_{VIN}_")
        assert entity.device_info["identifiers"] == {(DOMAIN, f"eu_{VIN}")}
        assert entity.has_entity_name
        assert entity.available
        assert PIN not in repr(entity.extra_state_attributes)
        if isinstance(entity, OmodaJaecooBinarySensor):
            assert entity.is_on is None
        elif isinstance(entity, OmodaJaecooLock):
            assert entity.is_locked is None
        else:
            assert entity.hvac_mode is None
            assert entity.current_temperature is None
            assert entity.target_temperature == 21
            assert entity.supported_features & ClimateEntityFeature.TURN_ON
    coordinator.async_lock.assert_not_awaited()
    coordinator.async_climate.assert_not_awaited()


@pytest.mark.parametrize(
    "restore_target,expected", [(24.5, 24.5), (200, 21), (21.25, 21), (None, 21)]
)
async def test_restore_only_safe_local_target_never_hvac_or_command(
    hass, entry, coordinator, restore_target, expected
):
    from homeassistant.core import State
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.util import dt as dt_util

    # Use deterministic entity_id from the first registration, then emulate a restart.
    await setup(hass, entry, coordinator)
    climate_id = entity_id(hass, "climate", "climate")
    with patch(
        f"custom_components.{DOMAIN}.PLATFORMS",
        [Platform.BINARY_SENSOR, Platform.LOCK, Platform.CLIMATE],
    ):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert not coordinator._listeners
    restore = async_get_restore_state(hass)
    restore.last_states[climate_id] = StoredState(
        State(
            climate_id,
            "heat_cool",
            {"selected_temperature": restore_target, "temperature": 29},
        ),
        None,
        dt_util.utcnow(),
    )
    with (
        patch(
            f"custom_components.{DOMAIN}.OmodaJaecooCoordinator",
            return_value=coordinator,
        ),
        patch(
            f"custom_components.{DOMAIN}.PLATFORMS",
            [Platform.BINARY_SENSOR, Platform.LOCK, Platform.CLIMATE],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert state(hass, "climate", "climate").state == "unknown"
    assert (
        state(hass, "climate", "climate").attributes["selected_temperature"] == expected
    )
    coordinator.async_climate.assert_not_awaited()
    coordinator.async_lock.assert_not_awaited()
