"""Explicit, accepted-only EU lock/climate commands; no wake or write retries.

The caller owns opt-in, persistent PIN blocking, and account-wide serialization.
No request occurs at import, construction, or argument validation time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .api import (
    _VEHICLE_LIST_KEYS,
    BFF,
    TSP,
    TSP_SECRET,
    VEHICLES_PATH,
    ApiError,
    ControlSession,
    JaecooApi,
    Vehicle,
    _bff_headers,
    _valid_vin,
)

# Public application protocol key, not an account credential.
SM4_KEY = b"mHU80av2zFtf4OY6"
COMMAND_ROUTES = {
    "authority": (BFF, "/tsp/v1/app/vmc/queryVehicleAuthority"),
    "vehicles": (BFF, VEHICLES_PATH),
    "default": (BFF, "/tsp/v1/app/vmc/setVecDefault"),
    "pin": (BFF, "/tsp/v1/app/cpm/checkPassword"),
    "lock": (TSP, "/asc/vehicleControl/lockControl"),
    "climate": (TSP, "/asc/vehicleControl/airControl"),
}
_ACCEPTED = frozenset({"000000", "A00079"})
_PREPARATION_SUCCESS = frozenset({"000000", "0", "200"})
_REJECTED = frozenset(
    {
        "A00082",
        "A00084",
        "A07312",
        "A07900",
        "A00089",
        "A00546",
        "A00567",
        "A00000",
        "A00374",
        "A00554",
        "A00604",
        "A00643",
        "A00757",
        "A00285",
        "A00282",
    }
)


class CommandError(ApiError):
    """Command was not sent or was definitively rejected; safe fixed text only."""


class PinVerificationError(CommandError):
    """PIN check failed or was inconclusive; no physical command was sent."""


class CommandOutcomeUnknown(CommandError):
    """Submission outcome is ambiguous; the caller must not retry automatically."""


class PinVerificationCancelled(asyncio.CancelledError):
    """Cancelled during PIN checking; locally block controls before re-raising."""


class CommandCancelled(asyncio.CancelledError):
    """Cancelled during submission; record unknown outcome before re-raising."""


def _encode_pin(pin: str) -> str:
    digest = hashlib.md5(pin.encode("utf-8"), usedforsecurity=False).hexdigest()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(digest.encode("ascii")) + padder.finalize()
    encryptor = Cipher(algorithms.SM4(SM4_KEY), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode(
        "ascii"
    )


def _command_body(
    vin: str, task_id: str, fields: dict[str, str], timestamp: int
) -> dict[str, str]:
    body = {
        **fields,
        "clientType": "1",
        "seq": f"{vin}-{timestamp}",
        "taskId": task_id,
        "vin": vin,
        "appId": "eu-1",
    }
    canonical = "".join(f"{key}={body[key]}&" for key in sorted(body))
    canonical += f"secretKey={TSP_SECRET[::2]}&timestamp={timestamp}"
    body["sign"] = (
        base64.b64encode(hashlib.sha256(canonical.encode()).digest()).decode().upper()
    )
    return body


def _successful(result: dict[str, Any]) -> bool:
    code = result.get("code")
    return (
        not isinstance(code, bool)
        and str(code) in _PREPARATION_SUCCESS
        and result.get("success") is not False
        and result.get("ok") is not False
        and not result.get("error")
    )


def _discovery_contains(result: dict[str, Any], vin: str) -> bool:
    """Require fresh successful discovery to include the explicitly selected VIN."""
    data = result.get("data")
    candidates: list[Any] = []
    if isinstance(data, list):
        candidates.extend(data)
    elif isinstance(data, dict):
        if data.get("vin") or data.get("VIN"):
            candidates.append(data)
        for key in _VEHICLE_LIST_KEYS:
            if isinstance(data.get(key), list):
                candidates.extend(data[key])
    return any(
        isinstance(item, dict) and _valid_vin(item.get("vin") or item.get("VIN")) == vin
        for item in candidates
    )


def _permission_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        return int(value)
    return None


class CommandClient:
    """Narrow explicit commands. A None return means accepted, never completed."""

    def __init__(
        self, api: JaecooApi, session: aiohttp.ClientSession, country_code: str
    ) -> None:
        if not isinstance(country_code, str) or not re.fullmatch(
            r"[1-9][0-9]{0,3}", country_code
        ):
            raise CommandError("Invalid account country code.")
        self._api = api
        self._session = session
        self._country_code = country_code

    def _validate(self, vin: str, pin: str, flag: bool) -> Vehicle:
        if type(flag) is not bool:
            raise CommandError("Command state must be a boolean.")
        if not isinstance(pin, str) or re.fullmatch(r"[0-9]{4,8}", pin) is None:
            raise CommandError(
                "A control PIN of 4 to 8 digits is required; no command was sent."
            )
        try:
            return self._api.get_vehicle(vin)
        except ApiError:
            raise CommandError(
                "Controls require a discovered account vehicle."
            ) from None

    async def _post(
        self, route: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        if route not in COMMAND_ROUTES:
            raise CommandError("Endpoint is not in the command allowlist.")
        host, path = COMMAND_ROUTES[route]
        physical = route in ("lock", "climate")
        failure = CommandOutcomeUnknown if physical else CommandError
        try:
            async with self._session.post(
                host + path,
                headers=headers,
                data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                ),
                timeout=aiohttp.ClientTimeout(total=40, connect=10),
                allow_redirects=False,
                ssl=True,
                raise_for_status=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise failure(
                        "Backend response did not establish command acceptance."
                    )
                try:
                    result = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise failure(
                        "Backend returned an unreadable command response."
                    ) from None
                if not isinstance(result, dict):
                    raise failure("Backend returned an unexpected command response.")
                return result
        except asyncio.CancelledError:
            if physical:
                raise CommandCancelled(
                    "Command submission was cancelled; outcome is unknown."
                ) from None
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise failure("Command request outcome could not be established.") from None

    async def _bff(
        self, route: str, credentials: ControlSession, body: dict[str, Any]
    ) -> dict[str, Any]:
        _, path = COMMAND_ROUTES[route]
        return await self._post(
            route,
            _bff_headers(path, self._country_code, credentials.account.access_token),
            body,
        )

    async def _authority(
        self, vin: str, credentials: ControlSession, category: int
    ) -> None:
        result = await self._bff(
            "authority",
            credentials,
            {
                "vin": vin,
                "tUserId": credentials.tuser_id,
                "channelId": "1",
            },
        )
        if not _successful(result):
            raise CommandError("Vehicle control authority could not be established.")
        data = result.get("data")
        permissions = data.get("permissionList") if isinstance(data, dict) else None
        if not isinstance(permissions, list):
            raise CommandError(
                "Vehicle control authority is unknown; no command was sent."
            )
        states: dict[int, int | None] = {}
        for item in permissions:
            if not isinstance(item, dict):
                continue
            identifier = _permission_integer(item.get("id"))
            state = _permission_integer(item.get("state"))
            if identifier is not None:
                # Conflicting duplicates cannot establish permission.
                states[identifier] = (
                    state
                    if identifier not in states or states[identifier] == state
                    else None
                )
        if states.get(category) is None or states[category] <= 0:
            raise CommandError(
                "Vehicle control is not known to be authorized; no command was sent."
            )
        if (
            category == 204
            and 2041 in states
            and (states[2041] is None or states[2041] <= 0)
        ):
            raise CommandError(
                "Climate control is not authorized; no command was sent."
            )

    async def _task_id(self, vin: str, pin: str, credentials: ControlSession) -> str:
        for route, body in (("vehicles", {}), ("default", {"vin": vin})):
            result = await self._bff(route, credentials, body)
            if not _successful(result):
                raise CommandError("Vehicle selection failed; no command was sent.")
            if route == "vehicles" and not _discovery_contains(result, vin):
                raise CommandError(
                    "Selected vehicle is absent from current discovery; no command was sent."
                )
        try:
            result = await self._bff(
                "pin",
                credentials,
                {
                    "vin": vin,
                    "tUserId": credentials.tuser_id,
                    "channelId": "1",
                    "password": _encode_pin(pin),
                    "needDecode": 0,
                    "scene": 0,
                    "type": 0,
                },
            )
            data = result.get("data")
            task_id = data.get("taskId") if isinstance(data, dict) else None
            if task_id is None:
                task_id = result.get("taskId")
            if (
                not _successful(result)
                or not isinstance(task_id, str)
                or not task_id.strip()
            ):
                raise PinVerificationError(
                    "PIN verification failed or was inconclusive; no command was sent."
                )
            return task_id
        except asyncio.CancelledError:
            raise PinVerificationCancelled(
                "PIN verification was cancelled; no command was sent."
            ) from None
        except ApiError:
            raise PinVerificationError(
                "PIN verification failed or was inconclusive; no command was sent."
            ) from None

    async def _submit(
        self, vin: str, pin: str, category: int, route: str, fields: dict[str, str]
    ) -> None:
        try:
            credentials = await self._api.async_control_session(vin)
        except ApiError:
            raise CommandError(
                "Control session is unavailable; no command was sent."
            ) from None
        await self._authority(vin, credentials, category)
        task_id = await self._task_id(vin, pin, credentials)
        timestamp = int(time.time() * 1000)
        result = await self._post(
            route,
            {
                "Authorization": credentials.user_token,
                "timestamp": str(timestamp),
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "okhttp/4.9.2",
            },
            _command_body(vin, task_id, fields, timestamp),
        )
        code = result.get("code")
        if isinstance(code, str) and code in _REJECTED:
            raise CommandError(
                "Vehicle command was rejected; no automatic retry will be made."
            )
        if (
            not isinstance(code, str)
            or code not in _ACCEPTED
            or result.get("success") is False
            or result.get("ok") is False
            or result.get("error")
        ):
            raise CommandOutcomeUnknown(
                "Vehicle command acceptance is unknown; do not retry automatically."
            )

    async def async_lock(self, vin: str, pin: str, locked: bool) -> None:
        self._validate(vin, pin, locked)
        await self._submit(vin, pin, 203, "lock", {"lockType": "0" if locked else "1"})

    async def async_climate(
        self, vin: str, pin: str, on: bool, temperature: float, duration: int
    ) -> None:
        vehicle = self._validate(vin, pin, on)
        low, high, step = (
            vehicle.min_temperature,
            vehicle.max_temperature,
            vehicle.temperature_step,
        )
        if (
            low is None
            or high is None
            or step not in (0.5, 1.0)
            or not 14 <= low < high <= 33
            or isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or not low <= temperature <= high
            or not math.isclose(
                temperature, round(temperature, 1), rel_tol=0, abs_tol=1e-8
            )
            or not math.isclose(
                (temperature - low) / step,
                round((temperature - low) / step),
                abs_tol=1e-8,
            )
        ):
            raise CommandError(
                "Climate temperature is outside discovered vehicle capabilities."
            )
        if (
            type(duration) is not int
            or not 1 <= duration <= 60
            or duration not in vehicle.allowed_air_durations
        ):
            raise CommandError(
                "Climate duration is not allowed by discovered vehicle capabilities."
            )
        await self._submit(
            vin,
            pin,
            204,
            "climate",
            {
                "airControlType": "1" if on else "0",
                "airType": "1",
                "temperature": f"{temperature:.1f}",
                "times": str(duration),
            },
        )
