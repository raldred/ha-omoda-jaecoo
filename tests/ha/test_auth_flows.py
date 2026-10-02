"""Four native account/auth choices, exercised through real HA flow managers.

All transport is mocked by the socket-blocking HA harness. These identifiers,
passwords, codes and tokens are synthetic and must never leave the test process.
"""

import hashlib
from unittest.mock import patch

import pytest
from conftest import DOMAIN, EMAIL, PASSWORD, PIN, VIN
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_config_flow import assert_native_form, schema_value, start

from custom_components.omoda_jaecoo.config_flow import account_unique_id

PHONE = "7700900123"
OTP = "001234"


async def configure(hass, result, data):
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def credentials(hass, account="email", method="otp"):
    result = await start(hass)
    result = await configure(
        hass, result, {"account_type": account, "auth_method": method}
    )
    assert_native_form(result, "credentials")
    return result


def identity_input(account):
    return {
        account: EMAIL if account == "email" else "+44 7700 900123",
        "country_code": "44",
    }


async def request_form(hass, account="email"):
    result = await credentials(hass, account)
    result = await configure(hass, result, identity_input(account))
    assert not assert_native_form(result, "request_code")
    return result


async def otp_form(hass, account="email"):
    result = await request_form(hass, account)
    result = await configure(hass, result, {})
    assert_native_form(result, "otp")
    return result


async def finish(hass, result):
    if result.get("step_id") == "vehicles":
        result = await configure(hass, result, {"selected_vins": [VIN]})
    assert_native_form(result, "pin")
    return await configure(hass, result, {})


def assert_no_auth(api):
    api.async_login.assert_not_awaited()
    api.async_login_phone.assert_not_awaited()
    api.async_login_otp.assert_not_awaited()
    api.async_request_otp.assert_not_awaited()


def assert_secret_free_form(result):
    schema = result["data_schema"]
    for secret in (PASSWORD, OTP, PIN):
        assert secret not in repr(schema)
        for marker in schema.schema:
            assert secret not in repr(getattr(marker, "description", None))
            default = marker.default
            assert secret not in repr(default() if callable(default) else default)


