"""Native refresh action: explicit account target, permissions and no commands."""

import asyncio
from unittest.mock import patch

import pytest
import voluptuous as vol
from conftest import DOMAIN, VIN_2
from homeassistant.core import Context
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.omoda_jaecoo import services


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def call(hass, entry_id, **kwargs):
    await hass.services.async_call(
        DOMAIN, "refresh_status", {"config_entry_id": entry_id}, blocking=True, **kwargs
    )


async def test_action_registered_without_entries_and_requires_target(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "refresh_status")
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, "refresh_status", {}, blocking=True)
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, "missing-entry")
    assert error.value.translation_key == "invalid_refresh_target"


async def test_refresh_reads_once_without_auth_or_vehicle_commands(
    hass, entry, mock_api
):
    await setup(hass, entry)
    mock_api.async_realtime.reset_mock()
    await call(hass, entry.entry_id)
    mock_api.async_realtime.assert_awaited_once()
    mock_api.async_login.assert_not_awaited()
    mock_api.async_login_phone.assert_not_awaited()
    mock_api.async_login_otp.assert_not_awaited()
    mock_api.async_request_otp.assert_not_awaited()
    mock_api.async_control_session.assert_not_awaited()
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, entry.entry_id)
    assert error.value.translation_key == "refresh_cooldown"
    assert mock_api.async_realtime.await_count == 1


async def test_action_stays_registered_after_unload(hass, entry, mock_api):
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "refresh_status")
    mock_api.async_realtime.reset_mock()
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, entry.entry_id)
    assert error.value.translation_key == "refresh_target_unloaded"
    mock_api.async_realtime.assert_not_awaited()


async def test_wrong_integration_cannot_be_targeted(hass):
    other = MockConfigEntry(domain="some_other_integration")
    other.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, other.entry_id)
    assert error.value.translation_key == "invalid_refresh_target"


async def test_explicit_nonadmin_is_denied(hass, entry, mock_api, hass_admin_user):
    await setup(hass, entry)
    user = await hass.auth.async_create_user("limited test user")
    assert not user.is_admin
    mock_api.async_realtime.reset_mock()
    with pytest.raises(Unauthorized):
        await call(hass, entry.entry_id, context=Context(user_id=user.id))
    mock_api.async_realtime.assert_not_awaited()


async def test_admin_can_request_refresh(hass, entry, mock_api, hass_admin_user):
    await setup(hass, entry)
    mock_api.async_realtime.reset_mock()
    await call(hass, entry.entry_id, context=Context(user_id=hass_admin_user.id))
    mock_api.async_realtime.assert_awaited_once()


async def test_failed_refresh_surfaces_error_not_success(
    hass, entry, mock_api, api_types
):
    await setup(hass, entry)
    mock_api.async_realtime.side_effect = api_types.CannotConnect("safe test failure")
    with pytest.raises(HomeAssistantError) as error:
        await call(hass, entry.entry_id)
    assert error.value.translation_key == "refresh_failed"
    mock_api.async_request_otp.assert_not_awaited()


async def test_duplicate_inflight_refresh_is_not_queued(hass, entry, mock_api):
    await setup(hass, entry)
    started, release = asyncio.Event(), asyncio.Event()

    async def held(*args):
        started.set()
        await release.wait()
        return {"dumpEnergy": 70}

    mock_api.async_realtime.reset_mock()
    mock_api.async_realtime.side_effect = held
    task = asyncio.create_task(call(hass, entry.entry_id))
    try:
        await started.wait()
        with pytest.raises(ServiceValidationError) as error:
            await call(hass, entry.entry_id)
        assert error.value.translation_key == "refresh_in_progress"
    finally:
        release.set()
        await task
    assert mock_api.async_realtime.await_count == 1


async def test_service_only_refreshes_selected_account(hass, entry, mock_api):
    first = await setup(hass, entry)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Second test account",
        unique_id="another-account",
        data={**entry.data, "selected_vins": [VIN_2]},
    )
    second = await setup(hass, other)
    with (
        patch.object(
            first, "async_refresh", wraps=first.async_refresh
        ) as first_refresh,
        patch.object(
            second, "async_refresh", wraps=second.async_refresh
        ) as second_refresh,
    ):
        await call(hass, entry.entry_id)
        first_refresh.assert_awaited_once()
        second_refresh.assert_not_awaited()


async def test_manual_cooldown_expires_without_touching_vehicle_controls(
    hass, entry, mock_api, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(services, "monotonic", lambda: clock[0])
    await setup(hass, entry)
    mock_api.async_realtime.reset_mock()
    await call(hass, entry.entry_id)
    clock[0] += 11
    await call(hass, entry.entry_id)
    assert mock_api.async_realtime.await_count == 2
    mock_api.async_control_session.assert_not_awaited()
