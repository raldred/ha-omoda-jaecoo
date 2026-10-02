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

EXTRA_KEYS = frozenset(
    {
        "window_front_left",
        "window_front_right",
        "window_rear_left",
        "window_rear_right",
        "sunroof_open",
        "windscreen_defrost",
        "heated_windscreen",
        "rear_defrost",
        "steering_wheel_heat",
    }
)

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
    *(
        BinarySensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=BinarySensorDeviceClass.WINDOW,
            entity_registry_enabled_default=False,
        )
        for key in (
            "window_front_left",
            "window_front_right",
            "window_rear_left",
            "window_rear_right",
            "sunroof_open",
        )
    ),
    *(
        BinarySensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=BinarySensorDeviceClass.RUNNING,
            entity_registry_enabled_default=False,
            icon=icon,
        )
        for key, icon in (
            ("windscreen_defrost", "mdi:car-defrost-front"),
            ("heated_windscreen", "mdi:car-defrost-front"),
            ("rear_defrost", "mdi:car-defrost-rear"),
            ("steering_wheel_heat", "mdi:steering"),
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
        if key in EXTRA_KEYS:
            return getattr(snapshot, "extras", {}).get(key)
        return snapshot.doors.get(key.removeprefix("door_"))
