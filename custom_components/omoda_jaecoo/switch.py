"""Opt-in independent rear-window heating; state always from the cloud snapshot."""

from __future__ import annotations

import math
from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .capability import (
    REAR_WINDOW_HEATING,
    allowed_durations,
    rear_window_heating_reported,
    temperature_bounds,
)
from .entity import OmodaJaecooControlEntity


def _explicit_number(value: Any) -> float:
    """Reject boolean settings before numeric coercion can erase their type."""
    if isinstance(value, bool):
        raise vol.Invalid("Choose a number, not a boolean.")
    return vol.Coerce(float)(value)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entity_platform.async_get_current_platform().async_register_entity_service(
        "rear_defrost_with_climate",
        {
            vol.Required("enable"): bool,
            vol.Required("temperature_celsius"): _explicit_number,
            vol.Required("duration"): _explicit_number,
            vol.Required("acknowledge_climate_change"): vol.In([True]),
        },
        "async_rear_defrost_with_climate",
    )
    if not coordinator.controls_enabled:
        return
    snapshots = coordinator.data or {}
    async_add_entities(
        OmodaJaecooRearDefrost(coordinator, vin)
        for vin in coordinator.selected_vins
        if allowed_durations(coordinator.vehicles[vin])
        and rear_window_heating_reported(snapshots.get(vin))
    )


class OmodaJaecooRearDefrost(OmodaJaecooControlEntity, SwitchEntity):
    """The car reports rWinHeatingState, so an accepted write stays unconfirmed."""

    _attr_translation_key = REAR_WINDOW_HEATING
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, vin: str) -> None:
        super().__init__(coordinator, vin, REAR_WINDOW_HEATING)

    @property
    def is_on(self) -> bool | None:
        """An unknown reading stays unknown; it is never presented as off."""
        if (snapshot := self.snapshot) is None:
            return None
        return getattr(snapshot, "extras", {}).get(REAR_WINDOW_HEATING)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_request(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_request(False)

    async def _async_request(self, on: bool) -> None:
        self._ensure_controls_available()
        await self.coordinator.async_rear_defrost(self._vin, on)
        self.async_write_ha_state()

    async def async_rear_defrost_with_climate(
        self,
        enable: bool,
        temperature_celsius: float,
        duration: float,
        acknowledge_climate_change: bool,
    ) -> None:
        """Coupled action: OFF also stops cabin heating, unlike normal switch OFF."""
        self._ensure_controls_available()
        if type(enable) is not bool or acknowledge_climate_change is not True:
            raise ServiceValidationError(
                "Explicitly choose on/off and acknowledge that cabin climate also changes."
            )
        bounds = temperature_bounds(self.coordinator.vehicles[self._vin])
        if (
            bounds is None
            or isinstance(temperature_celsius, bool)
            or not isinstance(temperature_celsius, (int, float))
            or not math.isfinite(temperature_celsius)
            or not bounds[0] <= temperature_celsius <= bounds[1]
            or not math.isclose(
                (temperature_celsius - bounds[0]) / bounds[2],
                round((temperature_celsius - bounds[0]) / bounds[2]),
                abs_tol=1e-8,
            )
        ):
            raise ServiceValidationError("Choose a supported temperature in Celsius.")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration not in allowed_durations(self.coordinator.vehicles[self._vin])
        ):
            raise ServiceValidationError(
                "Choose a reported supported duration in minutes."
            )
        await self.coordinator.async_rear_defrost_with_climate(
            self._vin,
            enable,
            temperature_celsius,
            int(duration),
            acknowledge_climate_change=True,
        )
        self.async_write_ha_state()
