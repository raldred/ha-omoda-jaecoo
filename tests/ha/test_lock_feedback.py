"""Native HA progress and bounded passive confirmation, entirely offline.

The only write is a fake initial CommandClient call. Socket-blocking fixtures
also forbid accidentally reaching HA, the account service, or a real vehicle.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from conftest import DOMAIN, PIN, VIN, VIN_2
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from test_control_entities import entity_id, state

from custom_components.omoda_jaecoo import coordinator as coordinator_module
from custom_components.omoda_jaecoo.api import (
    ApiError,
    AuthenticationError,
    RateLimitError,
)
from custom_components.omoda_jaecoo.commands import (
    CommandCancelled,
    CommandClient,
    CommandError,
    CommandOutcomeUnknown,
    PinVerificationCancelled,
    PinVerificationError,
)
from custom_components.omoda_jaecoo.lock import OmodaJaecooLock


@pytest.fixture
def fast_followup(monkeypatch):
    """Keep the real coordinator/refresh scheduling; shorten only its delays."""
    monkeypatch.setattr(coordinator_module, "LOCK_STATUS_DELAYS", (0,) * 5)


async def setup(hass, entry, mock_api, *, initial=None, both=False):
    mock_api.async_realtime.return_value = (
        {"doorLock": 0} if initial is None else initial
    )
    entry.add_to_hass(hass)
    if both:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "selected_vins": [VIN, VIN_2]}
        )
    hass.config_entries.async_update_entry(entry, options={"enable_controls": True})
    client = AsyncMock(spec=CommandClient)
    with (
        patch(f"custom_components.{DOMAIN}.CommandClient", return_value=client),
        patch(f"custom_components.{DOMAIN}.PLATFORMS", [Platform.LOCK]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    mock_api.async_realtime.reset_mock()
    return entry.runtime_data, client


async def finish(coord, hass):
    tasks = tuple(coord._lock_followup_tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    await hass.async_block_till_done()
    assert not coord._lock_followup_tasks


def no_extra_writes(client, mock_api, *, locked=False):
    client.async_lock.assert_awaited_once_with(VIN, PIN, locked)
    client.async_climate.assert_not_awaited()
    mock_api.async_control_session.assert_not_awaited()
    mock_api.async_login.assert_not_awaited()


@pytest.mark.parametrize("locked", [False, True])
async def test_native_progress_before_command_returns(
    hass, entry, mock_api, fast_followup, locked
):
    coord, client = await setup(
        hass, entry, mock_api, initial={"doorLock": int(locked)}
    )
    started, release, reading, release_read = (asyncio.Event() for _ in range(4))

    async def held_command(*args):
        started.set()
        await release.wait()

    async def held_read(*args):
        reading.set()
        await release_read.wait()
        return {"doorLock": int(not locked)}

    client.async_lock.side_effect = held_command
    mock_api.async_realtime.side_effect = held_read
    task = asyncio.create_task(
        hass.services.async_call(
            "lock",
            "lock" if locked else "unlock",
            {"entity_id": entity_id(hass, "lock", "lock")},
            blocking=True,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        native = OmodaJaecooLock(coord, VIN)
        assert native.is_locking is locked
        assert native.is_unlocking is (not locked)
        assert state(hass, "lock", "lock").state == (
            "locking" if locked else "unlocking"
        )
        assert coord.data[VIN].door_locked is (not locked)
        assert coord.last_command_status[VIN] == "submitting"
        assert mock_api.async_realtime.await_count == 0
        release.set()
        # Acceptance must return while the first passive read is still blocked.
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(reading.wait(), 2)
        assert coord.last_command_status[VIN] == "accepted_unconfirmed"
        assert state(hass, "lock", "lock").state == (
            "locking" if locked else "unlocking"
        )
        assert coord.data[VIN].door_locked is (not locked)
    finally:
        release.set()
        release_read.set()
        await task
        await finish(coord, hass)
    assert state(hass, "lock", "lock").state == ("locked" if locked else "unlocked")
    assert coord.last_command_status[VIN] == "state_observed"
    assert mock_api.async_realtime.await_count == 1
    assert coord.update_interval == timedelta(minutes=5)
    no_extra_writes(client, mock_api, locked=locked)


@pytest.mark.parametrize(
    "initial,reply",
    [
        ({"doorLock": 1}, {"doorLock": 1}),  # Already at target, no source time.
        ({"doorLock": 0}, {"doorLock": 0}),  # Unchanged prior value, no time.
        ({}, {"doorLock": 1}),  # Unknown prior cannot prove a change.
        ({"doorLock": 0}, {}),
        ({"doorLock": 0}, {"odometer": 100}),
        ({"doorLock": 0}, {"doorLock": 1, "timestamp": "old"}),
        ({"doorLock": 0}, {"doorLock": 1, "timestamp": "future"}),
        (
            {"doorLock": 0},
            {
                "doorLock": 1,
                "dumpEnergy": 0,
                "electricRange": 0,
                "totalCurrent": -1000,
                "totalVoltage": 0,
            },
        ),
    ],
)
async def test_cached_missing_or_stale_reports_are_not_confirmation(
    hass, entry, mock_api, fast_followup, initial, reply
):
    coord, client = await setup(hass, entry, mock_api, initial=initial)
    reply = dict(reply)
    if reply.get("timestamp") == "old":
        reply["timestamp"] = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    elif reply.get("timestamp") == "future":
        reply["timestamp"] = (dt_util.utcnow() + timedelta(minutes=1)).isoformat()
    mock_api.async_realtime.return_value = reply
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert mock_api.async_realtime.await_count == 5
    assert coord.pending_lock_target(VIN) is None
    assert coord.lock_state_uncertain(VIN)
    assert state(hass, "lock", "lock").state == "unknown"
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert not coord._lock_requests[VIN].state_observed
    assert coord.update_interval == timedelta(minutes=5)
    no_extra_writes(client, mock_api)


@pytest.mark.parametrize("initial", [{"doorLock": 1}, {}])
async def test_fresh_timestamp_confirms_even_unchanged_or_unknown_prior(
    hass, entry, mock_api, fast_followup, initial
):
    coord, client = await setup(hass, entry, mock_api, initial=initial)

    async def fresh(*args):
        return {"doorLock": 1, "timestamp": dt_util.utcnow().isoformat()}

    mock_api.async_realtime.side_effect = fresh
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert mock_api.async_realtime.await_count == 1
    assert coord.pending_lock_target(VIN) is None
    assert not coord.lock_state_uncertain(VIN)
    assert coord.last_command_status[VIN] == "state_observed"
    assert state(hass, "lock", "lock").state == "unlocked"
    no_extra_writes(client, mock_api)


async def test_credible_opposite_state_remains_known_after_budget(
    hass, entry, mock_api, fast_followup
):
    coord, client = await setup(hass, entry, mock_api)

    async def fresh_opposite(*args):
        return {"doorLock": 0, "timestamp": dt_util.utcnow().isoformat()}

    mock_api.async_realtime.side_effect = fresh_opposite
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert mock_api.async_realtime.await_count == 5
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert coord._lock_requests[VIN].report_known
    assert not coord.lock_state_uncertain(VIN)
    assert state(hass, "lock", "lock").state == "locked"
    no_extra_writes(client, mock_api)


async def test_later_ordinary_poll_recovers_without_command_or_pin_check(
    hass, entry, mock_api, fast_followup
):
    coord, client = await setup(hass, entry, mock_api)
    mock_api.async_realtime.return_value = {}
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert state(hass, "lock", "lock").state == "unknown"
    mock_api.async_realtime.return_value = {
        "doorLock": 1,
        "timestamp": dt_util.utcnow().isoformat(),
    }
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert state(hass, "lock", "lock").state == "unlocked"
    assert coord.last_command_status[VIN] == "state_observed"
    assert not coord.lock_state_uncertain(VIN)
    assert mock_api.async_realtime.await_count == 6
    no_extra_writes(client, mock_api)


@pytest.mark.parametrize("error", [ApiError, RateLimitError, AuthenticationError])
async def test_failed_passive_read_aborts_without_retry(
    hass, entry, mock_api, fast_followup, error
):
    coord, client = await setup(hass, entry, mock_api)
    mock_api.async_realtime.side_effect = error("offline test failure")
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert mock_api.async_realtime.await_count == 1
    assert coord.pending_lock_target(VIN) is None
    assert coord.lock_state_uncertain(VIN)
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert not coord.last_update_success
    if error is RateLimitError:
        assert coord.update_interval == timedelta(minutes=10)
    no_extra_writes(client, mock_api)


@pytest.mark.parametrize(
    "error,status",
    [
        (CommandError, "rejected"),
        (CommandOutcomeUnknown, "unknown_outcome"),
        (PinVerificationError, "pin_blocked"),
        (CommandCancelled, "unknown_outcome"),
        (PinVerificationCancelled, "pin_blocked"),
        (asyncio.CancelledError, "not_sent"),
    ],
)
async def test_unsuccessful_send_clears_progress_without_passive_reads(
    hass, entry, mock_api, fast_followup, error, status
):
    coord, client = await setup(hass, entry, mock_api)
    client.async_lock.side_effect = error()
    with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
        await coord.async_lock(VIN, False)
    await hass.async_block_till_done()
    assert coord.pending_lock_target(VIN) is None
    ambiguous = status == "unknown_outcome"
    assert coord.lock_state_uncertain(VIN) is ambiguous
    assert not coord._lock_followup_tasks
    assert coord.last_command_status[VIN] == status
    assert state(hass, "lock", "lock").state == ("unknown" if ambiguous else "locked")
    assert not coord._command_lock.locked()
    assert mock_api.async_realtime.await_count == 0
    no_extra_writes(client, mock_api)


async def test_followup_refreshes_all_selected_vehicles_normally(
    hass, entry, mock_api, fast_followup
):
    coord, client = await setup(hass, entry, mock_api, both=True)
    mock_api.async_realtime.return_value = {"doorLock": 1}
    with patch.object(coord, "async_refresh", wraps=coord.async_refresh) as refresh:
        await coord.async_lock(VIN, False)
        await finish(coord, hass)
    refresh.assert_awaited_once()
    assert [call.args[0] for call in mock_api.async_realtime.await_args_list] == [
        VIN,
        VIN_2,
    ]
    no_extra_writes(client, mock_api)


@pytest.mark.parametrize("unload", [False, True])
async def test_cancellation_stops_pending_reads_and_spinner(
    hass, entry, mock_api, fast_followup, unload
):
    coord, client = await setup(hass, entry, mock_api)
    reading, cancelled = asyncio.Event(), asyncio.Event()

    async def held(*args):
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock_api.async_realtime.side_effect = held
    await coord.async_lock(VIN, False)
    await asyncio.wait_for(reading.wait(), 2)
    tasks = tuple(coord._lock_followup_tasks.values())
    if unload:
        with patch(f"custom_components.{DOMAIN}.PLATFORMS", [Platform.LOCK]):
            assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        tasks[0].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await hass.async_block_till_done()
    assert cancelled.is_set()
    assert not coord._lock_followup_tasks
    assert coord.pending_lock_target(VIN) is None
    assert coord.lock_state_uncertain(VIN)
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert mock_api.async_realtime.await_count == 1
    if unload:
        # HA retains the entity registry placeholder after successful unload.
        assert state(hass, "lock", "lock").state == "unavailable"
    else:
        assert not OmodaJaecooLock(coord, VIN).is_unlocking
    no_extra_writes(client, mock_api)


async def test_overall_deadline_cancels_hung_read(
    hass, entry, mock_api, fast_followup, monkeypatch
):
    monkeypatch.setattr(coordinator_module, "LOCK_STATUS_TIMEOUT", 0.02)
    coord, client = await setup(hass, entry, mock_api)
    cancelled = asyncio.Event()

    async def held(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    mock_api.async_realtime.side_effect = held
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert cancelled.is_set()
    assert mock_api.async_realtime.await_count == 1
    assert coord.pending_lock_target(VIN) is None
    assert coord.lock_state_uncertain(VIN)
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    no_extra_writes(client, mock_api)


async def test_superseded_request_cannot_clear_new_pending_or_status(
    hass, entry, mock_api, fast_followup
):
    coord, client = await setup(hass, entry, mock_api)
    first_read, second_read, release_first, release_second = (
        asyncio.Event() for _ in range(4)
    )
    reads = 0

    async def held(*args):
        nonlocal reads
        reads += 1
        if reads == 1:
            first_read.set()
            await release_first.wait()
            return {"doorLock": 1, "timestamp": dt_util.utcnow().isoformat()}
        second_read.set()
        await release_second.wait()
        return {"doorLock": 0, "timestamp": dt_util.utcnow().isoformat()}

    mock_api.async_realtime.side_effect = held
    await coord.async_lock(VIN, False)
    old_task = coord._lock_followup_tasks[VIN]
    await asyncio.wait_for(first_read.wait(), 2)
    coord._last_command_at = None  # Two deliberate users, not a follow-up retry.
    try:
        await coord.async_lock(VIN, True)
        replacement = coord._lock_requests[VIN]
        new_task = coord._lock_followup_tasks[VIN]
        release_first.set()
        await asyncio.wait_for(second_read.wait(), 2)
        await asyncio.wait_for(old_task, 2)
        assert coord._lock_requests[VIN] is replacement
        assert coord._lock_followup_tasks[VIN] is new_task
        assert coord.pending_lock_target(VIN) is True
        assert coord.last_command_status[VIN] == "accepted_unconfirmed"
        assert state(hass, "lock", "lock").state == "locking"
    finally:
        release_first.set()
        release_second.set()
        await finish(coord, hass)
    assert coord.last_command_status[VIN] == "state_observed"
    assert state(hass, "lock", "lock").state == "locked"
    assert mock_api.async_realtime.await_count == 2
    assert client.async_lock.await_count == 2
    assert [call.args for call in client.async_lock.await_args_list] == [
        (VIN, PIN, False),
        (VIN, PIN, True),
    ]
    client.async_climate.assert_not_awaited()
    mock_api.async_control_session.assert_not_awaited()
    mock_api.async_login.assert_not_awaited()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_newer_climate_status_not_overwritten_by_lock_followup(
    hass, entry, mock_api, fast_followup, confirmed
):
    coord, client = await setup(hass, entry, mock_api)
    reading, release = asyncio.Event(), asyncio.Event()

    async def held(*args):
        reading.set()
        await release.wait()
        return (
            {"doorLock": 1, "timestamp": dt_util.utcnow().isoformat()}
            if confirmed
            else {}
        )

    mock_api.async_realtime.side_effect = held
    await coord.async_lock(VIN, False)
    await asyncio.wait_for(reading.wait(), 2)
    coord._last_command_at = None
    # Climate's own passive read burst is isolated in this lock-status test.
    with patch.object(coord, "_start_climate_followup"):
        await coord.async_climate(VIN, True, 21)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    release.set()
    await finish(coord, hass)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    assert coord.pending_lock_target(VIN) is None
    assert coord._lock_requests[VIN].state_observed is confirmed
    assert state(hass, "lock", "lock").state == ("unlocked" if confirmed else "unknown")
    assert mock_api.async_realtime.await_count == (1 if confirmed else 5)
    client.async_lock.assert_awaited_once_with(VIN, PIN, False)
    client.async_climate.assert_awaited_once_with(VIN, PIN, True, 21, 15)
    mock_api.async_control_session.assert_not_awaited()
    mock_api.async_login.assert_not_awaited()
