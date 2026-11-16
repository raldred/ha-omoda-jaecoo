"""Offline cached-session diagnostic contracts. No external requests."""

import ast
import asyncio
import inspect
import json
from unittest.mock import AsyncMock, patch

import pytest
from test_commands import Response, VIN, authority, client, commands, load

diag = load("permission_diagnostic")


def test_diagnostic_network_call_is_statically_limited_to_authority():
    """Guard against later reuse of auth/preparation/actuation helpers."""
    tree = ast.parse(inspect.getsource(diag.diagnose_rear_defrost_permissions))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    method_calls = [node for node in calls if isinstance(node.func, ast.Attribute)]
    assert {node.func.attr for node in method_calls} == {"get_vehicle", "_bff"}
    bff = [node for node in method_calls if node.func.attr == "_bff"]
    assert len(bff) == 1
    assert isinstance(bff[0].args[0], ast.Constant)
    assert bff[0].args[0].value == "authority"
    helpers = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
    assert helpers == {"_cached_session", "permission_snapshot", "CommandError"}
    cache_tree = ast.parse(inspect.getsource(diag._cached_session))
    cache_methods = {node.func.attr for node in ast.walk(cache_tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert cache_methods == {"strip", "time"}



def test_only_authority_and_zero_auth_pin_or_actuator_calls():
    obj, session = client(Response(authority((204, 1), (232, 0))))
    with (
        patch.object(obj._api, "async_control_session", new_callable=AsyncMock) as control,
        patch.object(obj._api, "_ensure_tsp_login", new_callable=AsyncMock) as login,
        patch.object(obj._api, "_ensure_tokens", new_callable=AsyncMock) as ensure,
        patch.object(obj._api, "_refresh", new_callable=AsyncMock) as refresh,
        patch.object(obj._api, "_post", new_callable=AsyncMock) as api_post,
        patch.object(obj, "_task_id", new_callable=AsyncMock) as pin,
        patch.object(obj, "async_rear_defrost", new_callable=AsyncMock) as prepare,
        patch.object(obj, "_submit", new_callable=AsyncMock) as actuator,
    ):
        result = asyncio.run(diag.diagnose_rear_defrost_permissions(obj, VIN))
        for spy in (control, login, ensure, refresh, api_post, pin, prepare, actuator):
            spy.assert_not_called()
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/queryVehicleAuthority")
    body = json.loads(session.calls[0][1]["data"])
    assert body == {"vin": VIN, "tUserId": "private-user-id", "channelId": "1"}
    assert result["permissions"]["232"]["local"]["status"] == "denied"
    assert result["permissions"]["2321"]["local"]["status"] == "missing"


@pytest.mark.parametrize("field,value", [
    ("_tokens", None), ("_tuser_id", None), ("_tuser_id", " "),
    ("_user_token", ""), ("_tsp_account", None),
    ("_tsp_login_confirmed", False), ("_tsp_login_confirmed", 1),
])
def test_missing_cached_session_never_requests(field, value):
    obj, session = client()
    setattr(obj._api, field, value)
    with pytest.raises(commands.CommandError):
        asyncio.run(diag.diagnose_rear_defrost_permissions(obj, VIN))
    assert session.calls == []


def test_equal_but_not_identical_account_and_expired_account_fail_closed():
    obj, session = client()
    tokens = obj._api._tokens
    obj._api._tsp_account = type(tokens)(tokens.access_token, tokens.refresh_token, tokens.expires_at)
    with pytest.raises(commands.CommandError):
        asyncio.run(diag.diagnose_rear_defrost_permissions(obj, VIN))
    obj._api._tokens = obj._api._tsp_account = type(tokens)("secret", None, 1)
    with pytest.raises(commands.CommandError):
        asyncio.run(diag.diagnose_rear_defrost_permissions(obj, VIN))
    assert session.calls == []


@pytest.mark.parametrize("rows,local,upstream,mismatch,conflict", [
    ([], "missing", "missing", False, False),
    ([(232, 0)], "denied", "denied", False, False),
    ([(232, "1")], "allowed", "allowed", False, False),
    ([(232, True)], "unknown", "missing", True, False),
    ([(232, "bad-secret")], "unknown", "missing", True, False),
    ([(232, " 1 ")], "unknown", "allowed", True, False),
    ([(" 232 ", 1)], "missing", "allowed", True, False),
    ([(232, 1), (232, 0)], "unknown", "denied", True, True),
    ([(232, 1), (232, 0), (232, 1)], "unknown", "allowed", True, True),
    ([(232, 1), (232, 1)], "allowed", "allowed", False, False),
    ([(232, 999999999999999)], "unknown", "unknown", False, False),
    ([(232, -1)], "unknown", "unknown", False, False),
])
def test_states_and_parser_comparison(rows, local, upstream, mismatch, conflict):
    report = diag.permission_snapshot(authority(*rows))["permissions"]["232"]
    assert report["local"]["status"] == local
    assert report["upstream"]["status"] == upstream
    assert report["parser_mismatch"] is mismatch
    assert report["duplicate_conflict"] is conflict
    assert report["duplicate"] is (len(rows) > 1)


@pytest.mark.parametrize("result", [{}, {"code": "secret", "data": {"permissionList": []}},
                                       {"code": "000000", "data": {"permissionList": "secret"}}])
def test_unreadable_or_failed_authority_is_unknown_not_missing(result):
    report = diag.permission_snapshot(result)
    assert report["authority"] == "unknown"
    for item in report["permissions"].values():
        assert item["present"] is None
        assert item["local"]["status"] == "unknown"


def test_redaction_types_and_numeric_domain():
    result = authority((232, "private-secret"), (204, 123456789012345),
                       (2041, {"password": "private-secret"}), (999, "private-secret"))
    result["message"] = "private-secret"
    result["data"]["vin"] = VIN
    result["data"]["permissionList"].extend([None, "private-secret", {"id": 2321}])
    report = diag.permission_snapshot(result)
    text = json.dumps(report)
    for secret in ("private-secret", "123456789012345", VIN, "password", '"999"'):
        assert secret not in text
    assert set(report["permissions"]) == {"204", "2041", "20411", "232", "2321"}
    assert report["permissions"]["232"]["types"] == [{"id_type": "integer", "state_type": "string"}]


def test_session_changed_during_request_discards_snapshot():
    obj, _ = client()

    async def changed(*args):
        obj._api._tsp_login_confirmed = False
        return authority((232, 1))

    obj._bff = AsyncMock(side_effect=changed)
    with pytest.raises(commands.CommandError):
        asyncio.run(diag.diagnose_rear_defrost_permissions(obj, VIN))
    obj._bff.assert_awaited_once()
