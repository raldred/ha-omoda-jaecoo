"""Opt-in independent rear-window heating; state always from the cloud snapshot."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .capability import (
    REAR_WINDOW_HEATING,
    allowed_durations,
    rear_window_heating_reported,
)
from .entity import OmodaJaecooControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
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
