"""Pure gates over discovered vehicle metadata and reported state.

Nothing here performs I/O. Missing metadata means the control is not offered:
reported presence is evidence of hardware, an absent field is never capability.
"""

from __future__ import annotations

import math
from typing import Any

# The vehicle must report this before any rear-window write is offered.
REAR_WINDOW_HEATING = "rear_defrost"


def allowed_durations(vehicle: Any) -> tuple[int, ...]:
    """Discovery reports a set of allowed run durations, not an upper bound.

    Returns an empty tuple when the set is missing or implausible, so callers
    fail closed instead of clamping a request onto an unsupported value.
    """
    durations = getattr(vehicle, "allowed_air_durations", ())
    if (
        not isinstance(durations, tuple)
        or not durations
        or any(
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60
            for value in durations
        )
    ):
        return ()
    return durations


def temperature_bounds(vehicle: Any) -> tuple[float, float, float] | None:
    """Do not invent control capabilities for vehicles missing model metadata."""
    values = (
        getattr(vehicle, "min_temperature", None),
        getattr(vehicle, "max_temperature", None),
        getattr(vehicle, "temperature_step", None),
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        return None
    minimum, maximum, step = (float(value) for value in values)
    if (
        not 14 <= minimum < maximum <= 33
        or step not in (0.5, 1)
        or not allowed_durations(vehicle)
    ):
        return None
    return minimum, maximum, step


def rear_window_heating_reported(snapshot: Any) -> bool:
    """Only a parsed 0/1 reading proves a rear-window heater exists to control.

    Discovery publishes no capability flag for it, so the car's own telemetry is
    the only available evidence; an unknown or absent reading offers no control.
    """
    extras = getattr(snapshot, "extras", None)
    return isinstance(extras, dict) and isinstance(
        extras.get(REAR_WINDOW_HEATING), bool
    )
