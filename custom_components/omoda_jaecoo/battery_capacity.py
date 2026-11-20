"""Strict local validation of user-supplied usable battery capacities."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .const import CONF_USABLE_BATTERY_CAPACITIES


def usable_battery_capacity(options: Mapping[str, Any], vin: str) -> float | None:
    """Return configured usable kWh, never infer capacity from a vehicle model."""
    if not isinstance(options, Mapping):
        return None
    capacities = options.get(CONF_USABLE_BATTERY_CAPACITIES)
    if not isinstance(capacities, Mapping):
        return None
    value = capacities.get(vin)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Bounds first also safely reject arbitrarily large Python integers.
    if not 0.1 <= value <= 500 or not math.isfinite(value):
        return None
    return float(value)
