"""Allowlisted diagnostics: no account, VIN, tokens, PIN, GPS or raw responses."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONTROL_PIN,
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
        "read_only": True,
        "vehicle_count": len(entry.data[CONF_SELECTED_VINS]),
        "poll_interval_minutes": entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        ),
        "control_pin_stored": bool(entry.data.get(CONF_CONTROL_PIN)),
        "control_pin_verified": False,
        "last_update_success": coordinator.last_update_success if coordinator else None,
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
