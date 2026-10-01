"""Native battery/range/odometer sensors, with honest telemetry freshness."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OmodaJaecooCoordinator

DESCRIPTIONS = (
    SensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SensorEntityDescription(
        key="electric_range",
        translation_key="electric_range",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="odometer",
        translation_key="odometer",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="cabin_temperature",
        translation_key="cabin_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SensorEntityDescription(
        key="command_status",
        translation_key="command_status",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=[
            "not_requested",
            "submitting",
            "accepted_unconfirmed",
            "rejected",
            "unknown_outcome",
            "pin_blocked",
            "not_sent",
        ],
    ),
    SensorEntityDescription(
        key="observed_at",
        translation_key="observed_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="fetched_at",
        translation_key="fetched_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="freshness",
        translation_key="freshness",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        options=["current", "stale", "unknown", "no_data", "unreliable"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: OmodaJaecooCoordinator = entry.runtime_data
    async_add_entities(
        OmodaJaecooSensor(coordinator, vin, description)
        for vin in coordinator.selected_vins
        for description in DESCRIPTIONS
        if description.key != "command_status" or coordinator.controls_enabled
    )


class OmodaJaecooSensor(CoordinatorEntity[OmodaJaecooCoordinator], SensorEntity):
    """An entity property only reads memory; no network requests per sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OmodaJaecooCoordinator,
        vin: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._vin = vin
        self._attr_unique_id = f"eu_{vin}_{description.key}"
        vehicle = coordinator.vehicles[vin]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"eu_{vin}")},
            name=vehicle.name,
            manufacturer="Omoda / Jaecoo",
            model=vehicle.model,
        )

    @property
    def available(self) -> bool:
        if self.entity_description.key == "command_status":
            return True
        return super().available

    @property
    def native_value(self):
        if self.entity_description.key == "command_status":
            return self.coordinator.last_command_status.get(self._vin, "not_requested")
        snapshot = (self.coordinator.data or {}).get(self._vin)
        if snapshot is None:
            return None
        if self.entity_description.key == "freshness":
            return snapshot.freshness()
        return getattr(snapshot, self.entity_description.key)
