"""Synthetic pure-parser tests, not evidence of manufacturer enum meanings."""

import importlib.util
import socket
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "custom_components/omoda_jaecoo/telemetry.py"
SPEC = importlib.util.spec_from_file_location("_offline_jaecoo_telemetry", PATH)
telemetry = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = telemetry
SPEC.loader.exec_module(telemetry)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
ACTIVE = {"chargeGunState": "1", "chargeState": "1"}
BAD_NUMBERS = [
    True,
    False,
    "",
    "unknown",
    "NaN",
    "inf",
    float("nan"),
    float("inf"),
    float("-inf"),
    None,
    [],
    {},
    10**1000,
]


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in telemetry tests")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, blocked)
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)


def charging(data, observed=NOW, now=NOW, unit="unverified"):
    return telemetry.parse_charging(data, observed, now, unit)


@pytest.mark.parametrize(
    "plug,charge,status",
    [
        (0, 0, "unplugged"),
        (1, 0, "plugged_in"),
        (1, 1, "charging"),
        (0, 1, None),
        (0, 2, None),
        (1, 2, None),
        (2, 0, None),
        (255, 255, None),
        (None, 0, None),
        (1, None, None),
        (None, None, None),
    ],
)
def test_only_known_charging_pairs(plug, charge, status):
    result = charging({"chargeGunState": plug, "chargeState": charge})
    assert result.status == status
    assert result.plug_code == plug
    assert result.charge_code == charge


@pytest.mark.parametrize("value", BAD_NUMBERS + [-1, 256, 0.1, "1.5"])
@pytest.mark.parametrize(
    "field",
    [
        "chargeGunState",
        "chargeState",
        "fastChargingGunStatus",
        "appointmentChargeState",
    ],
)
def test_codes_reject_invalid_numbers(field, value):
    names = {
        "chargeGunState": "plug_code",
        "chargeState": "charge_code",
        "fastChargingGunStatus": "fast_code",
        "appointmentChargeState": "schedule_code",
    }
    result = charging({**ACTIVE, field: value})
    assert getattr(result, names[field]) is None
    if field in ("chargeGunState", "chargeState"):
        assert result.status is None


def test_raw_codes_not_used_to_infer_status():
    result = charging({"fastChargingGunStatus": "2", "appointmentChargeState": "2"})
    assert result.status is None
    assert result.fast_code == 2
    assert result.schedule_code == 2
    assert charging({}).remaining_raw is None


@pytest.mark.parametrize("power", [0, "0", "7.2", 1000])
def test_direct_power_only_when_charging(power):
    result = charging({**ACTIVE, "chargingPower": power})
    assert result.power_kw == float(power)
    for frame in (
        {},
        {"chargeGunState": 1, "chargeState": 0},
        {"chargeGunState": 0, "chargeState": 0},
    ):
        assert charging({**frame, "chargingPower": power}).power_kw is None


@pytest.mark.parametrize("power", BAD_NUMBERS + [-0.1, 1000.1])
def test_invalid_power_unknown(power):
    assert charging({**ACTIVE, "chargingPower": power}).power_kw is None


def test_no_power_inference_or_missing_as_zero():
    assert (
        charging({**ACTIVE, "totalVoltage": 400, "totalCurrent": 20}).power_kw is None
    )


@pytest.mark.parametrize("raw", BAD_NUMBERS + [-1, 1_000_001])
def test_invalid_remaining_unknown(raw):
    result = charging({**ACTIVE, "remainChargeTime": raw}, unit="minutes")
    assert result.remaining_raw is None
    assert result.remaining_minutes is None
    assert result.estimated_finish is None


def test_remaining_units_are_explicit_and_charging_only():
    frame = {**ACTIVE, "remainChargeTime": "120"}
    unverified = charging(frame)
    assert unverified.remaining_raw == 120
    assert unverified.remaining_minutes is None
    assert unverified.estimated_finish is None
    assert charging(frame, unit="minutes").remaining_minutes == 120
    assert charging(frame, unit="seconds").remaining_minutes == 2
    assert charging(frame, unit="hours").remaining_minutes is None
    idle = charging({**frame, "chargeState": 0}, unit="minutes")
    assert idle.remaining_raw == 120
    assert idle.remaining_minutes is None
    assert idle.estimated_finish is None


