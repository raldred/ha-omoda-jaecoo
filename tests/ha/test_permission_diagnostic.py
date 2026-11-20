"""Native admin diagnostic action, with every network and actuator path mocked."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from conftest import DOMAIN, VIN
from homeassistant.core import Context
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omoda_jaecoo import services

ACTION = "diagnose_rear_defrost_permissions"


async def setup(hass, entry, mock_api, tokens, vehicles):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mock_api._tokens = mock_api._tsp_account = tokens
    mock_api._user_token = "private-user-token"
    mock_api._tuser_id = "private-user-id"
    mock_api._tsp_login_confirmed = True
    mock_api.get_vehicle.return_value = vehicles[0]
    mock_api.reset_mock()
    return entry.runtime_data


async def call(hass, entry_id, **kwargs):
    return await hass.services.async_call(DOMAIN, ACTION, {"config_entry_id": entry_id},
                                         blocking=True, return_response=True, **kwargs)


async def test_registration_even_with_refresh_already_registered(hass):
    hass.services.async_register(DOMAIN, "refresh_status", AsyncMock())
    services.async_register_actions(hass)
    assert hass.services.has_service(DOMAIN, ACTION)
    services.async_register_actions(hass)
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, ACTION, {}, blocking=True)
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, ACTION,
                                      {"config_entry_id": "missing", "vin": VIN}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await call(hass, "missing")


async def test_wrong_domain_and_unloaded_targets_rejected(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    for domain in (DOMAIN, "another_integration"):
        entry = MockConfigEntry(domain=domain)
        entry.add_to_hass(hass)
        with pytest.raises(ServiceValidationError):
            await call(hass, entry.entry_id)


async def test_admin_read_safe_response_no_auth_refresh_pin_actuators(
    hass, entry, mock_api, tokens, vehicles, hass_admin_user, caplog
):
    coordinator = await setup(hass, entry, mock_api, tokens, vehicles)
    result = {"code": "000000", "data": {"permissionList": [
        {"id": 232, "state": 0}, {"id": 20411, "state": "private-secret"},
    ]}, "message": "private-secret"}
    with (
        patch.object(coordinator.command_client, "_bff", new_callable=AsyncMock, return_value=result) as bff,
        patch.object(coordinator.command_client, "_task_id", new_callable=AsyncMock) as pin,
        patch.object(coordinator.command_client, "_submit", new_callable=AsyncMock) as actuator,
        patch.object(coordinator, "async_refresh", new_callable=AsyncMock) as refresh,
    ):
        response = await call(hass, entry.entry_id, context=Context(user_id=hass_admin_user.id))
        bff.assert_awaited_once()
        assert bff.call_args.args[0] == "authority"
        assert bff.call_args.args[2] == {"vin": VIN, "tUserId": "private-user-id", "channelId": "1"}
        pin.assert_not_called()
        actuator.assert_not_called()
        refresh.assert_not_called()
    for method in (mock_api._ensure_tsp_login, mock_api._ensure_tokens, mock_api._refresh,
                   mock_api.async_control_session, mock_api.async_login, mock_api.async_realtime,
                   mock_api.async_login_phone, mock_api.async_login_otp, mock_api.async_request_otp):
        method.assert_not_called()
    report = response["vehicles"][0]["permissions"]
    assert report["232"]["local"]["status"] == "denied"
    assert report["2321"]["local"]["status"] == "missing"
    assert report["20411"]["local"]["status"] == "unknown"
    assert "REAR_DEFROST_PERMISSION_SNAPSHOT" in caplog.text
    for secret in ("private-secret", "private-user-token", "private-user-id", tokens.access_token, VIN):
        assert secret not in str(response)
        assert secret not in caplog.text


async def test_nonadmin_and_missing_session_never_query(
    hass, entry, mock_api, tokens, vehicles, hass_admin_user
):
    coordinator = await setup(hass, entry, mock_api, tokens, vehicles)
    user = await hass.auth.async_create_user("Non-admin")
    assert not user.is_admin
    with patch.object(coordinator.command_client, "_bff", new_callable=AsyncMock) as bff:
        with pytest.raises(Unauthorized):
            await call(hass, entry.entry_id, context=Context(user_id=user.id))
        mock_api._tuser_id = None
        with pytest.raises(HomeAssistantError):
            await call(hass, entry.entry_id)
        bff.assert_not_called()
    mock_api.async_control_session.assert_not_called()
    mock_api._ensure_tsp_login.assert_not_called()
    mock_api._refresh.assert_not_called()


async def test_backend_exception_redacted_and_no_retry(
    hass, entry, mock_api, tokens, vehicles, caplog
):
    coordinator = await setup(hass, entry, mock_api, tokens, vehicles)
    from custom_components.omoda_jaecoo.api import ApiError
    with patch.object(coordinator.command_client, "_bff", new_callable=AsyncMock,
                      side_effect=ApiError("private-secret")) as bff:
        with pytest.raises(HomeAssistantError) as error:
            await call(hass, entry.entry_id)
        assert "private-secret" not in str(error.value)
        assert "private-secret" not in caplog.text
        bff.assert_awaited_once()


async def test_unloaded_action_remains_registered(hass, entry, mock_api, tokens, vehicles):
    await setup(hass, entry, mock_api, tokens, vehicles)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.services.has_service(DOMAIN, ACTION)
    with pytest.raises(ServiceValidationError):
        await call(hass, entry.entry_id)
