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
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHARGE_DEPTH_IS_TARGET,
    CONF_ENABLE_CHARGING_DETAILS,
    DOMAIN,
)
from .coordinator import OmodaJaecooCoordinator

SEAT_KEYS = tuple(
    f"{seat}_seat_{kind}"
    for seat in ("driver", "passenger", "rear_left", "rear_right", "rear_centre")
    for kind in ("heat", "vent")
)
EXTRA_KEYS = frozenset({"speed", "hv_voltage", "hv_current", *SEAT_KEYS})
CHARGING_KEYS = {
    "charging_status": "status",
    "charging_power": "power_kw",
    "charge_time_remaining_raw": "remaining_raw",
    "charge_time_remaining": "remaining_minutes",
    "charging_eta": "estimated_finish",
}

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
            "state_observed",
            "confirmation_timeout",
        ],
    ),
    SensorEntityDescription(
        key="charging_status",
        translation_key="charging_status",
        device_class=SensorDeviceClass.ENUM,
        options=["unplugged", "plugged_in", "charging"],
        icon="mdi:ev-station",
    ),
    SensorEntityDescription(
        key="charging_power",
        translation_key="charging_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="charge_time_remaining_raw",
        translation_key="charge_time_remaining_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:timer-outline",
    ),
    SensorEntityDescription(
        key="charge_time_remaining",
        translation_key="charge_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="charging_eta",
        translation_key="charging_eta",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="charging_depth_raw",
        translation_key="charging_depth_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="charge_target",
        translation_key="charge_target",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        icon="mdi:battery-lock",
    ),
    SensorEntityDescription(
        key="charge_schedule",
        translation_key="charge_schedule",
        device_class=SensorDeviceClass.ENUM,
        options=["enabled", "disabled"],
        icon="mdi:calendar-clock",
    ),
    SensorEntityDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    SensorEntityDescription(
        key="hv_voltage",
        translation_key="hv_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="hv_current",
        translation_key="hv_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    *(
        SensorEntityDescription(
            key=key,
            translation_key=key,
            suggested_display_precision=0,
            entity_registry_enabled_default=False,
            icon="mdi:car-seat-heater"
            if key.endswith("heat")
            else "mdi:car-seat-cooler",
        )
        for key in SEAT_KEYS
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
        if (description.key != "command_status" or coordinator.controls_enabled)
        and (
            description.key not in ("charge_schedule", "charging_depth_raw")
            or coordinator.options.get(CONF_ENABLE_CHARGING_DETAILS, False)
        )
        and (
            description.key != "charge_target"
            or (
                coordinator.options.get(CONF_ENABLE_CHARGING_DETAILS, False)
                and coordinator.options.get(CONF_CHARGE_DEPTH_IS_TARGET, False)
            )
        )
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
        key = self.entity_description.key
        if key == "charge_schedule":
            schedule = self.coordinator.schedules.get(self._vin)
            return ("enabled" if schedule.enabled else "disabled") if schedule else None
        if key in ("charging_depth_raw", "charge_target"):
            depth = self.coordinator.charge_depths.get(self._vin)
            return (
                depth
                if key == "charging_depth_raw" or depth is None or 0 <= depth <= 100
                else None
            )
        snapshot = (self.coordinator.data or {}).get(self._vin)
        if snapshot is None:
            return None
        if key == "freshness":
            return snapshot.freshness()
        if key in EXTRA_KEYS:
            return snapshot.extras.get(key)
        if key in CHARGING_KEYS:
            return (
                getattr(snapshot.charging, CHARGING_KEYS[key])
                if snapshot.charging
                else None
            )
        return getattr(snapshot, key)

    @property
    def extra_state_attributes(self):
        key = self.entity_description.key
        if key == "charging_eta":
            return {
                "estimate_basis": "vehicle sample plus reported remaining minutes",
                "assumes_continuous_charging": True,
            }
        if key == "charging_status":
            snapshot = (self.coordinator.data or {}).get(self._vin)
            charging = snapshot.charging if snapshot else None
            if charging:
                return {
                    "plug_code": charging.plug_code,
                    "charge_code": charging.charge_code,
                    "fast_connector_code": charging.fast_code,
                    "schedule_code": charging.schedule_code,
                }
        if key == "charge_schedule":
            schedule = self.coordinator.schedules.get(self._vin)
            if schedule:
                return {
                    "time_basis": "reported local time; timezone unverified",
                    "plan_count": len(schedule.plans),
                    "plans": [
                        {
                            "enabled": plan.enabled,
                            "start_time": f"{plan.start_minutes // 60:02d}:{plan.start_minutes % 60:02d}"
                            if plan.start_minutes is not None
                            else None,
                            "duration_minutes": plan.duration_minutes,
                            "repeat_day_codes": list(plan.cycle_codes)
                            if plan.cycle_codes is not None
                            else None,
                        }
                        for plan in schedule.plans
                    ],
                }
        return None
