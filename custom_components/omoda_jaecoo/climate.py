"""Opt-in remote climate with cloud-confirmed state and a local target preference."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import OmodaJaecooControlEntity


def temperature_bounds(vehicle) -> tuple[float, float, float] | None:
    """Do not invent control capabilities for vehicles missing model metadata."""
    values = (
        getattr(vehicle, "min_temperature", None),
        getattr(vehicle, "max_temperature", None),
        getattr(vehicle, "temperature_step", None),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        return None
    minimum, maximum, step = (float(value) for value in values)
    durations = getattr(vehicle, "allowed_air_durations", ())
    if (
        not 14 <= minimum < maximum <= 33
        or step not in (0.5, 1)
        or not isinstance(durations, tuple)
        or not durations
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 60
            for value in durations
        )
    ):
        return None
    return minimum, maximum, step


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if coordinator.controls_enabled:
        async_add_entities(
            OmodaJaecooClimate(coordinator, vin)
            for vin in coordinator.selected_vins
            if temperature_bounds(coordinator.vehicles[vin]) is not None
        )


class OmodaJaecooClimate(OmodaJaecooControlEntity, ClimateEntity, RestoreEntity):
    """Restore only a local temperature preference, never a previous HVAC state."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator, vin: str) -> None:
        super().__init__(coordinator, vin, "climate")
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
        bounds = temperature_bounds(coordinator.vehicles[vin])
        if bounds is None:
            raise ValueError("Climate controls require valid temperature bounds.")
        self._attr_min_temp, self._attr_max_temp, self._attr_target_temperature_step = (
            bounds
        )
        minimum, maximum, step = bounds
        last_step = math.floor((maximum - minimum) / step + 1e-9)
        chosen_step = min(last_step, max(0, round((21 - minimum) / step)))
        self._selected_temperature = minimum + chosen_step * step

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (state := await self.async_get_last_state()) is not None:
            try:
                self._selected_temperature = self._validate_temperature(
                    state.attributes.get("selected_temperature")
                )
            except ServiceValidationError:
                pass

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "selected_temperature": self._selected_temperature,
        }

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self.snapshot is None or self.snapshot.climate_on is None:
            return None
        return HVACMode.HEAT_COOL if self.snapshot.climate_on else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        return self.snapshot.cabin_temperature if self.snapshot else None

    @property
    def target_temperature(self) -> float:
        if self.snapshot is not None and self.snapshot.target_temperature is not None:
            return self.snapshot.target_temperature
        return self._selected_temperature

    def _validate_temperature(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ServiceValidationError(
                "Choose a temperature within the vehicle's supported range and step."
            )
        temperature = float(value)
        if (
            not math.isfinite(temperature)
            or not self.min_temp <= temperature <= self.max_temp
        ):
            raise ServiceValidationError(
                "Choose a temperature within the vehicle's supported range and step."
            )
        steps = (temperature - self.min_temp) / self.target_temperature_step
        if not math.isclose(steps, round(steps), abs_tol=1e-7, rel_tol=0):
            raise ServiceValidationError(
                "Choose a temperature within the vehicle's supported range and step."
            )
        return temperature

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if "hvac_mode" in kwargs:
            raise ServiceValidationError(
                "Set the HVAC mode separately using climate.set_hvac_mode, then set the temperature."
            )
        temperature = self._validate_temperature(kwargs.get(ATTR_TEMPERATURE))
        # Never start HVAC implicitly, including when its cloud state is unknown.
        if self.hvac_mode == HVACMode.HEAT_COOL:
            self._ensure_controls_available()
            await self.coordinator.async_climate(self._vin, True, temperature)
        self._selected_temperature = temperature
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        elif hvac_mode == HVACMode.HEAT_COOL:
            await self.async_turn_on()
        else:
            raise ServiceValidationError("Only off and heat/cool modes are supported.")

    async def async_turn_on(self) -> None:
        self._ensure_controls_available()
        await self.coordinator.async_climate(
            self._vin, True, self._selected_temperature
        )
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        self._ensure_controls_available()
        await self.coordinator.async_climate(
            self._vin, False, self._selected_temperature
        )
        self.async_write_ha_state()