@pytest.mark.parametrize(
    "raw,unit,decoded",
    [
        (10080, "minutes", 10080),
        (10081, "minutes", None),
        (604800, "seconds", 10080),
        (604801, "seconds", None),
        (1_000_000, "unverified", None),
        (0, "minutes", 0),
        (90, "seconds", 1.5),
    ],
)
def test_decoded_duration_at_most_seven_days(raw, unit, decoded):
    result = charging({**ACTIVE, "remainChargeTime": raw}, unit=unit)
    assert result.remaining_raw == raw
    assert result.remaining_minutes == decoded


@pytest.mark.parametrize(
    "age,remaining,finish",
    [
        (0, 30, NOW + timedelta(minutes=30)),
        (15, 30, NOW + timedelta(minutes=15)),
        (15.01, 30, None),
        (-1, 30, None),
        (10, 9, None),
        (10, 10, NOW),
    ],
)
def test_eta_from_fresh_observation_not_fetch_time(age, remaining, finish):
    result = charging(
        {**ACTIVE, "remainChargeTime": remaining},
        observed=NOW - timedelta(minutes=age),
        unit="minutes",
    )
    assert result.estimated_finish == finish


@pytest.mark.parametrize(
    "observed,now",
    [
        (None, NOW),
        (NOW.replace(tzinfo=None), NOW),
        (NOW, NOW.replace(tzinfo=None)),
    ],
)
def test_eta_requires_aware_observation_and_now(observed, now):
    result = charging({**ACTIVE, "remainChargeTime": 30}, observed, now, "minutes")
    assert result.remaining_minutes == 30
    assert result.estimated_finish is None


def test_eta_datetime_overflow_unknown():
    end = datetime.max.replace(tzinfo=timezone.utc)
    assert (
        charging(
            {**ACTIVE, "remainChargeTime": 1}, end, end, "minutes"
        ).estimated_finish
        is None
    )


@pytest.mark.parametrize(
    "coordinates,lat,lon",
    [
        ({"lat": "51.5", "lon": "-0.1"}, 51.5, -0.1),
        ({"latitude": -90, "longitude": 180}, -90, 180),
        ({"lat": 0, "lon": 5}, 0, 5),
        ({"lat": 5, "lon": 0}, 5, 0),
        ({"lat": 90, "lon": -180}, 90, -180),
    ],
)
def test_position_valid_coordinates(coordinates, lat, lon):
    result = telemetry.parse_position(coordinates, NOW)
    assert result.latitude == lat
    assert result.longitude == lon
    assert result.fetched_at == NOW
    assert result.observed_at is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"lat": 0, "lon": 0},
        {"lat": 91, "lon": 1},
        {"lat": 1, "lon": 181},
        {"lat": 51500000, "lon": -100000},
        {"lat": 51},
        {"lon": 1},
        {"lat": 51, "lon": 1, "latitude": 52},
        None,
        [],
    ],
)
def test_invalid_position_never_guessed(data):
    assert telemetry.parse_position(data, NOW) is None


@pytest.mark.parametrize("value", BAD_NUMBERS)
@pytest.mark.parametrize("field", ["lat", "lon"])
def test_coordinates_reject_nonfinite_bool_and_text(field, value):
    assert telemetry.parse_position({"lat": 51, "lon": 1, field: value}, NOW) is None


@pytest.mark.parametrize("field", ["gpsTime", "positionTime"])
@pytest.mark.parametrize(
    "stamp",
    [
        NOW.isoformat(),
        "2026-10-02T12:00:00Z",
        NOW.timestamp(),
        str(NOW.timestamp()),
        NOW.timestamp() * 1000,
        str(NOW.timestamp() * 1000),
    ],
)
def test_position_source_iso_seconds_and_milliseconds(field, stamp):
    result = telemetry.parse_position({"lat": 51, "lon": 1, field: stamp}, NOW)
    assert result.observed_at == NOW


