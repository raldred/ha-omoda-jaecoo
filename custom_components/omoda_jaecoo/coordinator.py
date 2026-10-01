"""Passive account-level telemetry polling. No vehicle wake or control calls."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthenticationError, JaecooApi, RateLimitError, Vehicle
from .const import (
    CONF_POLL_INTERVAL,
    CONF_SELECTED_VINS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    STALE_AFTER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class VehicleSnapshot:
    """Normalized units with separate observation and fetch times."""

    battery: float | None
    electric_range: float | None  # km, never inferred from the app display-unit flag
    odometer: float | None  # km
    observed_at: datetime | None
    fetched_at: datetime
    has_payload: bool
    degraded: bool = False

    def freshness(self) -> str:
        if self.degraded:
            return "unreliable"
        if not self.has_payload:
            return "no_data"
        if self.observed_at is None:
            return "unknown"
        return (
            "stale"
            if (dt_util.utcnow() - self.observed_at).total_seconds()
            > STALE_AFTER_SECONDS
            else "current"
        )


def finite_number(value: Any, *, maximum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if (
        not math.isfinite(number)
        or number < 0
        or (maximum is not None and number > maximum)
    ):
        return None
    return number


def observation_time(data: dict[str, Any], now: datetime) -> datetime | None:
    """Accept epoch seconds/ms or explicit-zone ISO dates, never guess a timezone."""
    for key in (
        "time",
        "resultTime",
        "reportTime",
        "collectTime",
        "timestamp",
        "updateTime",
    ):
        value = data.get(key)
        if value is None or isinstance(value, bool):
            continue
        result = None
        try:
            number = float(value)
            if math.isfinite(number):
                if number >= 1e12:
                    number /= 1000
                result = datetime.fromtimestamp(number, timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            if isinstance(value, str):
                try:
                    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    pass
        if result is None or result.tzinfo is None or result.utcoffset() is None:
            continue
        result = result.astimezone(timezone.utc)
        # Reject counters, implausible epochs and clock-skewed future frames.
        if (
            datetime(2000, 1, 1, tzinfo=timezone.utc)
            <= result
            <= now + timedelta(minutes=5)
        ):
            return result
    return None


def normalize_snapshot(
    data: dict[str, Any], now: datetime, previous: VehicleSnapshot | None = None
) -> VehicleSnapshot:
    """Retain last readings on empty/asleep replies, without relabelling them fresh."""
    if not data:
        return VehicleSnapshot(
            previous.battery if previous else None,
            previous.electric_range if previous else None,
            previous.odometer if previous else None,
            previous.observed_at if previous else None,
            now,
            False,
        )
    range_value = None
    for key in ("dynamicPureElectricRange", "electricRange", "pureElectricRange"):
        range_value = finite_number(data.get(key))
        if range_value is not None:
            break
    battery = finite_number(data.get("dumpEnergy"), maximum=100)
    # A known HV-off placeholder pattern. Never reject zero solely because it is zero,
    # or reject all parked frames: valid cached battery/range remain useful.
    try:
        placeholder_current = float(data.get("totalCurrent", "nan")) == -1000
    except (TypeError, ValueError, OverflowError):
        placeholder_current = False
    if (
        finite_number(data.get("totalVoltage")) == 0
        and placeholder_current
        and (battery == 0 or range_value == 0)
    ):
        return VehicleSnapshot(
            previous.battery if previous else None,
            previous.electric_range if previous else None,
            previous.odometer if previous else None,
            previous.observed_at if previous else None,
            now,
            True,
            degraded=True,
        )
    return VehicleSnapshot(
        battery=round(battery, 1) if battery is not None else None,
        electric_range=range_value,
        odometer=finite_number(data.get("odometer")),
        observed_at=observation_time(data, now),
        fetched_at=now,
        has_payload=True,
    )


class OmodaJaecooCoordinator(DataUpdateCoordinator[dict[str, VehicleSnapshot]]):
    """One coordinator and auth session per account, not per sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: JaecooApi) -> None:
        self.api = api
        self.options = dict(entry.options)
        self.selected_vins = list(entry.data[CONF_SELECTED_VINS])
        self.vehicles: dict[str, Vehicle] = {}
        self.poll_interval = timedelta(
            minutes=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self.poll_interval,
        )

    async def _async_setup(self) -> None:
        try:
            vehicles = await self.api.async_list_vehicles()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Account session expired; sign in again."
            ) from err
        except ApiError as err:
            raise UpdateFailed("Unable to discover vehicles from the cloud.") from err
        self.vehicles = {vehicle.vin: vehicle for vehicle in vehicles}
        if not self.selected_vins or not set(self.selected_vins).issubset(
            self.vehicles
        ):
            raise ConfigEntryAuthFailed(
                "Vehicle access changed; verify your account authorization."
            )

    async def _async_update_data(self) -> dict[str, VehicleSnapshot]:
        result = {}
        previous = self.data or {}
        try:
            for vin in self.selected_vins:
                raw = await self.api.async_realtime(vin)
                result[vin] = normalize_snapshot(
                    raw, dt_util.utcnow(), previous.get(vin)
                )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Account session expired; sign in again."
            ) from err
        except RateLimitError as err:
            current = self.update_interval or self.poll_interval
            self.update_interval = min(current * 2, timedelta(hours=1))
            raise UpdateFailed(
                "Cloud rate limit reached; polling slowed down."
            ) from err
        except ApiError as err:
            raise UpdateFailed(
                "Unable to read vehicle telemetry from the cloud."
            ) from err
        self.update_interval = self.poll_interval
        return result
