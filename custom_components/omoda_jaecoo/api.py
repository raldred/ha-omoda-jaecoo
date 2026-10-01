"""Read-only async EU Omoda/Jaecoo API, independent of Home Assistant.

Protocol reference: chery-connect-ha/omoda9-ha, revision
7d80cd6a7215168f58d147cbd475c82e52cd3944. See THIRD_PARTY_NOTICES.md.
The caller owns the injected aiohttp session. No requests occur at import time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BFF = "https://legend-oj.omodaauto.nl/api"
TSP = "https://tspconsole-eu.cheryinternational.com"
TOKEN_PATH = "/auth/oauth2/token"
VEHICLES_PATH = "/tsp/v1/app/vmc/queryList"
TSP_LOGIN_PATH = "/tsp/v1/app/auth/login"
REALTIME_PATH = "/asr/manager/realtime"
# Public application protocol constants, not account credentials.
APP_BASIC = "Basic bGVnZW5kQXBwOmxlZ2VuZEFwcA=="
APP_VERSION = "1.1.9"
SIGN_NONCE = "chery_legend_h5"
SIGN_SECRET = "cX5fR8lJ6pK2xD4uH1eK4pY6wA4xO0sK"
AES_KEY = b"w9R8Ag1KiL0pvMHc"
TSP_SECRET = "EBUJPYr7oDd48C9Te9c755942Y7T48dV293Y4Z931J098X41aYf0"
ALLOWED_ROUTES = {
    "token": (BFF, TOKEN_PATH),
    "vehicles": (BFF, VEHICLES_PATH),
    "tsp_login": (BFF, TSP_LOGIN_PATH),
    "realtime": (TSP, REALTIME_PATH),
}
_VEHICLE_LIST_KEYS = (
    "controlCarList",
    "authorizedControlCarList",
    "carList",
    "list",
    "vehicles",
)
_AUTH_ERRORS = frozenset({"invalid_grant", "invalid_token"})
_SUCCESS_CODES = frozenset({"000000", "0", "200"})


class ApiError(Exception):
    """Safe, fixed-text API error; never includes server messages or credentials."""


class AuthenticationError(ApiError):
    """Definitive account credential/session rejection."""


class CannotConnect(ApiError):
    """Transport failure or temporary server unavailability, not bad credentials."""


class RateLimitError(ApiError):
    """The backend asked the caller to slow down."""


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _valid_vin(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value.upper()):
        return value.upper()
    return None


@dataclass(frozen=True)
class TokenSet:
    """Persist only account tokens and the explicit server expiry, never passwords."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TokenSet:
        access = _text(value.get("access_token"))
        if not access:
            raise ApiError("Stored account token is invalid.")
        return cls(
            access, _text(value.get("refresh_token")), _number(value.get("expires_at"))
        )


@dataclass(frozen=True)
class ControlSession:
    """Ephemeral credentials for explicit commands; never persisted or displayed."""

    account: TokenSet = field(repr=False)
    user_token: str = field(repr=False)
    tuser_id: str = field(repr=False)

    @property
    def tokens(self) -> TokenSet:
        return self.account


def _climate_metadata(
    minimum: Any, maximum: Any, step: Any, durations: Any
) -> tuple[float | None, float | None, float | None, tuple[int, ...]]:
    low, high, increment = _number(minimum), _number(maximum), _number(step)
    if low is None or high is None or not 14 <= low < high <= 33:
        low = high = None
    if increment not in (0.5, 1.0):
        increment = None
    values = durations.split(",") if isinstance(durations, str) else durations
    allowed: tuple[int, ...] = ()
    if isinstance(values, (list, tuple)) and values:
        numbers = [_number(value) for value in values]
        if all(n is not None and n.is_integer() and 1 <= n <= 60 for n in numbers):
            allowed = tuple(sorted({int(n) for n in numbers if n is not None}))
    return low, high, increment, allowed


@dataclass(frozen=True)
class Vehicle:
    """Minimal discovery metadata; raw account/vehicle records are not persisted."""

    vin: str
    name: str
    model: str | None
    power_type: int | None
    min_temperature: float | None = None
    max_temperature: float | None = None
    temperature_step: float | None = None
    allowed_air_durations: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vin": self.vin,
            "name": self.name,
            "model": self.model,
            "power_type": self.power_type,
            "min_temperature": self.min_temperature,
            "max_temperature": self.max_temperature,
            "temperature_step": self.temperature_step,
            "allowed_air_durations": list(self.allowed_air_durations),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Vehicle:
        vin = _valid_vin(value.get("vin"))
        if not vin:
            raise ApiError("Stored vehicle identifier is invalid.")
        power = value.get("power_type")
        return cls(
            vin,
            _text(value.get("name")) or "Vehicle",
            _text(value.get("model")),
            power if isinstance(power, int) and not isinstance(power, bool) else None,
            *_climate_metadata(
                value.get("min_temperature"),
                value.get("max_temperature"),
                value.get("temperature_step"),
                value.get("allowed_air_durations"),
            ),
        )


