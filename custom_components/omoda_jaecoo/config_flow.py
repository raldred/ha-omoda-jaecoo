"""Native setup, reauthentication, PIN reconfiguration and polling options."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import probatio as p
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
    JaecooApi,
    RateLimitError,
    Vehicle,
)
from .const import (
    CONF_CLEAR_PIN,
    CONF_CONTROL_PIN,
    CONF_COUNTRY_CODE,
    CONF_EMAIL,
    CONF_POLL_INTERVAL,
    CONF_SELECTED_VINS,
    CONF_TOKENS,
    CONF_VEHICLES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def account_unique_id(email: str) -> str:
    """One entry per EU account; do not expose the email in registry identifiers."""
    return "eu_" + hashlib.sha256(email.strip().casefold().encode()).hexdigest()


def valid_pin(pin: str) -> bool:
    # No remote verification: wrong-PIN attempts can lock the account.
    return not pin or bool(re.fullmatch(r"[0-9]{4,8}", pin))


def error_key(error: ApiError) -> str:
    if isinstance(error, AuthenticationError):
        return "invalid_auth"
    if isinstance(error, RateLimitError):
        return "rate_limited"
    if isinstance(error, CannotConnect):
        return "cannot_connect"
    return "api_error"


class OmodaJaecooConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure an account, discover vehicles and optionally store its control PIN."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: JaecooApi | None = None
        self._email = ""
        self._country_code = "44"
        self._vehicles: list[Vehicle] = []
        self._selected_vins: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OmodaJaecooOptionsFlow:
        return OmodaJaecooOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            email = str(user_input.get(CONF_EMAIL, "")).strip()
            country = str(user_input.get(CONF_COUNTRY_CODE, "44")).strip()
            if "@" not in email or not email:
                errors[CONF_EMAIL] = "invalid_email"
            elif not re.fullmatch(r"[1-9][0-9]{0,3}", country):
                errors[CONF_COUNTRY_CODE] = "invalid_country"
            elif not user_input.get(CONF_PASSWORD):
                errors[CONF_PASSWORD] = "invalid_auth"
            else:
                # Detect duplicates BEFORE signing in, avoiding unnecessary session eviction.
                await self.async_set_unique_id(account_unique_id(email))
                self._abort_if_unique_id_configured()
                api = JaecooApi(
                    async_get_clientsession(self.hass), country_code=country
                )
                try:
                    await api.async_login(email, user_input[CONF_PASSWORD])
                    vehicles = await api.async_list_vehicles()
                except ApiError as err:
                    errors["base"] = error_key(err)
                else:
                    if not vehicles:
                        errors["base"] = "no_vehicles"
                    else:
                        self._api = api
                        self._email, self._country_code = email, country
                        self._vehicles = vehicles
                        if len(vehicles) == 1:
                            self._selected_vins = [vehicles[0].vin]
                            return await self.async_step_pin()
                        return await self.async_step_vehicles()
        # Never put a password/PIN in defaults or suggested values, including on errors.
        return self.async_show_form(
            step_id="user",
            data_schema=p.Schema(
                {
                    p.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.EMAIL
                        )
                    ),
                    p.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    p.Required(
                        CONF_COUNTRY_CODE, default="44"
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

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
        # Owner/delegate accounts can expose the same physical VIN. Do not let a second
        # entry silently collide with the existing vehicle's stable entity identifiers.
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
                    CONF_EMAIL: self._email,
                    CONF_COUNTRY_CODE: self._country_code,
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
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_PASSWORD):
                errors[CONF_PASSWORD] = "invalid_auth"
            else:
                # Identity is fixed to this entry, not editable during reauthentication.
                api = JaecooApi(
                    async_get_clientsession(self.hass),
                    country_code=entry.data[CONF_COUNTRY_CODE],
                )
                try:
                    await api.async_login(
                        entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
                    )
                    vehicles = await api.async_list_vehicles()
                except ApiError as err:
                    errors["base"] = error_key(err)
                else:
                    if not set(entry.data[CONF_SELECTED_VINS]).issubset(
                        {v.vin for v in vehicles}
                    ):
                        return self.async_abort(reason="vehicle_access_changed")
                    if api.tokens is None:
                        errors["base"] = "invalid_auth"
                    else:
                        await self.async_set_unique_id(
                            account_unique_id(entry.data[CONF_EMAIL])
                        )
                        self._abort_if_unique_id_mismatch()
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates={
                                CONF_TOKENS: api.tokens.to_dict(),
                                CONF_VEHICLES: [v.to_dict() for v in vehicles],
                            },
                        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=p.Schema(
                {
                    p.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
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
                elif pin:
                    data[CONF_CONTROL_PIN] = pin
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
    """Tune passive polling without storing credentials in options."""

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
                return self.async_create_entry(
                    title="", data={CONF_POLL_INTERVAL: int(value)}
                )
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
                }
            ),
            errors=errors,
        )
