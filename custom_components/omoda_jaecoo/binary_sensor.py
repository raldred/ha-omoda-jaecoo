"""Read-only vehicle body state from cached cloud telemetry."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import OmodaJaecooEntity

DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="door_lock",
        translation_key="door_lock",
        device_class=BinarySensorDeviceClass.LOCK,
    ),
    BinarySensorEntityDescription(
        key="climate_running",
        translation_key="climate_running",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    *(
        BinarySensorEntityDescription(
            key=key, translation_key=key, device_class=BinarySensorDeviceClass.DOOR
        )
        for key in (
            "door_front_left",
            "door_front_right",
            "door_rear_left",
            "door_rear_right",
            "boot",
        )
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        OmodaJaecooBinarySensor(coordinator, vin, description)
        for vin in coordinator.selected_vins
        for description in DESCRIPTIONS
    )


class OmodaJaecooBinarySensor(OmodaJaecooEntity, BinarySensorEntity):
    """Unknown body state remains unknown, not closed/off/locked."""

    def __init__(
        self, coordinator, vin: str, description: BinarySensorEntityDescription
    ) -> None:
        super().__init__(coordinator, vin, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if (snapshot := self.snapshot) is None:
            return None
        key = self.entity_description.key
        if key == "door_lock":
            return None if snapshot.door_locked is None else not snapshot.door_locked
        if key == "climate_running":
            return snapshot.climate_on
        return snapshot.doors.get(key.removeprefix("door_"))
