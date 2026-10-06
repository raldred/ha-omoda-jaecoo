"""Four native sign-in routes, fixed-account reauth and local control settings."""

from __future__ import annotations

import hashlib
import math
import re
from time import monotonic
from typing import Any

import voluptuous as p
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ApiError,
    AuthenticationError,
    CannotConnect,
    CaptchaError,
    JaecooApi,
    OtpDeliveryError,
    OtpDeliveryUnknown,
    RateLimitError,
    Vehicle,
)
from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_AUTH_METHOD,
    CONF_CHARGE_DEPTH_IS_TARGET,
    CONF_CLEAR_PIN,
    CONF_CLIMATE_DURATION,
    CONF_CONTROL_PIN,
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_ENABLE_CHARGING_DETAILS,
    CONF_ENABLE_CONTROLS,
    CONF_ENABLE_LOCATION,
    CONF_OTP,
    CONF_OTP_ACTION,
    CONF_PHONE,
    CONF_PIN_BLOCKED,
    CONF_POLL_INTERVAL,
    CONF_SELECTED_VINS,
    CONF_TOKENS,
    CONF_VEHICLES,
    DEFAULT_CLIMATE_DURATION,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    OTP_MAX_ATTEMPTS,
    OTP_RESEND_SECONDS,
)
from .identity import InvalidPhoneNumber, normalize_phone, phone_identity

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def account_unique_id(
    identifier: str, account_type: str = "email", country_code: str = "44"
) -> str:
    """Preserve legacy email IDs; phone IDs use a separate canonical namespace."""
    identity = (
        identifier.strip().casefold()
        if account_type == "email"
        else "phone:" + phone_identity(identifier, country_code)
    )
    return "eu_" + hashlib.sha256(identity.encode()).hexdigest()


def valid_pin(pin: str) -> bool:
    return not pin or bool(re.fullmatch(r"[0-9]{4,8}", pin))


def error_key(error: ApiError) -> str:
    if isinstance(error, AuthenticationError):
        return "invalid_auth"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, CaptchaError):
        return "captcha_failed"
    if isinstance(error, OtpDeliveryError):
        return "delivery_failed"
    if isinstance(error, CannotConnect):
        return "cannot_connect"
    return "api_error"


class OmodaJaecooConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Choose identifier and authentication method independently."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: JaecooApi | None = None
        self._account_type = "email"
        self._auth_method = "password"
        self._identifier = ""
        self._country_code = "44"
        self._vehicles: list[Vehicle] = []
        self._selected_vins: list[str] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None
        self._otp_requested = False
        self._otp_attempts = 0
        self._code_send_in_progress = False
        self._resend_not_before = 0.0
        self._auth_not_before = 0.0
        self._discovery_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OmodaJaecooOptionsFlow:
        return OmodaJaecooOptionsFlow()

    def _new_api(self) -> JaecooApi:
        return JaecooApi(
            async_get_clientsession(self.hass), country_code=self._country_code
        )

    def _account_data(self) -> dict[str, str]:
        field = CONF_EMAIL if self._account_type == "email" else CONF_PHONE
        return {
            CONF_ACCOUNT_TYPE: self._account_type,
            CONF_AUTH_METHOD: self._auth_method,
            CONF_COUNTRY_CODE: self._country_code,
            field: self._identifier,
        }

    def _destination(self) -> str:
        if self._account_type == "phone":
            return f"+{self._country_code} …{self._identifier[-4:]}"
        local, _, domain = self._identifier.partition("@")
        return f"{local[:1]}…@{domain}"

    def _honor_retry_after(self, error: ApiError) -> None:
        if not isinstance(error, RateLimitError):
            return
        delay = getattr(error, "retry_after", None)
        if (
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or not math.isfinite(delay)
            or delay <= 0
        ):
            delay = OTP_RESEND_SECONDS
        deadline = monotonic() + delay
        self._resend_not_before = max(self._resend_not_before, deadline)
        self._auth_not_before = max(self._auth_not_before, deadline)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            kind = user_input.get(CONF_ACCOUNT_TYPE, "email")
            method = user_input.get(CONF_AUTH_METHOD, "password")
            if kind not in ("email", "phone") or method not in ("password", "otp"):
                errors["base"] = "invalid_method"
            else:
                self._account_type, self._auth_method = kind, method
                return await self.async_step_credentials()
        return self.async_show_form(
            step_id="user",
            data_schema=p.Schema(
                {
                    p.Required(
                        CONF_ACCOUNT_TYPE, default="email"
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["email", "phone"], translation_key="account_type"
                        )
                    ),
                    p.Required(
                        CONF_AUTH_METHOD, default="password"
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["password", "otp"], translation_key="auth_method"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    def _credentials_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        field = CONF_EMAIL if self._account_type == "email" else CONF_PHONE
        field_type = (
            selector.TextSelectorType.EMAIL
            if field == CONF_EMAIL
            else selector.TextSelectorType.TEL
        )
        schema = {
            p.Required(field): selector.TextSelector(
                selector.TextSelectorConfig(type=field_type)
            ),
            p.Required(
                CONF_COUNTRY_CODE, default=self._country_code
            ): selector.TextSelector(),
        }
        if self._auth_method == "password":
            schema[p.Required(CONF_PASSWORD)] = PASSWORD_SELECTOR
        return self.async_show_form(
            step_id="credentials", data_schema=p.Schema(schema), errors=errors
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is None:
            return self._credentials_form(errors)
        field = CONF_EMAIL if self._account_type == "email" else CONF_PHONE
        identifier = str(user_input.get(field, "")).strip()
        country = str(user_input.get(CONF_COUNTRY_CODE, "44")).strip()
        if not re.fullmatch(r"[1-9][0-9]{0,3}", country):
            errors[CONF_COUNTRY_CODE] = "invalid_country"
        elif self._account_type == "email":
            parts = identifier.split("@")
            if (
                len(parts) != 2
                or not all(parts)
                or any(c.isspace() for c in identifier)
            ):
                errors[field] = "invalid_email"
        else:
            try:
                identifier = await self.hass.async_add_executor_job(
                    normalize_phone, identifier, country
                )
            except InvalidPhoneNumber:
                errors[field] = "invalid_phone"
        if self._auth_method == "password" and not user_input.get(CONF_PASSWORD):
            errors[CONF_PASSWORD] = "invalid_auth"
        if errors:
            return self._credentials_form(errors)
        # Check duplicates before password login or requesting a code.
        await self.async_set_unique_id(
            account_unique_id(identifier, self._account_type, country)
        )
        self._abort_if_unique_id_configured()
        self._identifier, self._country_code = identifier, country
        self._api = self._new_api()
        self._otp_requested = False
        self._otp_attempts = 0
        if self._auth_method == "otp":
            return await self.async_step_request_code()
        if monotonic() < self._auth_not_before:
            return self._credentials_form({"base": "rate_limited"})
        try:
            await self._password_login(user_input[CONF_PASSWORD])
        except ApiError as err:
            self._honor_retry_after(err)
            return self._credentials_form({"base": error_key(err)})
        return await self._async_discover()

    async def _password_login(self, password: str) -> None:
        assert self._api is not None
        if self._account_type == "phone":
            await self._api.async_login_phone(self._identifier, password)
        else:
            await self._api.async_login(self._identifier, password)

    def _request_code_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="request_code",
            data_schema=p.Schema({}),
            errors=errors,
            description_placeholders={"destination": self._destination()},
        )

    async def async_step_request_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._api is None or not self._identifier:
            return self.async_abort(reason="setup_expired")
        if user_input is None:
            return self._request_code_form({})
        return await self._async_send_code(resend=False)

    async def _async_send_code(self, *, resend: bool) -> ConfigFlowResult:
        assert self._api is not None
        if self._code_send_in_progress or monotonic() < self._resend_not_before:
            form = self._otp_form if resend else self._request_code_form
            return form({"base": "resend_cooldown"})
        self._resend_not_before = monotonic() + OTP_RESEND_SECONDS
        self._code_send_in_progress = True
        try:
            await self._api.async_request_otp(self._identifier, self._account_type)
        except OtpDeliveryUnknown:
            # The email/SMS may have arrived despite an ambiguous delivery response.
            self._otp_requested = True
            self._otp_attempts = 0
            return self._otp_form({"base": "delivery_unknown"})
        except ApiError as err:
            self._honor_retry_after(err)
            form = self._otp_form if resend else self._request_code_form
            return form({"base": error_key(err)})
        finally:
            self._code_send_in_progress = False
            self._resend_not_before = max(
                self._resend_not_before, monotonic() + OTP_RESEND_SECONDS
            )
        self._otp_requested = True
        self._otp_attempts = 0
        return self._otp_form({})

    def _otp_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="otp",
            data_schema=p.Schema(
                {
                    p.Optional(CONF_OTP): PASSWORD_SELECTOR,
                    p.Required(
                        CONF_OTP_ACTION, default="verify"
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["verify", "resend"], translation_key="otp_action"
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"destination": self._destination()},
        )

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._api is None or not self._otp_requested:
            return self.async_abort(reason="setup_expired")
        if user_input is None:
            return self._otp_form({})
        action = user_input.get(CONF_OTP_ACTION, "verify")
        if action == "resend":
            return await self._async_send_code(resend=True)
        if action != "verify":
            return self._otp_form({"base": "invalid_method"})
        if monotonic() < self._auth_not_before:
            return self._otp_form({"base": "rate_limited"})
        if self._otp_attempts >= OTP_MAX_ATTEMPTS:
            return self._otp_form({"base": "too_many_attempts"})
        code = str(user_input.get(CONF_OTP, "")).strip()
        if not re.fullmatch(r"[0-9]{4,8}", code):
            return self._otp_form({CONF_OTP: "invalid_code"})
        try:
            await self._api.async_login_otp(self._identifier, code, self._account_type)
        except AuthenticationError:
            self._otp_attempts += 1
            key = (
                "too_many_attempts"
                if self._otp_attempts >= OTP_MAX_ATTEMPTS
                else "invalid_code"
            )
            return self._otp_form({"base": key})
        except ApiError as err:
            self._honor_retry_after(err)
            return self._otp_form({"base": error_key(err)})
        return await self._async_discover()

    async def _async_discover(self) -> ConfigFlowResult:
        if self._api is None or self._api.tokens is None:
            return self.async_abort(reason="setup_expired")
        if monotonic() < self._auth_not_before:
            self._discovery_error = "rate_limited"
            return self._discovery_form()
        try:
            vehicles = await self._api.async_list_vehicles()
        except ApiError as err:
            self._honor_retry_after(err)
            self._discovery_error = error_key(err)
            return self._discovery_form()
        if not vehicles:
            self._discovery_error = "no_vehicles"
            return self._discovery_form()
        self._discovery_error = None
        self._vehicles = vehicles
        if self._reauth_entry is not None:
            entry = self._reauth_entry
            if not set(entry.data[CONF_SELECTED_VINS]).issubset(
                {v.vin for v in vehicles}
            ):
                return self.async_abort(reason="vehicle_access_changed")
            await self.async_set_unique_id(
                account_unique_id(
                    self._identifier, self._account_type, self._country_code
                )
            )
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    **self._account_data(),
                    CONF_TOKENS: self._api.tokens.to_dict(),
                    CONF_VEHICLES: [v.to_dict() for v in vehicles],
                },
            )
        if len(vehicles) == 1:
            self._selected_vins = [vehicles[0].vin]
            return await self.async_step_pin()
        return await self.async_step_vehicles()

    def _discovery_form(self) -> ConfigFlowResult:
        errors = {"base": self._discovery_error} if self._discovery_error else {}
        return self.async_show_form(
            step_id="discovery", data_schema=p.Schema({}), errors=errors
        )

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_discover()
        return self._discovery_form()

    async def async_step_vehicles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            requested = user_input.get(CONF_SELECTED_VINS, [])
            known = {vehicle.vin for vehicle in self._vehicles}
            if (
                not isinstance(requested, list)
                or not requested
                or not set(requested).issubset(known)
            ):
                errors["base"] = "invalid_selection"
            else:
                self._selected_vins = list(dict.fromkeys(requested))
                return await self.async_step_pin()
        choices = [
            {"value": v.vin, "label": f"{v.name} (…{v.vin[-4:]})"}
            for v in self._vehicles
        ]
        return self.async_show_form(
            step_id="vehicles",
            data_schema=p.Schema(
                {
                    p.Required(
                        CONF_SELECTED_VINS, default=[v.vin for v in self._vehicles]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=choices,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_pin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured_vins = {
            vin
            for entry in self._async_current_entries()
            for vin in entry.data.get(CONF_SELECTED_VINS, [])
        }
        if configured_vins.intersection(self._selected_vins):
            return self.async_abort(reason="vehicle_already_configured")
        errors = {}
        if user_input is not None:
            pin = str(user_input.get(CONF_CONTROL_PIN, ""))
            if not valid_pin(pin):
                errors[CONF_CONTROL_PIN] = "invalid_pin"
            elif self._api is None or self._api.tokens is None:
                return self.async_abort(reason="setup_expired")
            else:
                data = {
                    **self._account_data(),
                    CONF_TOKENS: self._api.tokens.to_dict(),
                    CONF_VEHICLES: [v.to_dict() for v in self._vehicles],
                    CONF_SELECTED_VINS: self._selected_vins,
                }
                if pin:
                    data[CONF_CONTROL_PIN] = pin
                return self.async_create_entry(title="Omoda / Jaecoo", data=data)
        return self.async_show_form(
            step_id="pin",
            data_schema=p.Schema(
                {
                    p.Optional(CONF_CONTROL_PIN): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._reauth_entry = entry
        self._account_type = entry.data.get(CONF_ACCOUNT_TYPE, "email")
        self._auth_method = entry.data.get(CONF_AUTH_METHOD, "password")
        self._country_code = entry.data.get(CONF_COUNTRY_CODE, "44")
        if self._account_type not in ("email", "phone") or self._auth_method not in (
            "password",
            "otp",
        ):
            return self.async_abort(reason="invalid_account_config")
        try:
            if self._account_type == "phone":
                self._identifier = await self.hass.async_add_executor_job(
                    normalize_phone, entry.data[CONF_PHONE], self._country_code
                )
            else:
                self._identifier = entry.data[CONF_EMAIL]
                if (
                    not isinstance(self._identifier, str)
                    or self._identifier.count("@") != 1
                    or not all(self._identifier.split("@"))
                    or any(c.isspace() for c in self._identifier)
                ):
                    raise ValueError("Invalid saved account identifier.")
            self._api = self._new_api()
        except (KeyError, ValueError, ApiError):
            return self.async_abort(reason="invalid_account_config")
        if self._auth_method == "otp":
            return await self.async_step_request_code()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._reauth_entry is None or self._api is None:
            return self.async_abort(reason="setup_expired")
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_PASSWORD):
                errors[CONF_PASSWORD] = "invalid_auth"
            elif monotonic() < self._auth_not_before:
                errors["base"] = "rate_limited"
            else:
                try:
                    await self._password_login(user_input[CONF_PASSWORD])
                except ApiError as err:
                    self._honor_retry_after(err)
                    errors["base"] = error_key(err)
                else:
                    return await self._async_discover()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=p.Schema(
                {
                    p.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders={"destination": self._destination()},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            pin = str(user_input.get(CONF_CONTROL_PIN, ""))
            clear = user_input.get(CONF_CLEAR_PIN, False)
            if not valid_pin(pin):
                errors[CONF_CONTROL_PIN] = "invalid_pin"
            elif pin and clear:
                errors["base"] = "pin_conflict"
            else:
                data = dict(entry.data)
                if clear:
                    data.pop(CONF_CONTROL_PIN, None)
                    data.pop(CONF_PIN_BLOCKED, None)
                elif pin:
                    data[CONF_CONTROL_PIN] = pin
                    data.pop(CONF_PIN_BLOCKED, None)
                return self.async_update_reload_and_abort(
                    entry, data=data, reason="reconfigure_successful"
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=p.Schema(
                {
                    p.Optional(CONF_CONTROL_PIN): PASSWORD_SELECTOR,
                    p.Optional(
                        CONF_CLEAR_PIN, default=False
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )


class OmodaJaecooOptionsFlow(config_entries.OptionsFlow):
    """Tune polling and explicitly opt into PIN-protected remote commands."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            value = user_input.get(CONF_POLL_INTERVAL)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and MIN_POLL_INTERVAL <= value <= MAX_POLL_INTERVAL
                and int(value) == value
            ):
                enabled = user_input.get(CONF_ENABLE_CONTROLS, False)
                duration = user_input.get(
                    CONF_CLIMATE_DURATION, DEFAULT_CLIMATE_DURATION
                )
                location = user_input.get(CONF_ENABLE_LOCATION, False)
                details = user_input.get(CONF_ENABLE_CHARGING_DETAILS, False)
                depth_target = user_input.get(CONF_CHARGE_DEPTH_IS_TARGET, False)
                if depth_target and not details:
                    errors["base"] = "charging_details_required"
                elif enabled and not self.config_entry.data.get(CONF_CONTROL_PIN):
                    errors["base"] = "pin_required"
                elif enabled and self.config_entry.data.get(CONF_PIN_BLOCKED):
                    errors["base"] = "pin_blocked"
                elif (
                    isinstance(duration, bool)
                    or not isinstance(duration, (float, int))
                    or not 1 <= duration <= 60
                    or int(duration) != duration
                ):
                    errors[CONF_CLIMATE_DURATION] = "invalid_duration"
                else:
                    return self.async_create_entry(
                        title="",
                        data={
                            CONF_POLL_INTERVAL: int(value),
                            CONF_ENABLE_CONTROLS: bool(enabled),
                            CONF_CLIMATE_DURATION: int(duration),
                            CONF_ENABLE_LOCATION: bool(location),
                            CONF_ENABLE_CHARGING_DETAILS: bool(details),
                            CONF_CHARGE_DEPTH_IS_TARGET: bool(depth_target),
                        },
                    )
            else:
                errors[CONF_POLL_INTERVAL] = "invalid_interval"
        return self.async_show_form(
            step_id="init",
            data_schema=p.Schema(
                {
                    p.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL,
                            max=MAX_POLL_INTERVAL,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    p.Optional(
                        CONF_ENABLE_LOCATION,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_LOCATION, False
                        ),
                    ): selector.BooleanSelector(),
                    p.Optional(
                        CONF_ENABLE_CHARGING_DETAILS,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_CHARGING_DETAILS, False
                        ),
                    ): selector.BooleanSelector(),
                    p.Optional(
                        CONF_CHARGE_DEPTH_IS_TARGET,
                        default=self.config_entry.options.get(
                            CONF_CHARGE_DEPTH_IS_TARGET, False
                        ),
                    ): selector.BooleanSelector(),
                    p.Optional(
                        CONF_ENABLE_CONTROLS,
                        default=self.config_entry.options.get(
                            CONF_ENABLE_CONTROLS, False
                        ),
                    ): selector.BooleanSelector(),
                    p.Optional(
                        CONF_CLIMATE_DURATION,
                        default=self.config_entry.options.get(
                            CONF_CLIMATE_DURATION, DEFAULT_CLIMATE_DURATION
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=60,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
            errors=errors,
        )