@pytest.mark.parametrize("account", ["email", "phone"])
@pytest.mark.parametrize("method", ["password", "otp"])
async def test_all_four_native_paths(hass, auth_api, tokens, caplog, account, method):
    result = await start(hass)
    schema = assert_native_form(result, "user")
    assert {str(key) for key in schema} == {"account_type", "auth_method"}
    for field, default in (("account_type", "email"), ("auth_method", "password")):
        marker = next(key for key in schema if str(key) == field)
        assert marker.default() == default
    assert_no_auth(auth_api)
    result = await configure(
        hass, result, {"account_type": account, "auth_method": method}
    )
    schema = assert_native_form(result, "credentials")
    assert {str(key) for key in schema} == {account, "country_code"} | (
        {"password"} if method == "password" else set()
    )
    assert_no_auth(auth_api)
    user_input = identity_input(account)
    identifier = EMAIL if account == "email" else PHONE
    if method == "password":
        assert schema_value(schema, "password").config["type"] == "password"
        user_input["password"] = PASSWORD
    result = await configure(hass, result, user_input)
    if method == "otp":
        assert not assert_native_form(result, "request_code")
        assert_no_auth(auth_api)
        auth_api.async_list_vehicles.assert_not_awaited()
        result = await configure(hass, result, {})
        schema = assert_native_form(result, "otp")
        assert schema_value(schema, "otp").config["type"] == "password"
        assert_secret_free_form(result)
        auth_api.async_request_otp.assert_awaited_once_with(identifier, account)
        auth_api.async_login_otp.assert_not_awaited()
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        auth_api.async_login_otp.assert_awaited_once_with(identifier, OTP, account)
        auth_api.async_login.assert_not_awaited()
        auth_api.async_login_phone.assert_not_awaited()
    else:
        login_method = (
            auth_api.async_login if account == "email" else auth_api.async_login_phone
        )
        login_method.assert_awaited_once_with(identifier, PASSWORD)
        auth_api.async_request_otp.assert_not_awaited()
        auth_api.async_login_otp.assert_not_awaited()
    auth_api.async_list_vehicles.assert_awaited_once()
    with patch(f"custom_components.{DOMAIN}.async_setup_entry", return_value=True):
        result = await finish(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[account] == identifier
    assert data["account_type"] == account
    assert data["auth_method"] == method
    assert data["country_code"] == "44"
    assert data["tokens"] == tokens.to_dict()
    assert data["selected_vins"] == [VIN]
    assert not {
        "password",
        "otp",
        "code",
        "captcha",
        "captchaVerification",
    }.intersection(data)
    assert PASSWORD not in repr(data)
    assert OTP not in repr(data)
    for secret in (PASSWORD, OTP, tokens.access_token, tokens.refresh_token):
        assert secret not in caplog.text
    auth_api.async_realtime.assert_not_awaited()


@pytest.mark.parametrize(
    "phone,country,national",
    [
        ("07700 900123", "44", PHONE),
        ("+44 7700 900123", "44", PHONE),
        ("0044 7700 900123", "44", PHONE),
        ("+39 02 1234 5678", "39", "0212345678"),
        ("02 1234 5678", "39", "0212345678"),
    ],
)
async def test_equivalent_phone_inputs_have_canonical_identity(
    hass, auth_api, phone, country, national
):
    result = await credentials(hass, "phone", "password")
    result = await configure(
        hass, result, {"phone": phone, "country_code": country, "password": PASSWORD}
    )
    auth_api.async_login_phone.assert_awaited_once_with(national, PASSWORD)
    with patch(f"custom_components.{DOMAIN}.async_setup_entry", return_value=True):
        result = await finish(hass, result)
    assert result["data"]["phone"] == national
    assert result["result"].unique_id == account_unique_id(national, "phone", country)
    assert national not in result["result"].unique_id


@pytest.mark.parametrize(
    "account,identifier,country",
    [
        ("phone", "+33 612345678", "44"),
        ("phone", "not a phone", "44"),
        ("email", "invalid", "44"),
        ("email", EMAIL, "0"),
    ],
)
async def test_invalid_identity_never_authenticates_or_sends(
    hass, auth_api, account, identifier, country
):
    result = await credentials(hass, account)
    result = await configure(
        hass, result, {account: identifier, "country_code": country}
    )
    assert_native_form(result, "credentials")
    assert result["errors"]
    assert_no_auth(auth_api)
    auth_api.async_list_vehicles.assert_not_awaited()


def test_email_unique_id_is_byte_compatible_with_old_entries():
    assert (
        account_unique_id(" OWNER@EXAMPLE.INVALID ")
        == "eu_" + hashlib.sha256(EMAIL.encode()).hexdigest()
    )
    assert account_unique_id("07700 900123", "phone", "44") == account_unique_id(
        "+44 7700 900123", "phone", "44"
    )
    assert account_unique_id(PHONE, "phone", "44") != account_unique_id(PHONE)


@pytest.mark.parametrize("account", ["email", "phone"])
@pytest.mark.parametrize("method", ["password", "otp"])
async def test_duplicate_guard_precedes_all_auth_and_delivery(
    hass, auth_api, tokens, account, method
):
    identifier = EMAIL if account == "email" else PHONE
    saved = MockConfigEntry(
        domain=DOMAIN,
        unique_id=account_unique_id(identifier, account, "44"),
        data={account: identifier, "tokens": tokens.to_dict()},
    )
    saved.add_to_hass(hass)
    result = await credentials(hass, account, method)
    data = identity_input(account)
    if method == "password":
        data["password"] = PASSWORD
    result = await configure(hass, result, data)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert_no_auth(auth_api)
    auth_api.async_list_vehicles.assert_not_awaited()


@pytest.mark.parametrize("code", ["", "123", "123456789", "12a4", "１２３４", "12 34"])
async def test_invalid_otp_is_local_and_not_defaulted(hass, auth_api, code):
    result = await otp_form(hass)
    result = await configure(hass, result, {"otp": code, "otp_action": "verify"})
    assert_native_form(result, "otp")
    assert result["errors"]
    assert_secret_free_form(result)
    auth_api.async_login_otp.assert_not_awaited()
    auth_api.async_request_otp.assert_awaited_once()


@pytest.mark.parametrize(
    "exception,error",
    [
        ("CaptchaError", "captcha_failed"),
        ("OtpDeliveryError", "delivery_failed"),
        ("CannotConnect", "cannot_connect"),
    ],
)
async def test_known_delivery_failure_stays_at_explicit_request(
    hass, auth_api, api_types, exception, error
):
    auth_api.async_request_otp.side_effect = getattr(api_types, exception)()
    result = await request_form(hass)
    auth_api.async_request_otp.assert_not_awaited()
    result = await configure(hass, result, {})
    assert not assert_native_form(result, "request_code")
    assert error in result["errors"].values()
    auth_api.async_request_otp.assert_awaited_once()
    auth_api.async_login_otp.assert_not_awaited()
    # Re-rendering a form is never permission to deliver another code.
    result = await configure(hass, result, None)
    assert_native_form(result, "request_code")
    auth_api.async_request_otp.assert_awaited_once()
    result = await configure(hass, result, {})
    assert_native_form(result, "request_code")
    assert "resend_cooldown" in result["errors"].values()
    auth_api.async_request_otp.assert_awaited_once()


async def test_ambiguous_delivery_allows_entering_code_without_second_send(
    hass, auth_api, api_types
):
    auth_api.async_request_otp.side_effect = api_types.OtpDeliveryUnknown()
    result = await request_form(hass, "phone")
    result = await configure(hass, result, {})
    assert_native_form(result, "otp")
    assert "delivery_unknown" in result["errors"].values()
    auth_api.async_request_otp.assert_awaited_once_with(PHONE, "phone")
    result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
    assert_native_form(result, "vehicles")
    auth_api.async_login_otp.assert_awaited_once_with(PHONE, OTP, "phone")
    auth_api.async_request_otp.assert_awaited_once()


@pytest.mark.parametrize("method", ["password", "otp"])
@pytest.mark.parametrize("failure", ["CannotConnect", "RateLimitError", "empty"])
async def test_discovery_retry_retains_login_without_reusing_code(
    hass, auth_api, api_types, vehicles, method, failure, monkeypatch
):
    from custom_components.omoda_jaecoo import config_flow as flow_module

    clock = [1000.0]
    monkeypatch.setattr(flow_module, "monotonic", lambda: clock[0])
    auth_api.async_list_vehicles.side_effect = [
        ([] if failure == "empty" else getattr(api_types, failure)()),
        vehicles,
    ]
    result = await credentials(hass, "email", method)
    data = identity_input("email")
    if method == "password":
        data["password"] = PASSWORD
    result = await configure(hass, result, data)
    if method == "otp":
        result = await configure(hass, result, {})
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
    assert not assert_native_form(result, "discovery")
    assert result["errors"]
    assert_secret_free_form(result)
    auth_api.async_list_vehicles.assert_awaited_once()
    if failure == "RateLimitError":
        result = await configure(hass, result, {})
        assert result["errors"] == {"base": "rate_limited"}
        auth_api.async_list_vehicles.assert_awaited_once()
        clock[0] += 61
    result = await configure(hass, result, {})
    assert_native_form(result, "vehicles")
    assert auth_api.async_list_vehicles.await_count == 2
    if method == "otp":
        auth_api.async_request_otp.assert_awaited_once()
        auth_api.async_login_otp.assert_awaited_once()
        auth_api.async_login.assert_not_awaited()
    else:
        auth_api.async_login.assert_awaited_once()
        auth_api.async_request_otp.assert_not_awaited()


@pytest.mark.parametrize("account", ["email", "phone"])
async def test_otp_reauth_requires_consent_and_preserves_local_settings(
    hass, auth_api, api_types, tokens, vehicles, account
):
    replacement = api_types.TokenSet(
        "replacement-otp-access", "replacement-otp-refresh", 4102444800.0
    )
    auth_api.async_login_otp.return_value = replacement
    identifier = EMAIL if account == "email" else PHONE
    data = {
        account: identifier,
        "country_code": "44",
        "account_type": account,
        "auth_method": "otp",
        "tokens": tokens.to_dict(),
        "vehicles": [v.to_dict() for v in vehicles],
        "selected_vins": [VIN],
        "control_pin": PIN,
        "pin_blocked": True,
    }
    saved = MockConfigEntry(
        domain=DOMAIN,
        unique_id=account_unique_id(identifier, account, "44"),
        data=data,
        options={"poll_interval": 15, "enable_controls": True, "climate_duration": 12},
    )
    saved.add_to_hass(hass)
    old_options = dict(saved.options)
    result = await saved.start_reauth_flow(hass)
    assert not assert_native_form(result, "request_code")
    assert_no_auth(auth_api)
    result = await configure(hass, result, {})
    assert_native_form(result, "otp")
    auth_api.async_request_otp.assert_awaited_once_with(identifier, account)
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert saved.unique_id == account_unique_id(identifier, account, "44")
    for field in (
        account,
        "country_code",
        "selected_vins",
        "control_pin",
        "pin_blocked",
    ):
        assert saved.data[field] == data[field]
    assert saved.options == old_options
    assert saved.data["tokens"] == replacement.to_dict()
    assert OTP not in repr(saved.data)
    assert PASSWORD not in repr(saved.data)


@pytest.mark.parametrize("method", ["password", "otp"])
async def test_phone_entries_do_not_authenticate_or_send_on_setup_and_poll(
    hass, auth_api, api_types, entry, method
):
    entry.add_to_hass(hass)
    data = dict(entry.data)
    data.pop("email")
    data.update(phone=PHONE, account_type="phone", auth_method=method)
    hass.config_entries.async_update_entry(
        entry, data=data, unique_id=account_unique_id(PHONE, "phone", "44")
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert_no_auth(auth_api)
    # Even an automatic reauth transition after a poll error needs fresh consent.
    auth_api.async_realtime.side_effect = api_types.AuthenticationError()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert_no_auth(auth_api)
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["step_id"] == (
        "request_code" if method == "otp" else "reauth_confirm"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_phone_password_reauth_uses_saved_identity_not_editable_fields(
    hass, auth_api, entry
):
    entry.add_to_hass(hass)
    data = dict(entry.data)
    data.pop("email")
    data.update(phone=PHONE, account_type="phone", auth_method="password")
    hass.config_entries.async_update_entry(
        entry, data=data, unique_id=account_unique_id(PHONE, "phone", "44")
    )
    result = await entry.start_reauth_flow(hass)
    schema = assert_native_form(result, "reauth_confirm")
    assert {str(key) for key in schema} == {"password"}
    assert_secret_free_form(result)
    assert_no_auth(auth_api)
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await configure(hass, result, {"password": PASSWORD})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    auth_api.async_login_phone.assert_awaited_once_with(PHONE, PASSWORD)
    auth_api.async_login.assert_not_awaited()
    auth_api.async_request_otp.assert_not_awaited()
    assert entry.data["control_pin"] == PIN
    assert entry.data["selected_vins"] == [VIN]


@pytest.mark.parametrize("mismatch", ["identity", "vehicle"])
async def test_otp_reauth_cannot_change_account_or_selected_vin(
    hass, auth_api, entry, vehicles, mismatch
):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "account_type": "email", "auth_method": "otp"},
        unique_id=(
            account_unique_id("different@example.invalid")
            if mismatch == "identity"
            else entry.unique_id
        ),
    )
    before = dict(entry.data)
    if mismatch == "vehicle":
        auth_api.async_list_vehicles.return_value = vehicles[1:]
    result = await entry.start_reauth_flow(hass)
    assert_native_form(result, "request_code")
    result = await configure(hass, result, {})
    result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] != "reauth_successful"
    assert entry.data == before
    auth_api.async_request_otp.assert_awaited_once()
    auth_api.async_login_otp.assert_awaited_once()


