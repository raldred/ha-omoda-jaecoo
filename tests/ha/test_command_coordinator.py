"""Real coordinator guards around mocked commands; never touch a vehicle."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from conftest import DOMAIN, PIN, VIN
from homeassistant.exceptions import HomeAssistantError

from custom_components.omoda_jaecoo.commands import (
    CommandCancelled,
    CommandClient,
    CommandError,
    CommandOutcomeUnknown,
    PinVerificationCancelled,
    PinVerificationError,
)
from custom_components.omoda_jaecoo.coordinator import OmodaJaecooCoordinator


async def setup(hass, entry, mock_api, *, enabled=True, pin=True, blocked=False):
    entry.add_to_hass(hass)
    data = dict(entry.data)
    if not pin:
        data.pop("control_pin", None)
    if blocked:
        data["pin_blocked"] = True
    hass.config_entries.async_update_entry(
        entry, data=data, options={"enable_controls": enabled}
    )
    client = AsyncMock(spec=CommandClient)
    with patch(f"custom_components.{DOMAIN}.CommandClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coord = entry.runtime_data
    coord.async_request_refresh = AsyncMock()
    client.async_lock.assert_not_awaited()
    client.async_climate.assert_not_awaited()
    return coord, client


@pytest.mark.parametrize(
    "settings", [{"enabled": False}, {"pin": False}, {"blocked": True}]
)
async def test_explicit_opt_in_and_pin_are_required(hass, entry, mock_api, settings):
    coord, client = await setup(hass, entry, mock_api, **settings)
    with pytest.raises(HomeAssistantError):
        await coord.async_lock(VIN, True)
    client.async_lock.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


async def test_accepted_request_is_not_confirmed_state(hass, entry, mock_api):
    mock_api.async_realtime.return_value = {"doorLock": "1", "frontHVACState": "0"}
    coord, client = await setup(hass, entry, mock_api)
    await coord.async_lock(VIN, True)
    client.async_lock.assert_awaited_once_with(VIN, PIN, True)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    assert coord.data[VIN].door_locked is False
    coord.async_request_refresh.assert_awaited_once()
    with pytest.raises(HomeAssistantError, match="30 seconds"):
        await coord.async_lock(VIN, False)
    assert client.async_lock.await_count == 1


async def test_climate_passes_configured_duration(hass, entry, mock_api):
    coord, client = await setup(hass, entry, mock_api)
    # Test an existing configured option without invoking the HA reload listener.
    coord.entry = type(
        "EntryView",
        (),
        {
            "data": entry.data,
            "options": {"enable_controls": True, "climate_duration": 10},
        },
    )()
    await coord.async_climate(VIN, True, 21.5)
    client.async_climate.assert_awaited_once_with(VIN, PIN, True, 21.5, 10)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"


@pytest.mark.parametrize(
    "exception, status, blocked",
    [
        (PinVerificationError, "pin_blocked", True),
        (CommandError, "rejected", False),
        (CommandOutcomeUnknown, "unknown_outcome", False),
    ],
)
async def test_failed_commands_not_retried_or_confirmed(
    hass, entry, mock_api, exception, status, blocked
):
    coord, client = await setup(hass, entry, mock_api)
    client.async_lock.side_effect = exception("safe failure")
    with pytest.raises(HomeAssistantError):
        await coord.async_lock(VIN, True)
    assert coord.last_command_status[VIN] == status
    assert bool(entry.data.get("pin_blocked")) is blocked
    assert client.async_lock.await_count == 1
    coord.async_request_refresh.assert_not_awaited()
    if blocked:
        fresh = OmodaJaecooCoordinator(hass, entry, mock_api, client)
        assert not fresh.controls_available
        with pytest.raises(HomeAssistantError, match="paused"):
            await fresh.async_lock(VIN, True)
        assert client.async_lock.await_count == 1


@pytest.mark.parametrize(
    "exception, status, blocked",
    [
        (PinVerificationCancelled, "pin_blocked", True),
        (CommandCancelled, "unknown_outcome", False),
        (asyncio.CancelledError, "not_sent", False),
    ],
)
async def test_cancellation_preserved_with_honest_status(
    hass, entry, mock_api, exception, status, blocked
):
    coord, client = await setup(hass, entry, mock_api)
    client.async_lock.side_effect = exception()
    with pytest.raises(asyncio.CancelledError):
        await coord.async_lock(VIN, True)
    assert coord.last_command_status[VIN] == status
    assert bool(entry.data.get("pin_blocked")) is blocked
    assert not coord._command_lock.locked()
    assert client.async_lock.await_count == 1


async def test_concurrent_commands_are_not_queued_for_later_actuation(
    hass, entry, mock_api
):
    coord, client = await setup(hass, entry, mock_api)
    started, release = asyncio.Event(), asyncio.Event()

    async def held(*args):
        started.set()
        await release.wait()

    client.async_lock.side_effect = held
    task = asyncio.create_task(coord.async_lock(VIN, True))
    try:
        await started.wait()
        with pytest.raises(HomeAssistantError, match="in progress"):
            await coord.async_lock(VIN, False)
    finally:
        release.set()
        await task
    assert client.async_lock.await_count == 1


@pytest.mark.parametrize(
    "pin, blocked, reason",
    [(False, False, "pin_required"), (True, True, "pin_blocked")],
)
async def test_options_cannot_enable_without_usable_pin(
    hass, entry, mock_api, pin, blocked, reason
):
    await setup(hass, entry, mock_api, enabled=False, pin=pin, blocked=blocked)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "poll_interval": 5,
            "enable_controls": True,
            "climate_duration": 15,
        },
    )
    assert result["errors"] == {"base": reason}


async def test_reentering_pin_explicitly_clears_block_without_verification(
    hass, entry, mock_api
):
    _coord, client = await setup(hass, entry, mock_api, blocked=True)
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"control_pin": PIN}
        )
        await hass.async_block_till_done()
    assert result["type"] == "abort"
    assert not entry.data.get("pin_blocked", False)
    assert entry.data["control_pin"] == PIN
    client.async_lock.assert_not_awaited()
    client.async_climate.assert_not_awaited()
