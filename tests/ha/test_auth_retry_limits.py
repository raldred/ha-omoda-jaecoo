"""Server auth backoff applies to verification/password attempts, not only resends."""

import asyncio

import pytest
from conftest import EMAIL, PASSWORD
from test_auth_flows import (
    configure,
    credentials,
    identity_input,
    otp_form,
)
from test_config_flow import assert_native_form

from custom_components.omoda_jaecoo import config_flow as flow_module


async def test_slow_code_request_cannot_be_duplicated(hass, auth_api, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(flow_module, "monotonic", lambda: clock[0])
    started, release = asyncio.Event(), asyncio.Event()

    async def held(*args):
        started.set()
        await release.wait()

    auth_api.async_request_otp.side_effect = held
    flow = flow_module.OmodaJaecooConfigFlow()
    flow.hass = hass
    flow._api = auth_api
    flow._identifier = EMAIL
    task = asyncio.create_task(flow._async_send_code(resend=False))
    try:
        await started.wait()
        clock[0] += 120
        blocked = await flow._async_send_code(resend=False)
        assert blocked["errors"] == {"base": "resend_cooldown"}
        auth_api.async_request_otp.assert_awaited_once()
    finally:
        release.set()
        await task
    assert not flow._code_send_in_progress
    blocked = await flow._async_send_code(resend=True)
    assert blocked["errors"] == {"base": "resend_cooldown"}
    auth_api.async_request_otp.assert_awaited_once()


@pytest.mark.parametrize("account", ["email", "phone"])
async def test_password_retry_after_blocks_repeated_submit(
    hass, auth_api, api_types, tokens, monkeypatch, account
):
    clock = [1000.0]
    monkeypatch.setattr(flow_module, "monotonic", lambda: clock[0])
    method = auth_api.async_login if account == "email" else auth_api.async_login_phone
    method.side_effect = [api_types.RateLimitError("limited", retry_after=300), tokens]
    result = await credentials(hass, account, "password")
    data = {**identity_input(account), "password": PASSWORD}
    result = await configure(hass, result, data)
    assert result["errors"] == {"base": "rate_limited"}
    result = await configure(hass, result, data)
    assert result["errors"] == {"base": "rate_limited"}
    assert method.await_count == 1
    clock[0] += 301
    result = await configure(hass, result, data)
    assert_native_form(result, "vehicles")
    assert method.await_count == 2
    auth_api.async_request_otp.assert_not_awaited()


async def test_otp_verify_honors_retry_after_without_automatic_resend(
    hass, auth_api, api_types, tokens, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(flow_module, "monotonic", lambda: clock[0])
    auth_api.async_login_otp.side_effect = [
        api_types.RateLimitError("limited", retry_after=300),
        tokens,
    ]
    result = await otp_form(hass)
    data = {"otp": "001234", "otp_action": "verify"}
    result = await configure(hass, result, data)
    assert result["errors"] == {"base": "rate_limited"}
    clock[0] += 100
    result = await configure(hass, result, data)
    assert result["errors"] == {"base": "rate_limited"}
    auth_api.async_login_otp.assert_awaited_once()
    auth_api.async_request_otp.assert_awaited_once()
    clock[0] += 201
    result = await configure(hass, result, data)
    assert_native_form(result, "vehicles")
    assert auth_api.async_login_otp.await_count == 2
    auth_api.async_request_otp.assert_awaited_once()
