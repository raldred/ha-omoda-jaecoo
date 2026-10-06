"""Pure, conservative normalization of read-only cloud telemetry.

Known EU app field contracts are normalized; unsupported values stay unknown.
No network, Home Assistant, account, or vehicle-control dependencies live here.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class ChargingData:
    status: str | None
    plug_code: int | None
    charge_code: int | None
    fast_code: int | None
    schedule_code: int | None
    power_kw: float | None
    remaining_raw: float | None
    remaining_minutes: float | None
    estimated_finish: datetime | None


@dataclass(frozen=True)
class Position:
    latitude: float
    longitude: float
    observed_at: datetime | None
    fetched_at: datetime


@dataclass(frozen=True)
class SchedulePlan:
    enabled: bool | None
    start_minutes: int | None
    duration_minutes: int | None
    cycle_codes: tuple[int, ...] | None


@dataclass(frozen=True)
class ChargeSchedule:
    enabled: bool
    plans: tuple[SchedulePlan, ...]


def _number(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    number = _number(value, minimum, maximum)
    return int(number) if number is not None and number.is_integer() else None


def _switch(value: object) -> bool | None:
    code = _integer(value, 0, 1)
    return bool(code) if code is not None else None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def parse_charging(
    data: dict,
    observed_at: datetime | None,
    now: datetime,
) -> ChargingData:
    """Decode the EU app's charge states and minute-based remaining duration."""
    if not isinstance(data, dict):
        data = {}
    plug = _integer(data.get("chargeGunState"), 0, 255)
    charge = _integer(data.get("chargeState"), 0, 255)
    fast = _integer(data.get("fastChargingGunStatus"), 0, 255)
    schedule = _integer(data.get("appointmentChargeState"), 0, 255)
    # Official app: isChargingGunConnected is chargeGunState==1 OR
    # fastChargingGunStatus==1; isCharging==1 and isChargingComplete==2.
    status = None
    if plug == 1 or fast == 1:
        if charge == 1:
            status = "charging"
        elif charge in (0, 2):
            status = "plugged_in"
    elif plug == 0 and fast in (0, None) and charge in (0, 2):
        status = "unplugged"
    power = (
        _number(data.get("chargingPower"), 0, 1000) if status == "charging" else None
    )
    raw = _number(data.get("remainChargeTime"), 0, 1_000_000)
    minutes = None
    # Proven in the supplied EU app: JSON remainChargeTime passes unchanged
    # to RemainingChargingTimeWidget, which renders n//60 hours and n%60 min.
    if status == "charging" and raw is not None and raw <= 7 * 24 * 60:
        minutes = raw
    finish = None
    if minutes is not None and _aware(observed_at) and _aware(now):
        age = now - observed_at
        if timedelta(0) <= age <= timedelta(minutes=15):
            try:
                candidate = observed_at + timedelta(minutes=minutes)
            except OverflowError:
                candidate = None
            if candidate is not None and candidate >= now:
                finish = candidate
    return ChargingData(
        status, plug, charge, fast, schedule, power, raw, minutes, finish
    )


def _source_time(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return stamp if _aware(stamp) else None
    number = _number(value, -100_000_000_000_000, 100_000_000_000_000)
    if number is None:
        return None
    # Contemporary millisecond epochs are distinguishable from second epochs.
    if abs(number) >= 100_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _coordinate(data: dict, short: str, long: str, bound: int) -> float | None:
    values = [_number(data[key], -bound, bound) for key in (short, long) if key in data]
    if not values or any(value is None for value in values):
        return None
    return values[0] if all(value == values[0] for value in values) else None


def parse_position(data: dict, now: datetime) -> Position | None:
    """Accept only explicit coordinates; generic vehicle times are not GPS fixes."""
    if not isinstance(data, dict):
        return None
    latitude = _coordinate(data, "lat", "latitude", 90)
    longitude = _coordinate(data, "lon", "longitude", 180)
    if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
        return None
    times = [
        _source_time(data[key]) for key in ("gpsTime", "positionTime") if key in data
    ]
    observed = None
    if times and all(stamp is not None and stamp == times[0] for stamp in times):
        observed = times[0]
    if observed is not None and (
        not _aware(now)
        or observed < datetime(2000, 1, 1, tzinfo=timezone.utc)
        or observed > now + timedelta(minutes=5)
    ):
        observed = None
    return Position(latitude, longitude, observed, now)


def parse_schedule(data: dict) -> ChargeSchedule | None:
    """Normalize bounded plans without inferring weekdays, zones, or defaults."""
    if not isinstance(data, dict):
        return None
    enabled = _switch(data.get("mainSwitch"))
    plans = data.get("chargeAppointPlans")
    if enabled is None or not isinstance(plans, list) or len(plans) > 32:
        return None
    normalized = []
    for plan in plans:
        if not isinstance(plan, dict):
            return None
        switch = _switch(plan.get("switchStatus"))
        start = _integer(plan.get("startTime"), 0, 1439)
        duration = _integer(plan.get("timeConsuming"), 1, 10080)
        if any(
            key in plan and value is None
            for key, value in (
                ("switchStatus", switch),
                ("startTime", start),
                ("timeConsuming", duration),
            )
        ):
            return None
        codes = None
        if "cycleData" in plan:
            cycles = plan["cycleData"]
            if not isinstance(cycles, list) or len(cycles) > 7:
                return None
            codes = tuple(_integer(code, 1, 7) for code in cycles)
            if any(code is None for code in codes):
                return None
        normalized.append(SchedulePlan(switch, start, duration, codes))
    return ChargeSchedule(enabled, tuple(normalized))


def parse_depth(data: dict) -> float | None:
    """Return raw depth only; no percent or target-SoC claim is established."""
    return _number(data.get("depth"), 0, 10000) if isinstance(data, dict) else None


_SWITCH_FIELDS = {
    "window_front_left": "frontLeftWindowState",
    "window_front_right": "frontRightWindowState",
    "window_rear_left": "backLeftWindowState",
    "window_rear_right": "backRightWindowState",
    "sunroof_open": "sunroofState",
    "windscreen_defrost": "fWinHeatingState",
    "heated_windscreen": "frontWindshieldHeat",
    "rear_defrost": "rWinHeatingState",
    "steering_wheel_heat": "steerWheelHeating",
}
_SEAT_FIELDS = {
    "driver_seat": ("dSeat", ""),
    "passenger_seat": ("pSeat", ""),
    "rear_left_seat": ("lSeat", "2"),
    "rear_right_seat": ("rSeat", "2"),
    "rear_centre_seat": ("mSeat", "2"),
}


def parse_extras(data: dict) -> dict[str, float | bool | None]:
    """Return a stable allowlist of passive readings; presence is not capability."""
    if not isinstance(data, dict):
        data = {}
    voltage = _number(data.get("totalVoltage"), 0, 1000)
    current = _number(data.get("totalCurrent"), -2000, 2000)
    values = {
        "speed": _number(data.get("vehicleSpeed"), 0, 400),
        "hv_voltage": voltage if voltage != 0 else None,
        "hv_current": current if current != -1000 else None,
    }
    values.update(
        {key: _switch(data.get(source)) for key, source in _SWITCH_FIELDS.items()}
    )
    for key, (prefix, suffix) in _SEAT_FIELDS.items():
        for kind, source in (("heat", "Heating"), ("vent", "Ventilate")):
            level = _integer(data.get(f"{prefix}{source}State{suffix}"), 0, 10)
            values[f"{key}_{kind}"] = float(level) if level is not None else None
    return values