def _payload(response: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "body"):
        value = response.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _encode_password(password: str) -> str:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_KEY)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode(
        "ascii"
    )


def _bff_headers(
    path: str, country_code: str, access_token: str | None = None
) -> dict[str, str]:
    ts = int(time.time() * 1000)
    signature = hashlib.sha256(
        f"{SIGN_SECRET}{SIGN_NONCE}{path}{ts}".encode()
    ).hexdigest()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "en-GB",
        "Accept-Encoding": "gzip, deflate",
        "agent": "android",
        "version": APP_VERSION,
        "appversion": APP_VERSION,
        "Authorization": APP_BASIC,
        "DEPT-ID": country_code,
        "TENANT-ID": "300006",
        "TENANT-CODE": "300006",
        "CLIENT-TOC": "Y",
        "tenantCode": "300006",
        "tenantID": "300006",
        "channelId": "1",
        "countryId": "1",
        "User-Agent": "okhttp/4.9.0",
        "nonce": SIGN_NONCE,
        "timestamp": str(ts),
        "url": path,
        "signature": signature,
    }
    if access_token:
        headers.update(
            Authorization=f"Bearer {access_token}",
            **{"Content-Type": "application/json; charset=UTF-8"},
        )
    return headers


def _realtime_body(vin: str, timestamp: int) -> dict[str, str]:
    body = {"vin": vin, "appId": "eu-1"}
    canonical = "".join(f"{key}={body[key]}&" for key in sorted(body))
    canonical += f"secretKey={TSP_SECRET[::2]}&timestamp={timestamp}"
    body["sign"] = (
        base64.b64encode(hashlib.sha256(canonical.encode()).digest()).decode().upper()
    )
    return body


