"""Shared cached-cloud entities; properties never perform I/O."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OmodaJaecooCoordinator, VehicleSnapshot

_COMMAND_STATUSES = frozenset(
    {
        "not_requested",
        "not_sent",
        "submitting",
        "accepted_unconfirmed",
        "rejected",
        "unknown_outcome",
        "pin_blocked",
        "state_observed",
        "confirmation_timeout",
    }
)


class OmodaJaecooEntity(CoordinatorEntity[OmodaJaecooCoordinator]):
    """Common identity and in-memory snapshot access for one selected vehicle."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: OmodaJaecooCoordinator, vin: str, suffix: str
    ) -> None:
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"eu_{vin}_{suffix}"
        vehicle = coordinator.vehicles[vin]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"eu_{vin}")},
            name=vehicle.name,
            manufacturer="Omoda / Jaecoo",
            model=vehicle.model,
        )

    @property
    def snapshot(self) -> VehicleSnapshot | None:
        return (self.coordinator.data or {}).get(self._vin)

    @property
    def available(self) -> bool:
        return super().available and self.snapshot is not None


class OmodaJaecooControlEntity(OmodaJaecooEntity):
    """A request is not physical confirmation; expose only fixed outcome labels."""

    _attr_assumed_state = True

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        status = self.coordinator.last_command_status.get(self._vin)
        return {"last_command_status": status} if status in _COMMAND_STATUSES else {}

    def _ensure_controls_available(self) -> None:
        if (
            not self.coordinator.controls_enabled
            or not self.coordinator.controls_available
        ):
            raise HomeAssistantError(
                "Vehicle controls are disabled or PIN verification is blocked."
            )
        if not self.available:
            raise HomeAssistantError(
                "Vehicle cloud data is unavailable; no command was sent."
            )
