"""Allowlisted diagnostics: no account, VIN, tokens, PIN, GPS or raw responses."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROL_PIN,
    CONF_ENABLE_CHARGING_DETAILS,
    CONF_ENABLE_CONTROLS,
    CONF_ENABLE_LOCATION,
    CONF_PIN_BLOCKED,
    CONF_POLL_INTERVAL,
    CONF_SELECTED_VINS,
    DEFAULT_POLL_INTERVAL,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    coordinator = getattr(entry, "runtime_data", None)
    snapshots = (coordinator.data or {}) if coordinator else {}
    return {
        "backend": "EU Omoda / Jaecoo",
        "read_only": not bool(entry.options.get(CONF_ENABLE_CONTROLS, False)),
        "controls_blocked": bool(entry.data.get(CONF_PIN_BLOCKED, False)),
        "command_confirmation": "REST acceptance only; no terminal acknowledgement",
        "vehicle_count": len(entry.data[CONF_SELECTED_VINS]),
        "poll_interval_minutes": entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        ),
        "control_pin_stored": bool(entry.data.get(CONF_CONTROL_PIN)),
        "control_pin_verified": False,
        "location_enabled": bool(entry.options.get(CONF_ENABLE_LOCATION, False)),
        "charging_details_enabled": bool(
            entry.options.get(CONF_ENABLE_CHARGING_DETAILS, False)
        ),
        "charge_time_unit": "minutes",
        "optional_query_errors": sorted(set(coordinator._optional_errors.values()))
        if coordinator
        else [],
        "last_update_success": coordinator.last_update_success if coordinator else None,
        "location_report_count": sum(
            value is not None for value in coordinator.positions.values()
        )
        if coordinator
        else 0,
        "schedule_report_count": sum(
            value is not None for value in coordinator.schedules.values()
        )
        if coordinator
        else 0,
        "charging_depth_report_count": sum(
            value is not None for value in coordinator.charge_depths.values()
        )
        if coordinator
        else 0,
        "vehicles": [
            {
                "battery_present": snapshot.battery is not None,
                "electric_range_present": snapshot.electric_range is not None,
                "odometer_present": snapshot.odometer is not None,
                "observation_time_known": snapshot.observed_at is not None,
                "freshness": snapshot.freshness(),
            }
            for snapshot in snapshots.values()
        ],
    }