class JaecooApi:
    """Account auth, discovery, and existing cloud snapshots only (no wake/commands)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        country_code: str = "44",
        tokens: TokenSet | None = None,
        on_tokens: Callable[[TokenSet], None] | None = None,
    ) -> None:
        if not isinstance(country_code, str) or not re.fullmatch(
            r"[1-9][0-9]{0,3}", country_code
        ):
            raise ApiError("Invalid account country code.")
        self._session = session
        self._country_code = country_code
        self._tokens = tokens
        self._on_tokens = on_tokens
        self._refresh_lock = asyncio.Lock()
        self._rejected_refresh: TokenSet | None = None
        self._tsp_lock = asyncio.Lock()
        self._user_token: str | None = None
        self._tuser_id: str | None = None
        self._tsp_account: TokenSet | None = None
        self._tsp_login_confirmed = False
        self._vehicles: dict[str, Vehicle] = {}

    @property
    def tokens(self) -> TokenSet | None:
        return self._tokens

    async def _post(
        self, route: str, headers: dict[str, str], **kwargs: Any
    ) -> dict[str, Any]:
        if route not in ALLOWED_ROUTES:
            raise ApiError("Endpoint is not in the read-only allowlist.")
        host, path = ALLOWED_ROUTES[route]
        account_route = host == BFF
        try:
            async with self._session.post(
                host + path,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=40, connect=10),
                allow_redirects=False,
                ssl=True,
                raise_for_status=False,
                **kwargs,
            ) as response:
                # HTTP status takes precedence over arbitrary JSON messages.
                if response.status == 429:
                    raise RateLimitError("Backend rate limit reached.")
                if response.status >= 500:
                    raise CannotConnect("Backend is temporarily unavailable.")
                if account_route and response.status in (401, 424):
                    raise AuthenticationError("Account session was rejected.")
                try:
                    result = await response.json()
                except (ValueError, aiohttp.ContentTypeError):
                    raise ApiError("Backend returned an unreadable response.") from None
                if not isinstance(result, dict):
                    raise ApiError("Backend returned an unexpected response shape.")
                # Only machine identifiers are used, never message/msg/error_description.
                identifiers = [result.get(key) for key in ("error", "key", "code")]
                if account_route and any(
                    isinstance(value, str) and value in _AUTH_ERRORS
                    for value in identifiers
                ):
                    raise AuthenticationError(
                        "Account credentials or session were rejected."
                    )
                if not 200 <= response.status < 300:
                    raise ApiError("Backend rejected the request.")
                code = result.get("code")
                if str(code) == "429":
                    raise RateLimitError("Backend rate limit reached.")
                if route == "realtime" and code == "A07900":
                    return {}
                if account_route and str(code) in ("401", "424"):
                    raise AuthenticationError("Account session was rejected.")
                if (
                    (code is not None and str(code) not in _SUCCESS_CODES)
                    or result.get("success") is False
                    or result.get("ok") is False
                    or _text(result.get("error"))
                ):
                    raise ApiError("Backend returned an API error.")
                return result
        except (aiohttp.ClientError, TimeoutError):
            raise CannotConnect("Unable to connect to the backend.") from None

    def _accept_tokens(
        self, response: dict[str, Any], previous: TokenSet | None = None
    ) -> TokenSet:
        data = response if _text(response.get("access_token")) else _payload(response)
        access = _text(data.get("access_token"))
        if not access:
            raise ApiError("Authentication response did not include an account token.")
        refresh = _text(data.get("refresh_token"))
        if refresh is None and previous is not None:
            refresh = previous.refresh_token
        duration = _number(data.get("expires_in"))
        expires_at = (
            time.time() + duration if duration is not None and duration >= 0 else None
        )
        tokens = TokenSet(access, refresh, expires_at)
        self._tokens = tokens
        self._rejected_refresh = None
        self._user_token = None
        self._tuser_id = None
        self._tsp_account = None
        self._tsp_login_confirmed = False
        if self._on_tokens is not None:
            self._on_tokens(tokens)
        return tokens

    async def async_login(self, email: str, password: str) -> TokenSet:
        """One explicit password login; the password is never retained or retried."""
        if not email or not password:
            raise AuthenticationError("Account email and password are required.")
        async with self._refresh_lock:
            result = await self._post(
                "token",
                _bff_headers(TOKEN_PATH, self._country_code),
                data={
                    "username": email,
                    "password": _encode_password(password),
                    "grant_type": "password",
                    "scope": "server",
                    "needDecode": "1",
                    "loginType": "email",
                },
            )
            self._vehicles = {}
            return self._accept_tokens(result)

    async def _refresh(self, previous: TokenSet) -> None:
        async with self._refresh_lock:
            # Identity rather than token text also handles non-rotating access tokens.
            if self._tokens is not previous:
                return
            if not previous.refresh_token or self._rejected_refresh is previous:
                raise AuthenticationError("Account session needs reauthentication.")
            try:
                # Match the upstream grant exactly: this endpoint expects query
                # parameters for refresh. Never log request URLs or chain transport
                # exceptions, since the HTTPS URL now contains the refresh token.
                result = await self._post(
                    "token",
                    _bff_headers(TOKEN_PATH, self._country_code),
                    params={
                        "grant_type": "refresh_token",
                        "refresh_token": previous.refresh_token,
                        "scope": "server",
                    },
                )
            except AuthenticationError:
                # Do not repeatedly submit a definitively revoked refresh token.
                self._rejected_refresh = previous
                raise
            self._accept_tokens(result, previous)

    async def _ensure_tokens(self) -> TokenSet:
        tokens = self._tokens
        if tokens is None:
            raise AuthenticationError("Account login is required.")
        if tokens.expires_at is not None and time.time() >= tokens.expires_at - 60:
            await self._refresh(tokens)
        assert self._tokens is not None
        return self._tokens

    async def _account_post(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        tokens = await self._ensure_tokens()
        _, path = ALLOWED_ROUTES[route]
        try:
            return await self._post(
                route,
                _bff_headers(path, self._country_code, tokens.access_token),
                json=body,
            )
        except AuthenticationError:
            # Exactly one reactive refresh and retry, only for definitive account rejection.
            await self._refresh(tokens)
            assert self._tokens is not None
            return await self._post(
                route,
                _bff_headers(path, self._country_code, self._tokens.access_token),
                json=body,
            )

    async def async_list_vehicles(self) -> list[Vehicle]:
        result = await self._account_post("vehicles", {})
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
            if not candidates and not any(
                isinstance(data.get(key), list) for key in _VEHICLE_LIST_KEYS
            ):
                raise ApiError(
                    "Vehicle discovery returned an unrecognized response shape."
                )
        else:
            raise ApiError("Vehicle discovery returned an unrecognized response shape.")
        vehicles: dict[str, Vehicle] = {}
        for item in candidates:
            if not isinstance(item, dict):
                continue
            vin = _valid_vin(item.get("vin") or item.get("VIN"))
            if not vin or vin in vehicles:
                continue
            name = (
                _text(item.get("nickname"))
                or _text(item.get("nickName"))
                or _text(item.get("fullName"))
                or "Vehicle"
            )
            model = _text(item.get("model")) or _text(item.get("modelName"))
            power = _number(item.get("powerType"))
            vehicles[vin] = Vehicle(
                vin,
                name,
                model,
                int(power) if power is not None and power.is_integer() else None,
                *_climate_metadata(
                    item.get("minTemperature"),
                    item.get("maxTemperature"),
                    item.get("temperatureStepLength"),
                    item.get("maxAirDuration"),
                ),
            )
        self._vehicles = vehicles
        return list(vehicles.values())

    def get_vehicle(self, vin: str) -> Vehicle:
        """Return only already-discovered account metadata, without doing I/O."""
        if not isinstance(vin, str) or vin not in self._vehicles:
            raise ApiError("Vehicle is not a discovered account vehicle.")
        return self._vehicles[vin]

    async def _ensure_tsp_login(self) -> TokenSet:
        async with self._tsp_lock:
            tokens = await self._ensure_tokens()
            if self._user_token is None or self._tsp_account is not tokens:
                try:
                    result = await self._post(
                        "tsp_login",
                        _bff_headers(
                            TSP_LOGIN_PATH, self._country_code, tokens.access_token
                        ),
                        json={"channelId": "1"},
                    )
                except AuthenticationError:
                    await self._refresh(tokens)
                    tokens = await self._ensure_tokens()
                    result = await self._post(
                        "tsp_login",
                        _bff_headers(
                            TSP_LOGIN_PATH, self._country_code, tokens.access_token
                        ),
                        json={"channelId": "1"},
                    )
                # A concurrent read may have refreshed while this login was in flight.
                # Do not associate old TSP credentials with a new account session.
                if self._tokens is not tokens:
                    raise ApiError(
                        "Account session changed during vehicle-service login."
                    )
                data = _payload(result)
                self._user_token = _text(data.get("userToken"))
                identifier = data.get("tUserId")
                self._tuser_id = (
                    str(identifier)
                    if isinstance(identifier, int)
                    and not isinstance(identifier, bool)
                    and identifier > 0
                    else _text(identifier)
                )
                if self._user_token is None:
                    raise ApiError("Vehicle-service login did not include a token.")
                self._tsp_account = tokens
                code = result.get("code")
                self._tsp_login_confirmed = (
                    not isinstance(code, bool) and str(code) in _SUCCESS_CODES
                )
            assert self._tsp_account is not None
            return self._tsp_account

    async def async_control_session(self, vin: str) -> ControlSession:
        """Obtain cached TSP credentials only for an explicit known-VIN command."""
        self.get_vehicle(vin)
        account = await self._ensure_tsp_login()
        self.get_vehicle(vin)
        if (
            not self._tsp_login_confirmed
            or self._user_token is None
            or not self._user_token.strip()
            or self._tuser_id is None
            or not self._tuser_id.strip()
        ):
            raise ApiError("Vehicle-service login did not include control credentials.")
        return ControlSession(account, self._user_token, self._tuser_id)

    async def async_realtime(self, vin: str) -> dict[str, Any]:
        """Read an authorized car's cloud snapshot; sleeping cars are never woken."""
        self.get_vehicle(vin)
        await self._ensure_tsp_login()
        assert self._user_token is not None
        user_token = self._user_token
        ts = int(time.time() * 1000)
        try:
            result = await self._post(
                "realtime",
                {
                    "Authorization": user_token,
                    "timestamp": str(ts),
                    "x-TenantId": "",
                    "Content-Type": "application/json; charset=UTF-8",
                    "Accept": "application/json",
                    "User-Agent": "okhttp/4.9.0",
                    "version": APP_VERSION,
                    "agent": "android",
                },
                json=_realtime_body(vin, ts),
            )
        except ApiError:
            # A vehicle-service rejection is not proof that the account token is bad.
            # Re-establish TSP auth on a later poll, without retrying this read.
            if self._user_token == user_token:
                self._user_token = None
                self._tuser_id = None
                self._tsp_account = None
                self._tsp_login_confirmed = False
            raise
        return _payload(result)
