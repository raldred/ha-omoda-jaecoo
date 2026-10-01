"""Account onboarding, fixed-account reauthentication and local settings."""

from unittest.mock import patch

import probatio as p
import pytest
from conftest import DOMAIN, EMAIL, PASSWORD, PIN, VIN, VIN_2
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import selector


def assert_native_form(result, step):
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    assert isinstance(result["data_schema"], p.Schema)
    return result["data_schema"].schema


def schema_value(schema, name):
    return next(value for key, value in schema.items() if str(key) == name)


async def start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def login(hass, result, email=EMAIL):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": email, "password": PASSWORD}
    )


async def test_native_account_vehicle_and_masked_pin_forms(
    hass, mock_api, tokens, caplog
):
    """Credentials only reach login; discovered vehicles are explicitly selected."""
    with patch(f"custom_components.{DOMAIN}.async_setup_entry", return_value=True):
        result = await start(hass)
        schema = assert_native_form(result, "user")
        assert schema_value(schema, "password").config["type"] == "password"
        result = await login(hass, result, "  OWNER@EXAMPLE.INVALID  ")
        vehicle_schema = assert_native_form(result, "vehicles")
        selection = next(key for key in vehicle_schema if str(key) == "selected_vins")
        assert selection.default() == [VIN, VIN_2]
        # Preserve login identity casing; only account deduplication is casefolded.
        mock_api.async_login.assert_awaited_once_with("OWNER@EXAMPLE.INVALID", PASSWORD)
        mock_api.async_list_vehicles.assert_awaited_once()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"selected_vins": [VIN_2]}
        )
        schema = assert_native_form(result, "pin")
        pin_selector = schema_value(schema, "control_pin")
        assert isinstance(pin_selector, selector.TextSelector)
        assert pin_selector.config["type"] == "password"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"control_pin": PIN}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        data = result["data"]
        assert data["email"] == "OWNER@EXAMPLE.INVALID"
        assert data["tokens"] == tokens.to_dict()
        assert data["selected_vins"] == [VIN_2]
        assert data["control_pin"] == PIN
        assert "password" not in data
        assert PASSWORD not in repr(result)
        assert result["result"].unique_id != EMAIL
        assert EMAIL not in result["result"].unique_id
        mock_api.async_realtime.assert_not_awaited()
        for secret in (PASSWORD, PIN, tokens.access_token, tokens.refresh_token):
            assert secret not in caplog.text


async def test_single_vehicle_skips_selection_and_pin_is_optional(
    hass, mock_api, vehicles
):
    mock_api.async_list_vehicles.return_value = vehicles[:1]
    with patch(f"custom_components.{DOMAIN}.async_setup_entry", return_value=True):
        result = await login(hass, await start(hass))
        assert_native_form(result, "pin")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"]["selected_vins"] == [VIN]
        assert not result["data"].get("control_pin")
        assert PASSWORD not in repr(result)


@pytest.mark.parametrize(
    "exception,error",
    [
        ("AuthenticationError", "invalid_auth"),
        ("CannotConnect", "cannot_connect"),
        ("RateLimitError", "rate_limited"),
    ],
)
async def test_login_errors(hass, mock_api, api_types, exception, error):
    mock_api.async_login.side_effect = getattr(api_types, exception)()
    result = await login(hass, await start(hass))
    assert_native_form(result, "user")
    assert result["errors"] == {"base": error}
    mock_api.async_list_vehicles.assert_not_awaited()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_no_vehicles(hass, mock_api):
    mock_api.async_list_vehicles.return_value = []
    result = await login(hass, await start(hass))
    assert_native_form(result, "user")
    assert result["errors"] == {"base": "no_vehicles"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_duplicate_account(hass, mock_api):
    with patch(f"custom_components.{DOMAIN}.async_setup_entry", return_value=True):
        result = await login(hass, await start(hass))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"selected_vins": [VIN]}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        result = await login(hass, await start(hass), "OWNER@EXAMPLE.INVALID")
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("single_vehicle", [False, True])
async def test_second_account_cannot_configure_same_physical_vehicle(
    hass, entry, mock_api, vehicles, single_vehicle
):
    entry.add_to_hass(hass)
    if single_vehicle:
        mock_api.async_list_vehicles.return_value = vehicles[:1]
    result = await login(hass, await start(hass), "delegate@example.invalid")
    if not single_vehicle:
        assert_native_form(result, "vehicles")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"selected_vins": [VIN]}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "vehicle_already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data["selected_vins"] == [VIN]
    mock_api.async_login.assert_awaited_once_with("delegate@example.invalid", PASSWORD)
    mock_api.async_realtime.assert_not_awaited()


