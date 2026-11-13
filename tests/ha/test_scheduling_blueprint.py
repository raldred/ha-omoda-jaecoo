"""The shipped scheduling blueprint, loaded and run by real Home Assistant.

Every vehicle action is a captured mock call: nothing is sent to a car, no
schedule is enabled anywhere, and the automation only ever runs when a test
triggers it explicitly.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util import yaml as yaml_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

DOMAIN = "omoda_jaecoo"
BLUEPRINT = "scheduled_conditioning.yaml"
SOURCE = (
    Path(__file__).parents[2] / "blueprints/automation/omoda_jaecoo" / BLUEPRINT
)
CLIMATE = "climate.synthetic_vehicle"
DEFROST = "switch.synthetic_vehicle_rear_window_defrost"


@pytest.fixture
def blueprint():
    return yaml_util.load_yaml(SOURCE)


@pytest.fixture(autouse=True)
async def disarm_schedule(hass):
    """Leave no armed schedule and no installed blueprint behind.

    The harness' configuration folder lives inside the test environment, so the
    imported copy is removed again rather than left where a later run or a
    packaging check would see it as repository content.
    """
    yield
    if hass.states.get("automation.test_schedule") is not None:
        await hass.services.async_call(
            "automation",
            "turn_off",
            {"entity_id": "automation.test_schedule"},
            blocking=True,
        )
    installed = Path(hass.config.path("blueprints/automation/omoda_jaecoo"))
    (installed / BLUEPRINT).unlink(missing_ok=True)
    root = Path(hass.config.path("blueprints"))
    for folder in (installed, installed.parent, root):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


@pytest.fixture
def calls(hass):
    """Capture every action the blueprint can take, so none can reach a vehicle."""
    return {
        "rapid_heat": async_mock_service(hass, DOMAIN, "rapid_heat"),
        "set_temperature": async_mock_service(hass, "climate", "set_temperature"),
        "turn_on": async_mock_service(hass, "climate", "turn_on"),
        "defrost": async_mock_service(hass, "switch", "turn_on"),
    }


async def create(hass, **inputs):
    """Install the blueprint the way a user imports it, then build one automation."""
    folder = Path(hass.config.path("blueprints/automation/omoda_jaecoo"))
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE, folder / BLUEPRINT)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": "Test schedule",
                "use_blueprint": {
                    "path": f"omoda_jaecoo/{BLUEPRINT}",
                    "input": {
                        "start_time": "06:40:00",
                        "climate_entity": CLIMATE,
                        **inputs,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.test_schedule") is not None


async def start(hass):
    """Trigger the automation and let it run its own conditions and actions.

    Nothing is awaited to completion here: a pending cooldown delay must stay
    pending so tests can prove the two requests are not sent together.
    """
    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": "automation.test_schedule", "skip_condition": False},
        blocking=False,
    )
    for _ in range(50):
        await asyncio.sleep(0)


async def fire(hass):
    """Run the automation to completion, skipping any pending delay."""
    await start(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done()


def test_blueprint_is_generic_and_opt_in(blueprint):
    """No personal entity, time, threshold, zone or day pattern may be baked in."""
    metadata = blueprint["blueprint"]
    assert metadata["domain"] == "automation"
    assert metadata["source_url"].startswith("https://github.com/")
    assert metadata["source_url"].endswith(f"/{BLUEPRINT}")
    inputs = {
        name: value
        for section in metadata["input"].values()
        for name, value in section["input"].items()
    }
    # Targets and the start time must be chosen by the user, never defaulted.
    assert "default" not in inputs["start_time"]
    assert "default" not in inputs["climate_entity"]
    for name in ("defrost_entity", "temperature_entity", "presence_entity",
                 "battery_entity", "enable_entity"):
        assert inputs[name]["default"] == []
    assert inputs["days"]["default"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert blueprint["mode"] == "single"
    text = SOURCE.read_text(encoding="utf-8")
    assert "zone.home" not in text and "08:00" not in text
    assert "person." not in text and "input_boolean." not in text.replace(
        "domain: input_boolean", ""
    )
    for warning in ("garage", "never claims success", "EXPERIMENTAL"):
        assert warning in metadata["description"]
    # Nothing in the blueprint may retry or repeat a submitted request.
    assert "repeat:" not in text and "retry" not in text.replace("never retries", "")


async def test_default_configuration_sends_one_rapid_heat_request(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    await create(hass)
    await fire(hass)
    assert len(calls["rapid_heat"]) == 1
    assert calls["rapid_heat"][0].data == {
        "entity_id": [CLIMATE],
        "enable": True,
        "temperature": 21,
    }
    # No duration is sent unless one is chosen, so the integration option applies.
    assert "duration" not in calls["rapid_heat"][0].data
    assert not calls["defrost"] and not calls["turn_on"]


async def test_chosen_duration_and_temperature_are_passed_through(hass, calls):
    hass.states.async_set(CLIMATE, "unknown")
    await create(hass, run_duration=10, target_temperature=23.5)
    await fire(hass)
    assert calls["rapid_heat"][0].data == {
        "entity_id": [CLIMATE],
        "enable": True,
        "temperature": 23.5,
        "duration": 10,
    }


def test_temperature_input_is_unit_agnostic_and_can_express_fahrenheit(blueprint):
    """The input claims to follow the HA unit, so its range must allow one."""
    field = blueprint["blueprint"]["input"]["controls"]["input"]["target_temperature"]
    number = field["selector"]["number"]
    # A fixed °C label would be a lie, and a 33 maximum cannot express 70 °F.
    assert "unit_of_measurement" not in number
    assert number["min"] <= 61 and number["max"] >= 86
    assert "Home Assistant" in field["description"]


@pytest.mark.parametrize("unit_system", ["metric", "us_customary"])
@pytest.mark.parametrize("mode,action", [("rapid_heat", "rapid_heat"), ("climate", "set_temperature")])
async def test_the_chosen_temperature_reaches_each_action_unconverted(
    hass, calls, unit_system, mode, action
):
    """The blueprint passes the user's display-unit value through untouched.

    Converting it here would double-convert: both omoda_jaecoo.rapid_heat and
    climate.set_temperature read the Home Assistant unit themselves, which
    test_preconditioning.py proves against the real entity.
    """
    await hass.config.async_update(unit_system=unit_system)
    chosen = 70 if unit_system == "us_customary" else 21
    hass.states.async_set(CLIMATE, "off")
    await create(hass, conditioning_mode=mode, target_temperature=chosen)
    await fire(hass)
    assert calls[action][0].data["temperature"] == chosen


async def test_climate_mode_sets_preference_then_turns_on_once(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    await create(hass, conditioning_mode="climate", target_temperature=19)
    await fire(hass)
    assert not calls["rapid_heat"]
    assert len(calls["set_temperature"]) == 1
    assert calls["set_temperature"][0].data["temperature"] == 19
    assert len(calls["turn_on"]) == 1


async def test_both_controls_are_serialized_not_sent_together(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set(DEFROST, "off")
    await create(hass, defrost_entity=DEFROST)
    await start(hass)
    # The rear-window request must still be waiting out the command cooldown.
    assert len(calls["rapid_heat"]) == 1
    assert not calls["defrost"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    for _ in range(50):
        await asyncio.sleep(0)
    assert not calls["defrost"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=36))
    await hass.async_block_till_done()
    assert len(calls["defrost"]) == 1
    assert calls["defrost"][0].data["entity_id"] == [DEFROST]


async def test_defrost_only_schedule_sends_immediately_and_nothing_else(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set(DEFROST, "off")
    await create(hass, conditioning_mode="none", defrost_entity=DEFROST)
    await fire(hass)
    assert not calls["rapid_heat"] and not calls["turn_on"]
    assert len(calls["defrost"]) == 1


@pytest.mark.parametrize("climate_state,defrost_state", [("heat_cool", "on")])
async def test_already_running_controls_are_not_commanded_again(
    hass, calls, climate_state, defrost_state
):
    hass.states.async_set(CLIMATE, climate_state)
    hass.states.async_set(DEFROST, defrost_state)
    await create(hass, defrost_entity=DEFROST)
    await fire(hass)
    assert not calls["rapid_heat"] and not calls["defrost"]


@pytest.mark.parametrize(
    "reading,expected",
    [
        ("-4.5", 1),
        ("0", 1),
        ("0.9", 1),
        ("1", 0),
        ("12.0", 0),
        ("unavailable", 0),
        ("unknown", 0),
        ("", 0),
        ("cold", 0),
    ],
)
async def test_cold_gate_fails_closed_on_any_unusable_reading(
    hass, calls, reading, expected
):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set("sensor.outside_temperature", reading)
    await create(
        hass,
        temperature_entity="sensor.outside_temperature",
        temperature_threshold=1,
    )
    await fire(hass)
    assert len(calls["rapid_heat"]) == expected


async def test_cold_gate_sensor_missing_entirely_sends_nothing(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    await create(hass, temperature_entity="sensor.never_created")
    await fire(hass)
    assert not calls["rapid_heat"]


async def test_weather_entity_uses_its_temperature_attribute(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set("weather.local", "cloudy", {"temperature": -2})
    await create(hass, temperature_entity="weather.local", temperature_threshold=2)
    await fire(hass)
    assert len(calls["rapid_heat"]) == 1
    hass.states.async_set("weather.local", "cloudy", {"temperature": 9})
    calls["rapid_heat"].clear()
    await fire(hass)
    assert not calls["rapid_heat"]


@pytest.mark.parametrize(
    "state,expected", [("home", 1), ("not_home", 0), ("unavailable", 0)]
)
async def test_presence_gate_requires_an_exact_match(hass, calls, state, expected):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set("person.resident", state)
    await create(hass, presence_entity="person.resident")
    await fire(hass)
    assert len(calls["rapid_heat"]) == expected


@pytest.mark.parametrize(
    "reading,expected", [("55", 1), ("30", 1), ("29", 0), ("unknown", 0), ("", 0)]
)
async def test_battery_gate_fails_closed_without_a_usable_reading(
    hass, calls, reading, expected
):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set("sensor.vehicle_battery", reading)
    await create(hass, battery_entity="sensor.vehicle_battery", battery_minimum=30)
    await fire(hass)
    assert len(calls["rapid_heat"]) == expected


@pytest.mark.parametrize(
    "state,expected", [("on", 1), ("off", 0), ("unavailable", 0)]
)
async def test_manual_enable_helper_can_skip_a_day(hass, calls, state, expected):
    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set("input_boolean.precondition_tomorrow", state)
    await create(hass, enable_entity="input_boolean.precondition_tomorrow")
    await fire(hass)
    assert len(calls["rapid_heat"]) == expected


async def test_only_chosen_weekdays_run(hass, calls):
    hass.states.async_set(CLIMATE, "off")
    today = dt_util.now().strftime("%a").lower()
    other = next(day for day in ("mon", "tue", "wed") if day != today)
    await create(hass, days=[other])
    await fire(hass)
    assert not calls["rapid_heat"]


async def test_a_failed_request_is_never_retried_or_followed_by_defrost(hass, calls):
    """A rejected or ambiguous command must stop the run, not trigger a repeat."""

    def failing(call):
        raise HomeAssistantError("Command outcome unknown; do not retry.")

    hass.states.async_set(CLIMATE, "off")
    hass.states.async_set(DEFROST, "off")
    hass.services.async_register(DOMAIN, "rapid_heat", failing)
    await create(hass, defrost_entity=DEFROST)
    await fire(hass)
    assert not calls["defrost"]