@pytest.mark.parametrize(
    "data",
    [
        {"updateTime": NOW.isoformat(), "timestamp": NOW.timestamp()},
        {"gpsTime": "2026-10-02T12:00:00"},
        {"gpsTime": "invalid"},
        {"gpsTime": True},
        {"positionTime": float("nan")},
        {
            "gpsTime": NOW.isoformat(),
            "positionTime": (NOW - timedelta(minutes=1)).isoformat(),
        },
        {"gpsTime": NOW.isoformat(), "positionTime": None},
    ],
)
def test_position_unknown_or_ambiguous_source_time(data):
    result = telemetry.parse_position({"lat": 51, "lon": 1, **data}, NOW)
    assert result.observed_at is None
    assert result.fetched_at == NOW


def test_position_equal_source_times_and_no_private_fields():
    result = telemetry.parse_position(
        {
            "latitude": 51,
            "longitude": 1,
            "gpsTime": NOW.isoformat(),
            "positionTime": NOW.timestamp(),
            "vin": "synthetic",
            "address": "ignored",
        },
        NOW,
    )
    assert result.observed_at == NOW
    assert set(vars(result)) == {"latitude", "longitude", "observed_at", "fetched_at"}


@pytest.mark.parametrize("stamp", [0, "1900-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_position_bad_fix_time_does_not_invent_known_age(stamp):
    position = telemetry.parse_position({"lat": 51, "lon": 0, "gpsTime": stamp}, NOW)
    assert position is not None
    assert position.observed_at is None


PLAN = {
    "switchStatus": "1",
    "startTime": "60",
    "timeConsuming": "120",
    "cycleData": ["1", 7],
}


def schedule(plans, main="1"):
    return telemetry.parse_schedule({"mainSwitch": main, "chargeAppointPlans": plans})


def test_schedule_multiple_plans_allowlist_only():
    result = schedule(
        [
            {**PLAN, "vin": "synthetic", "chargeAppointId": "ignored"},
            {
                "switchStatus": 0,
                "startTime": 1439,
                "timeConsuming": 10080,
                "cycleData": [1, 2, 3, 4, 5, 6, 7],
            },
        ]
    )
    assert result.enabled is True
    assert result.plans == (
        telemetry.SchedulePlan(True, 60, 120, (1, 7)),
        telemetry.SchedulePlan(False, 1439, 10080, (1, 2, 3, 4, 5, 6, 7)),
    )
    assert set(vars(result.plans[0])) == {
        "enabled",
        "start_minutes",
        "duration_minutes",
        "cycle_codes",
    }


def test_schedule_empty_not_missing_and_no_fabricated_plan():
    assert schedule([], 0) == telemetry.ChargeSchedule(False, ())
    assert schedule([], 1) == telemetry.ChargeSchedule(True, ())
    assert telemetry.parse_schedule({"mainSwitch": 0}) is None
    assert telemetry.parse_schedule({"chargeAppointPlans": []}) is None


@pytest.mark.parametrize("main", BAD_NUMBERS + [2, -1, 0.5])
def test_schedule_main_switch_strict(main):
    assert schedule([], main) is None


@pytest.mark.parametrize("plans", [None, (), {}, "[]", [None], [1], [PLAN] * 33])
def test_schedule_rejects_malformed_shape_and_oversize(plans):
    assert schedule(plans) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("switchStatus", 2),
        ("switchStatus", True),
        ("startTime", -1),
        ("startTime", 1440),
        ("startTime", 1.1),
        ("timeConsuming", 0),
        ("timeConsuming", 10081),
        ("timeConsuming", "inf"),
        ("cycleData", None),
        ("cycleData", (1,)),
        ("cycleData", [0]),
        ("cycleData", [8]),
        ("cycleData", [1.1]),
        ("cycleData", [True]),
        ("cycleData", ["NaN"]),
    ],
)
def test_malformed_plan_invalidates_schedule(field, value):
    assert schedule([PLAN, {**PLAN, field: value}]) is None


def test_schedule_missing_fields_unknown_not_no_repeat():
    assert schedule([{}]).plans == (telemetry.SchedulePlan(None, None, None, None),)
    assert schedule([{"cycleData": []}]).plans == (
        telemetry.SchedulePlan(None, None, None, ()),
    )


