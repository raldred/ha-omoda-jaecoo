"""Synthetic normalization cases; never use private vehicle captures in tests."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omoda_jaecoo.coordinator import (
    finite_number,
    normalize_snapshot,
    observation_time,
)

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value", [True, False, None, "", "nan", "inf", "-inf", "-1", "not a number", [], {}]
)
def test_invalid_numbers_are_unknown_not_zero(value):
    assert finite_number(value) is None


@pytest.mark.parametrize("value", [0, "0", "0.0", 0.0])
def test_real_zero_is_preserved(value):
    assert finite_number(value, maximum=100) == 0


def test_battery_limit_and_kilometres_ignore_display_flag():
    result = normalize_snapshot(
        {
            "dumpEnergy": "101",
            "dynamicPureElectricRange": "80",
            "rangeUnit": 0,
            "odometer": "100",
        },
        NOW,
    )
    assert result.battery is None
    assert result.electric_range == 80
    assert result.odometer == 100


def test_zero_range_is_not_overwritten_by_fallback():
    result = normalize_snapshot(
        {"dynamicPureElectricRange": "0", "electricRange": "50"}, NOW
    )
    assert result.electric_range == 0


@pytest.mark.parametrize(
    "value",
    [
        NOW.timestamp(),
        str(NOW.timestamp()),
        NOW.timestamp() * 1000,
        "2026-10-01T12:00:00Z",
        "2026-10-01T14:00:00+02:00",
    ],
)
def test_source_timestamp_formats(value):
    assert observation_time({"resultTime": value}, NOW) == NOW


@pytest.mark.parametrize(
    "value",
    [
        0,
        "2026-10-01 12:00:00",
        "not a date",
        "NaN",
        True,
        "2026-10-02T12:00:00Z",
        "1900-01-01T12:00:00Z",
    ],
)
def test_invalid_ambiguous_or_future_timestamps_are_unknown(value):
    assert observation_time({"time": value}, NOW) is None


def test_timestamp_fallback_does_not_guess_timezone():
    assert (
        observation_time(
            {"time": "2026-10-01 12:00:00", "resultTime": NOW.isoformat()}, NOW
        )
        == NOW
    )


def test_degraded_hv_frame_does_not_publish_fake_zeros():
    previous = normalize_snapshot(
        {"dumpEnergy": "60", "dynamicPureElectricRange": "80", "odometer": "100"}, NOW
    )
    raw = {
        "dumpEnergy": "0",
        "dynamicPureElectricRange": "0",
        "odometer": "0",
        "totalVoltage": "0",
        "totalCurrent": "-1000",
        "time": NOW.isoformat(),
    }
    result = normalize_snapshot(raw, NOW, previous)
    assert (result.battery, result.electric_range, result.odometer) == (60, 80, 100)
    assert result.freshness() == "unreliable"
    first = normalize_snapshot(raw, NOW)
    assert first.battery is None and first.electric_range is None
    assert first.observed_at is None
    assert first.freshness() == "unreliable"


def test_valid_cached_parked_values_are_not_discarded():
    result = normalize_snapshot(
        {
            "dumpEnergy": "60",
            "dynamicPureElectricRange": "80",
            "totalVoltage": "0",
            "totalCurrent": "-1000",
        },
        NOW,
    )
    assert result.battery == 60 and result.electric_range == 80
    assert result.freshness() == "unknown"


def test_fetch_time_does_not_become_observation_time():
    result = normalize_snapshot({"dumpEnergy": "60"}, NOW)
    assert result.fetched_at == NOW
    assert result.observed_at is None
    assert result.freshness() == "unknown"
    empty = normalize_snapshot({}, NOW + timedelta(minutes=5), result)
    assert empty.battery == 60
    assert empty.observed_at is None
    assert empty.freshness() == "no_data"
