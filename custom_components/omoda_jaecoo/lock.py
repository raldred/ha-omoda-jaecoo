"""Opt-in native lock; accepted requests never change the cached lock state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import OmodaJaecooControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    if coordinator.controls_enabled:
        async_add_entities(
            OmodaJaecooLock(coordinator, vin) for vin in coordinator.selected_vins
        )


class OmodaJaecooLock(OmodaJaecooControlEntity, LockEntity):
    """A deliberate lock remains possible when the cloud lock reading is unknown."""

    _attr_translation_key = "lock"

    def __init__(self, coordinator, vin: str) -> None:
        super().__init__(coordinator, vin, "lock")

    @property
    def is_locked(self) -> bool | None:
        return self.snapshot.door_locked if self.snapshot else None

    async def async_lock(self, **kwargs: Any) -> None:
        self._ensure_controls_available()
        await self.coordinator.async_lock(self._vin, True)
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        self._ensure_controls_available()
        await self.coordinator.async_lock(self._vin, False)
        self.async_write_ha_state()
