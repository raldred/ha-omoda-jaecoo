"""Explicitly opted-in last cloud GPS reports, never live or restored positions."""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_LOCATION
from .coordinator import OmodaJaecooCoordinator
from .entity import OmodaJaecooEntity
from .telemetry import Position


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Do not create GPS entities unless recording location was explicitly enabled."""
    if not entry.options.get(CONF_ENABLE_LOCATION, False):
        return
    coordinator: OmodaJaecooCoordinator = entry.runtime_data
    async_add_entities(
        OmodaJaecooLocation(coordinator, vin) for vin in coordinator.selected_vins
    )


class OmodaJaecooLocation(OmodaJaecooEntity, TrackerEntity):
    """Read only validated coordinator memory; no polling, restore, or extrapolation."""

    _attr_translation_key = "location"
    _attr_source_type = SourceType.GPS
    _attr_icon = "mdi:car"

    def __init__(self, coordinator: OmodaJaecooCoordinator, vin: str) -> None:
        super().__init__(coordinator, vin, "location")

    @property
    def position(self) -> Position | None:
        return self.coordinator.positions.get(self._vin)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.position is not None

    @property
    def latitude(self) -> float | None:
        position = self.position
        return position.latitude if position is not None else None

    @property
    def longitude(self) -> float | None:
        position = self.position
        return position.longitude if position is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        position = self.position
        return {
            "source": "cloud_snapshot",
            "observed_at": (
                position.observed_at.isoformat()
                if position is not None and position.observed_at is not None
                else None
            ),
            "fetched_at": position.fetched_at.isoformat() if position else None,
            "source_time_known": position is not None
            and position.observed_at is not None,
        }