async def test_invalid_vehicle_selection(hass, mock_api):
    result = await login(hass, await start(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_vins": []}
    )
    assert_native_form(result, "vehicles")
    assert result["errors"]
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"selected_vins": ["UNAUTHORIZEDVIN"]}
        )


async def test_reauth_fixed_account_preserves_pin(hass, mock_api, api_types, entry):
    entry.add_to_hass(hass)
    replacement = api_types.TokenSet(
        "replacement-access", "replacement-refresh", 4102444800.0
    )
    mock_api.async_login.return_value = replacement
    result = await entry.start_reauth_flow(hass)
    schema = assert_native_form(result, "reauth_confirm")
    assert "email" not in [str(key) for key in schema]
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": PASSWORD}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    mock_api.async_login.assert_awaited_once_with(EMAIL, PASSWORD)
    assert entry.data["tokens"] == replacement.to_dict()
    assert entry.data["control_pin"] == PIN
    assert entry.data["selected_vins"] == [VIN]
    assert PASSWORD not in repr(entry.data)
    reload.assert_awaited_once_with(entry.entry_id)


async def test_reauth_does_not_replace_tokens_for_missing_selected_vehicle(
    hass, mock_api, vehicles, entry
):
    entry.add_to_hass(hass)
    old_data = dict(entry.data)
    mock_api.async_list_vehicles.return_value = vehicles[1:]
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD}
    )
    assert result["type"] is not FlowResultType.CREATE_ENTRY
    assert entry.data == old_data


@pytest.mark.parametrize(
    "exception,error",
    [
        ("AuthenticationError", "invalid_auth"),
        ("CannotConnect", "cannot_connect"),
        ("RateLimitError", "rate_limited"),
    ],
)
async def test_reauth_error_keeps_saved_entry(
    hass, entry, mock_api, api_types, exception, error
):
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await entry.start_reauth_flow(hass)
    mock_api.async_login.side_effect = getattr(api_types, exception)()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": PASSWORD}
    )
    schema = assert_native_form(result, "reauth_confirm")
    assert result["errors"] == {"base": error}
    assert entry.data == before
    assert PASSWORD not in repr(schema)
    assert PIN not in repr(schema)


async def test_optional_pin_is_locally_validated_without_api_verification(
    hass, mock_api
):
    result = await login(hass, await start(hass))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"selected_vins": [VIN]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"control_pin": "not-a-pin"}
    )
    assert_native_form(result, "pin")
    assert result["errors"] == {"control_pin": "invalid_pin"}
    mock_api.async_realtime.assert_not_awaited()
    assert mock_api.async_login.await_count == 1


async def test_options_poll_interval(hass, entry, mock_api):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert_native_form(result, "init")
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"poll_interval": 15}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["poll_interval"] == 15
    mock_api.async_login.assert_not_awaited()
    mock_api.async_realtime.assert_not_awaited()


@pytest.mark.parametrize("value", [4, 61])
async def test_poll_interval_schema_bounds(hass, entry, value):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert_native_form(result, "init")
    with pytest.raises(p.Invalid):
        result["data_schema"]({"poll_interval": value})


async def test_options_reject_fractional_interval(hass, entry):
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 5.5}
    )
    assert_native_form(result, "init")
    assert result["errors"] == {"poll_interval": "invalid_interval"}
    assert not entry.options


@pytest.mark.parametrize("pin", ["1357", ""])
async def test_reconfigure_pin_update_and_clear(hass, entry, mock_api, pin):
    entry.add_to_hass(hass)
    before = dict(entry.data)
    result = await entry.start_reconfigure_flow(hass)
    schema = assert_native_form(result, "reconfigure")
    assert schema_value(schema, "control_pin").config["type"] == "password"
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"control_pin": pin, "clear_pin": not pin}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data.get("control_pin", "") == pin
    for key in ("email", "tokens", "vehicles", "selected_vins"):
        assert entry.data[key] == before[key]
    mock_api.async_login.assert_not_awaited()
    mock_api.async_realtime.assert_not_awaited()
