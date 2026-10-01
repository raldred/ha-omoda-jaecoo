"""Passive polling plus separately gated, explicit user-requested controls."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiError, AuthenticationError, JaecooApi, RateLimitError, Vehicle
from .commands import (
    CommandCancelled,
    CommandClient,
    CommandError,
    CommandOutcomeUnknown,
    PinVerificationCancelled,
    PinVerificationError,
)
from .const import (
    COMMAND_COOLDOWN_SECONDS,
    CONF_CLIMATE_DURATION,
    CONF_CONTROL_PIN,
    CONF_ENABLE_CONTROLS,
    CONF_PIN_BLOCKED,
    CONF_POLL_INTERVAL,
    CONF_SELECTED_VINS,
    DEFAULT_CLIMATE_DURATION,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    STALE_AFTER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

# Extra passive reads after a lock request, not command retries. Each delay follows
# the previous read; the overall deadline also bounds slow network responses.
LOCK_STATUS_DELAYS = (1, 4, 5, 10, 15)
LOCK_STATUS_TIMEOUT = 60


@dataclass
class LockRequest:
    target: bool
    prior_locked: bool | None
    started_at: datetime
    accepted: bool = False
    waiting: bool = True
    state_observed: bool = False
    report_known: bool = False
    outcome_unknown: bool = False


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
    door_locked: bool | None = None
    climate_on: bool | None = None
    cabin_temperature: float | None = None
    target_temperature: float | None = None
    doors: dict[str, bool | None] = field(default_factory=dict)

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


def finite_number(
    value: Any, *, minimum: float = 0, maximum: float | None = None
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if (
        not math.isfinite(number)
        or number < minimum
        or (maximum is not None and number > maximum)
    ):
        return None
    return number


def binary_state(value: Any) -> bool | None:
    """Only documented 0/1 states; never treat sentinel/arbitrary strings as ON."""
    parsed = finite_number(value)
    return bool(parsed) if parsed in (0.0, 1.0) else None


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
    unlocked = binary_state(data.get("doorLock"))
    target = finite_number(data.get("frontSetTempLeft"), minimum=10, maximum=40)
    if target is None:
        target = finite_number(data.get("frontSetTempRight"), minimum=10, maximum=40)
    return VehicleSnapshot(
        battery=round(battery, 1) if battery is not None else None,
        electric_range=range_value,
        odometer=finite_number(data.get("odometer")),
        observed_at=observation_time(data, now),
        fetched_at=now,
        has_payload=True,
        door_locked=None if unlocked is None else not unlocked,
        climate_on=binary_state(data.get("frontHVACState")),
        cabin_temperature=finite_number(
            data.get("inCarTemperature"), minimum=-60, maximum=80
        ),
        target_temperature=target,
        doors={
            key: binary_state(data.get(source))
            for key, source in {
                "front_left": "frontLeftDoor",
                "front_right": "frontRightDoor",
                "rear_left": "backLeftDoor",
                "rear_right": "backRightDoor",
                "boot": "trunkDoor",
            }.items()
        },
    )


class OmodaJaecooCoordinator(DataUpdateCoordinator[dict[str, VehicleSnapshot]]):
    """One coordinator and auth session per account, not per sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: JaecooApi,
        command_client: CommandClient | None = None,
    ) -> None:
        self.api = api
        self.entry = entry
        self.command_client = command_client
        self._command_lock = asyncio.Lock()
        self._last_command_at: float | None = None
        self.last_command_status: dict[str, str] = {}
        self._last_command_kind: dict[str, str] = {}
        self._lock_requests: dict[str, LockRequest] = {}
        self._lock_followup_tasks: dict[str, asyncio.Task] = {}
        self._climate_followup_tasks: dict[str, asyncio.Task] = {}
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
        self._reconcile_lock_reports(result)
        return result

    def pending_lock_target(self, vin: str) -> bool | None:
        request = self._lock_requests.get(vin)
        return request.target if request is not None and request.waiting else None

    def lock_state_uncertain(self, vin: str) -> bool:
        request = self._lock_requests.get(vin)
        return bool(
            request
            and (request.accepted or request.outcome_unknown)
            and not request.report_known
        )

    def _reconcile_lock_reports(self, snapshots: dict[str, VehicleSnapshot]) -> None:
        for vin, request in self._lock_requests.items():
            snapshot = snapshots.get(vin)
            if not (request.accepted or request.outcome_unknown):
                continue
            if (
                snapshot is None
                or not snapshot.has_payload
                or snapshot.degraded
                or snapshot.door_locked is None
            ):
                if not request.state_observed:
                    request.report_known = False
                continue
            if snapshot.observed_at is not None:
                credible = (
                    request.started_at <= snapshot.observed_at <= snapshot.fetched_at
                )
            else:
                # Without a usable source time, a changed known state is evidence of
                # a new report; an unchanged cached target or prior unknown isn't.
                credible = (
                    request.prior_locked is not None
                    and snapshot.door_locked is not request.prior_locked
                )
            if not credible:
                if not request.state_observed:
                    request.report_known = False
                continue
            request.report_known = True
            if snapshot.door_locked is request.target:
                request.state_observed = True
                request.waiting = False
                if self._last_command_kind.get(vin) == "lock":
                    self.last_command_status[vin] = "state_observed"

    def _start_lock_followup(self, vin: str, request: LockRequest) -> None:
        # HA owns/cancels this task on entry unload. Superseded tasks notice their
        # request identity changed and exit without clobbering the new operation.
        task = self.entry.async_create_background_task(
            self.hass,
            self._async_follow_lock(vin, request),
            "Omoda / Jaecoo lock status checks",
            eager_start=False,
        )
        self._lock_followup_tasks[vin] = task
        # A task cancelled before its first execution never reaches its finally block.
        task.add_done_callback(
            lambda done: self._finish_lock_followup(vin, request, done)
        )

    def _finish_lock_followup(
        self, vin: str, request: LockRequest, task: asyncio.Task | None
    ) -> None:
        if self._lock_followup_tasks.get(vin) is task:
            self._lock_followup_tasks.pop(vin, None)
        if self._lock_requests.get(vin) is not request:
            return
        changed = request.waiting
        request.waiting = False
        if not request.state_observed and self._last_command_kind.get(vin) == "lock":
            changed |= self.last_command_status.get(vin) != "confirmation_timeout"
            self.last_command_status[vin] = "confirmation_timeout"
        if changed:
            self.async_update_listeners()

    async def _async_follow_lock(self, vin: str, request: LockRequest) -> None:
        try:
            async with asyncio.timeout(LOCK_STATUS_TIMEOUT):
                for delay in LOCK_STATUS_DELAYS:
                    await asyncio.sleep(delay)
                    if (
                        self._lock_requests.get(vin) is not request
                        or request.state_observed
                    ):
                        return
                    # Public coordinator refresh serializes with normal polling and
                    # handles auth failures/backoff. It reads selected cars only.
                    await self.async_refresh()
                    if (
                        self._lock_requests.get(vin) is not request
                        or request.state_observed
                        or not self.last_update_success
                    ):
                        return
        except TimeoutError:
            pass
        finally:
            self._finish_lock_followup(vin, request, asyncio.current_task())

    def _start_climate_followup(self, vin: str) -> None:
        # Climate has no native pending mode: just refresh reported values promptly.
        # It keeps accepted_unconfirmed because we cannot correlate a terminal ACK.
        task = self.entry.async_create_background_task(
            self.hass,
            self._async_follow_climate(vin),
            "Omoda / Jaecoo climate status checks",
            eager_start=False,
        )
        self._climate_followup_tasks[vin] = task
        task.add_done_callback(lambda done: self._finish_climate_followup(vin, done))

    def _finish_climate_followup(self, vin: str, task: asyncio.Task | None) -> None:
        if self._climate_followup_tasks.get(vin) is task:
            self._climate_followup_tasks.pop(vin, None)

    async def _async_follow_climate(self, vin: str) -> None:
        try:
            async with asyncio.timeout(LOCK_STATUS_TIMEOUT):
                for delay in LOCK_STATUS_DELAYS:
                    await asyncio.sleep(delay)
                    if (
                        self._climate_followup_tasks.get(vin)
                        is not asyncio.current_task()
                    ):
                        return
                    await self.async_refresh()
                    if not self.last_update_success:
                        return
        except TimeoutError:
            pass
        finally:
            self._finish_climate_followup(vin, asyncio.current_task())

    @property
    def controls_enabled(self) -> bool:
        return bool(self.entry.options.get(CONF_ENABLE_CONTROLS, False))

    @property
    def controls_available(self) -> bool:
        return (
            self.controls_enabled
            and bool(self.entry.data.get(CONF_CONTROL_PIN))
            and not self.entry.data.get(CONF_PIN_BLOCKED, False)
        )

    def _block_pin(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_PIN_BLOCKED: True}
        )

    async def async_lock(self, vin: str, locked: bool) -> None:
        await self._async_command(vin, locked=locked)

    async def async_climate(self, vin: str, on: bool, temperature: float) -> None:
        await self._async_command(vin, on=on, temperature=temperature)

    async def _async_command(
        self,
        vin: str,
        *,
        locked: bool | None = None,
        on: bool | None = None,
        temperature: float | None = None,
    ) -> None:
        if not self.controls_enabled:
            raise ServiceValidationError(
                "Enable remote controls in the integration options first."
            )
        if self.entry.data.get(CONF_PIN_BLOCKED):
            raise ServiceValidationError(
                "Controls are paused after an unsuccessful or inconclusive PIN check. Verify the PIN in the app, then re-enter it using Reconfigure."
            )
        pin = self.entry.data.get(CONF_CONTROL_PIN)
        if not pin:
            raise ServiceValidationError(
                "Save the vehicle control PIN using Reconfigure first."
            )
        if vin not in self.selected_vins or vin not in self.vehicles:
            raise ServiceValidationError("Vehicle is not selected or authorized.")
        if not self.last_update_success or self.command_client is None:
            raise HomeAssistantError(
                "Vehicle cloud connection is unavailable. No command was sent."
            )
        if self._command_lock.locked():
            raise HomeAssistantError(
                "Another vehicle command is in progress. No command was sent."
            )
        if (
            self._last_command_at is not None
            and time.monotonic() - self._last_command_at < COMMAND_COOLDOWN_SECONDS
        ):
            raise HomeAssistantError(
                "Wait at least 30 seconds between commands. Do not retry an unconfirmed action blindly."
            )
        async with self._command_lock:
            self._last_command_at = time.monotonic()
            self._last_command_kind[vin] = "lock" if locked is not None else "climate"
            # Supersede older climate checks without cancelling an in-flight shared
            # coordinator read (which could spuriously mark all entities unavailable).
            self._climate_followup_tasks.pop(vin, None)
            lock_request = None
            if locked is not None:
                snapshot = (self.data or {}).get(vin)
                lock_request = LockRequest(
                    target=locked,
                    prior_locked=snapshot.door_locked if snapshot else None,
                    started_at=dt_util.utcnow(),
                )
                self._lock_requests[vin] = lock_request
            self.last_command_status[vin] = "submitting"
            self.async_update_listeners()
            try:
                if locked is not None:
                    await self.command_client.async_lock(vin, pin, locked)
                else:
                    await self.command_client.async_climate(
                        vin,
                        pin,
                        on,
                        temperature,
                        self.entry.options.get(
                            CONF_CLIMATE_DURATION, DEFAULT_CLIMATE_DURATION
                        ),
                    )
            except PinVerificationCancelled:
                self._block_pin()
                self.last_command_status[vin] = "pin_blocked"
                raise
            except CommandCancelled:
                self.last_command_status[vin] = "unknown_outcome"
                if lock_request is not None:
                    lock_request.outcome_unknown = True
                    lock_request.waiting = False
                raise
            except PinVerificationError as err:
                self._block_pin()
                self.last_command_status[vin] = "pin_blocked"
                raise HomeAssistantError(
                    "PIN verification was unsuccessful or inconclusive. No physical command was sent. Controls are paused until you check and re-enter the PIN using Reconfigure."
                ) from err
            except CommandOutcomeUnknown as err:
                self.last_command_status[vin] = "unknown_outcome"
                if lock_request is not None:
                    lock_request.outcome_unknown = True
                    lock_request.waiting = False
                raise HomeAssistantError(
                    "The command outcome is unknown; the car may have acted. Check the vehicle before retrying."
                ) from err
            except (CommandError, ApiError) as err:
                self.last_command_status[vin] = "rejected"
                raise HomeAssistantError(str(err)) from err
            except asyncio.CancelledError:
                self.last_command_status[vin] = "not_sent"
                raise
            except Exception:  # noqa: BLE001 — command boundary must fail safely without secrets.
                # Unexpected client failures must not leave a permanent submitting state
                # or expose request details. Never assume the vehicle did not act.
                self.last_command_status[vin] = "unknown_outcome"
                if lock_request is not None:
                    lock_request.outcome_unknown = True
                    lock_request.waiting = False
                raise HomeAssistantError(
                    "An unexpected command error occurred; outcome is unknown. Check the vehicle before retrying."
                ) from None
            else:
                self.last_command_status[vin] = "accepted_unconfirmed"
                if lock_request is not None:
                    lock_request.accepted = True
                    self._start_lock_followup(vin, lock_request)
                else:
                    self._start_climate_followup(vin)
            finally:
                self._last_command_at = time.monotonic()
                if (
                    lock_request is not None
                    and not lock_request.accepted
                    and not lock_request.outcome_unknown
                    and self._lock_requests.get(vin) is lock_request
                ):
                    self._lock_requests.pop(vin, None)
                self.async_update_listeners()
        # Feedback continues in bounded background reads, allowing the service to
        # return promptly after acceptance. Neither path retries the physical command.