async def test_otp_reauth_discovery_retry_does_not_send_or_verify_again(
    hass, auth_api, api_types, entry, vehicles
):
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "account_type": "email", "auth_method": "otp"}
    )
    before = dict(entry.data)
    auth_api.async_list_vehicles.side_effect = [api_types.CannotConnect(), vehicles]
    result = await entry.start_reauth_flow(hass)
    result = await configure(hass, result, {})
    result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
    assert not assert_native_form(result, "discovery")
    assert entry.data == before
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await configure(hass, result, {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    auth_api.async_request_otp.assert_awaited_once()
    auth_api.async_login_otp.assert_awaited_once()
    assert auth_api.async_list_vehicles.await_count == 2


async def test_wrong_otp_has_three_attempt_limit_without_automatic_send(
    hass, auth_api, api_types, caplog
):
    auth_api.async_login_otp.side_effect = api_types.AuthenticationError()
    result = await otp_form(hass)
    for _ in range(3):
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        assert_native_form(result, "otp")
        assert result["errors"]
        assert_secret_free_form(result)
    assert auth_api.async_login_otp.await_count == 3
    result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]
    assert_secret_free_form(result)
    assert auth_api.async_login_otp.await_count == 3
    auth_api.async_request_otp.assert_awaited_once()
    assert OTP not in caplog.text
    assert "too_many_attempts" in result["errors"].values()


async def test_resend_requires_explicit_action_and_sixty_second_cooldown(
    hass, auth_api
):
    with patch(
        f"custom_components.{DOMAIN}.config_flow.monotonic", return_value=1000
    ) as clock:
        result = await otp_form(hass, "phone")
        result = await configure(hass, result, None)
        assert_native_form(result, "otp")
        auth_api.async_request_otp.assert_awaited_once()
        result = await configure(hass, result, {"otp_action": "resend"})
        assert_native_form(result, "otp")
        assert "resend_cooldown" in result["errors"].values()
        auth_api.async_request_otp.assert_awaited_once()
        auth_api.async_login_otp.assert_not_awaited()
        clock.return_value = 1059
        result = await configure(hass, result, {"otp_action": "resend"})
        assert "resend_cooldown" in result["errors"].values()
        auth_api.async_request_otp.assert_awaited_once()
        clock.return_value = 1061
        result = await configure(hass, result, {"otp_action": "resend"})
        assert_native_form(result, "otp")
        assert auth_api.async_request_otp.await_count == 2
        assert auth_api.async_request_otp.await_args.args == (PHONE, "phone")
        auth_api.async_login_otp.assert_not_awaited()
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        assert_native_form(result, "vehicles")
        auth_api.async_login_otp.assert_awaited_once_with(PHONE, OTP, "phone")


@pytest.mark.parametrize("retry_after", [None, 120])
async def test_delivery_rate_limit_honors_retry_after_and_native_minimum(
    hass, auth_api, api_types, retry_after
):
    auth_api.async_request_otp.side_effect = [
        api_types.RateLimitError(retry_after=retry_after),
        None,
    ]
    with patch(
        f"custom_components.{DOMAIN}.config_flow.monotonic", return_value=1000
    ) as clock:
        result = await request_form(hass)
        result = await configure(hass, result, {})
        assert_native_form(result, "request_code")
        assert "rate_limited" in result["errors"].values()
        clock.return_value = 1059 if retry_after is None else 1061
        result = await configure(hass, result, {})
        assert_native_form(result, "request_code")
        assert "resend_cooldown" in result["errors"].values()
        auth_api.async_request_otp.assert_awaited_once()
        clock.return_value = 1061 if retry_after is None else 1121
        result = await configure(hass, result, {})
        assert_native_form(result, "otp")
        assert auth_api.async_request_otp.await_count == 2
        auth_api.async_login_otp.assert_not_awaited()


async def test_ambiguous_delivery_also_establishes_resend_cooldown(
    hass, auth_api, api_types
):
    auth_api.async_request_otp.side_effect = api_types.OtpDeliveryUnknown()
    with patch(f"custom_components.{DOMAIN}.config_flow.monotonic", return_value=1000):
        result = await otp_form(hass)
        result = await configure(hass, result, {"otp_action": "resend"})
        assert_native_form(result, "otp")
        assert "resend_cooldown" in result["errors"].values()
        auth_api.async_request_otp.assert_awaited_once()
        auth_api.async_login_otp.assert_not_awaited()


async def test_explicit_resend_resets_wrong_code_attempt_budget(
    hass, auth_api, api_types, tokens
):
    auth_api.async_login_otp.side_effect = api_types.AuthenticationError()
    with patch(
        f"custom_components.{DOMAIN}.config_flow.monotonic", return_value=1000
    ) as clock:
        result = await otp_form(hass)
        for _ in range(3):
            result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        assert auth_api.async_login_otp.await_count == 3
        auth_api.async_request_otp.assert_awaited_once()
        clock.return_value = 1061
        result = await configure(hass, result, {"otp_action": "resend"})
        assert_native_form(result, "otp")
        assert auth_api.async_request_otp.await_count == 2
        auth_api.async_login_otp.side_effect = None
        auth_api.async_login_otp.return_value = tokens
        result = await configure(hass, result, {"otp": OTP, "otp_action": "verify"})
        assert_native_form(result, "vehicles")
        assert auth_api.async_login_otp.await_count == 4