def test_schedule_cap_and_raw_cycles_no_weekday_names():
    assert len(schedule([PLAN] * 32).plans) == 32
    assert schedule([{**PLAN, "startTime": 0, "cycleData": [7, 1, 7]}]).plans[
        0
    ].cycle_codes == (7, 1, 7)
    assert schedule([{**PLAN, "cycleData": []}]).plans[0].cycle_codes == ()


@pytest.mark.parametrize("depth", [0, "80", 100.5, 10000])
def test_depth_is_raw_not_percent(depth):
    assert telemetry.parse_depth({"depth": depth}) == float(depth)


@pytest.mark.parametrize("depth", BAD_NUMBERS + [-1, 10001])
def test_depth_rejects_invalid(depth):
    assert telemetry.parse_depth({"depth": depth}) is None
    assert telemetry.parse_depth({}) is None


SWITCHES = {
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
SEATS = {
    "driver_seat": ("dSeat", ""),
    "passenger_seat": ("pSeat", ""),
    "rear_left_seat": ("lSeat", "2"),
    "rear_right_seat": ("rSeat", "2"),
    "rear_centre_seat": ("mSeat", "2"),
}
EXPECTED_KEYS = (
    {"speed", "hv_voltage", "hv_current"}
    | set(SWITCHES)
    | {f"{seat}_{kind}" for seat in SEATS for kind in ("heat", "vent")}
)


def test_extras_stable_all_unknown_without_fields():
    result = telemetry.parse_extras({"unrecognized": 1})
    assert set(result) == EXPECTED_KEYS
    assert len(result) == 22
    assert all(value is None for value in result.values())


@pytest.mark.parametrize("key,source", SWITCHES.items())
@pytest.mark.parametrize(
    "code,decoded",
    [
        (0, False),
        ("0", False),
        (1, True),
        ("1", True),
        (2, None),
        (True, None),
        ("NaN", None),
        (0.5, None),
    ],
)
def test_extra_switches_strict(key, source, code, decoded):
    assert telemetry.parse_extras({source: code})[key] is decoded


@pytest.mark.parametrize("seat,prefix_suffix", SEATS.items())
@pytest.mark.parametrize("kind,source", [("heat", "Heating"), ("vent", "Ventilate")])
@pytest.mark.parametrize(
    "level,expected",
    [
        (0, 0),
        ("10", 10),
        ("3", 3),
        (-1, None),
        (11, None),
        (1.5, None),
        (True, None),
        ("inf", None),
    ],
)
def test_all_seat_levels_remain_raw(seat, prefix_suffix, kind, source, level, expected):
    prefix, suffix = prefix_suffix
    result = telemetry.parse_extras({f"{prefix}{source}State{suffix}": level})
    assert result[f"{seat}_{kind}"] == expected


@pytest.mark.parametrize(
    "source,key,valid,invalid",
    [
        ("vehicleSpeed", "speed", [0, "70.5", 400], [-1, 401]),
        ("totalVoltage", "hv_voltage", [0.1, "400", 1000], [0, -1, 1001]),
        ("totalCurrent", "hv_current", [-2000, -999, "0", 2000], [-2001, -1000, 2001]),
    ],
)
def test_extra_numeric_bounds_and_sentinels(source, key, valid, invalid):
    for value in valid:
        assert telemetry.parse_extras({source: value})[key] == float(value)
    for value in invalid + BAD_NUMBERS:
        assert telemetry.parse_extras({source: value})[key] is None


def test_frozen_models_and_input_untouched():
    frame = {**ACTIVE, "remainChargeTime": "20", "private": {"untouched": True}}
    before = frame.copy()
    result = charging(frame)
    assert frame == before
    objects = [
        result,
        telemetry.parse_position({"lat": 1, "lon": 1}, NOW),
        schedule([PLAN]),
        schedule([PLAN]).plans[0],
    ]
    for obj in objects:
        key = next(iter(vars(obj)))
        with pytest.raises(FrozenInstanceError):
            setattr(obj, key, None)
