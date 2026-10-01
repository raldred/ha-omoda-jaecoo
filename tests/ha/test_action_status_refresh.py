"""Bounded climate reads and lock-state evidence regressions, no network."""

import asyncio
from datetime import timedelta

import pytest
from conftest import PIN, VIN
from homeassistant.util import dt as dt_util
from test_lock_feedback import finish, setup

from custom_components.omoda_jaecoo import coordinator as coordinator_module
from custom_components.omoda_jaecoo.api import RateLimitError


@pytest.fixture(autouse=True)
def fast_checks(monkeypatch):
    monkeypatch.setattr(coordinator_module, "LOCK_STATUS_DELAYS", (0,) * 5)


async def test_climate_refreshes_are_bounded_and_never_resend(hass, entry, mock_api):
    coord, client = await setup(hass, entry, mock_api)
    mock_api.async_realtime.return_value = {"frontHVACState": 1, "inCarTemperature": 22}
    await coord.async_climate(VIN, True, 21)
    task = coord._climate_followup_tasks[VIN]
    await asyncio.wait_for(task, 2)
    await hass.async_block_till_done()
    assert mock_api.async_realtime.await_count == 5
    client.async_climate.assert_awaited_once_with(VIN, PIN, True, 21, 15)
    client.async_lock.assert_not_awaited()
    mock_api.async_control_session.assert_not_awaited()
    assert coord.data[VIN].climate_on is True
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    assert coord.update_interval == timedelta(minutes=5)
    assert not coord._climate_followup_tasks


async def test_climate_burst_stops_at_rate_limit(hass, entry, mock_api):
    coord, client = await setup(hass, entry, mock_api)
    mock_api.async_realtime.side_effect = RateLimitError("synthetic rate limit")
    await coord.async_climate(VIN, False, 21)
    await asyncio.wait_for(coord._climate_followup_tasks[VIN], 2)
    assert mock_api.async_realtime.await_count == 1
    assert coord.update_interval == timedelta(minutes=10)
    assert coord.last_command_status[VIN] == "accepted_unconfirmed"
    client.async_climate.assert_awaited_once()


async def test_unload_cancels_climate_reads_before_first_execution(
    hass, entry, mock_api
):
    coord, client = await setup(hass, entry, mock_api)
    await coord.async_climate(VIN, True, 21)
    task = coord._climate_followup_tasks[VIN]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert task.cancelled()
    assert not coord._climate_followup_tasks
    assert mock_api.async_realtime.await_count == 0
    client.async_climate.assert_awaited_once()


async def test_lock_check_cancelled_before_execution_clears_spinner(
    hass, entry, mock_api
):
    coord, client = await setup(hass, entry, mock_api)
    await coord.async_lock(VIN, False)
    task = coord._lock_followup_tasks[VIN]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await hass.async_block_till_done()
    assert not coord._lock_followup_tasks
    assert coord.pending_lock_target(VIN) is None
    assert coord.lock_state_uncertain(VIN)
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert mock_api.async_realtime.await_count == 0
    client.async_lock.assert_awaited_once()


async def test_old_matching_frame_does_not_inherit_prior_freshness(
    hass, entry, mock_api
):
    coord, client = await setup(hass, entry, mock_api)
    reads = 0

    async def data(*args):
        nonlocal reads
        reads += 1
        if reads == 1:
            return {"doorLock": 0, "timestamp": dt_util.utcnow().isoformat()}
        return {
            "doorLock": 1,
            "timestamp": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
        }

    mock_api.async_realtime.side_effect = data
    await coord.async_lock(VIN, False)
    await finish(coord, hass)
    assert reads == 5
    assert coord.last_command_status[VIN] == "confirmation_timeout"
    assert coord.lock_state_uncertain(VIN)
    assert not coord._lock_requests[VIN].report_known
    client.async_lock.assert_awaited_once()
