"""Synthetic command protocol tests. Sockets blocked; no HA/account/backend I/O."""

import asyncio
import importlib.util
import json
import socket
import sys
import types
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).parents[1] / "custom_components/omoda_jaecoo"
PACKAGE = "_offline_jaecoo_controls"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api = load("api")
commands = load("commands")
VIN = "LTEST123456789012"
PIN = "1234"
TOKENS = api.TokenSet("private-account", "private-refresh", None)
VEHICLE = api.Vehicle(VIN, "Test", "J7", 2, 16.0, 30.0, 0.5, (5, 10, 15))
GOOD = {"code": "000000"}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in command tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


class Response:
    def __init__(self, data=None, status=200, error=None):
        self.data, self.status, self.error = data, status, error
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.released = True

    async def json(self):
        if self.error is not None:
            raise self.error
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, "Unexpected request or unsafe retry"
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def authority(*permissions):
    return {
        "code": "000000",
        "data": {
            "permissionList": [
                {"id": identifier, "state": state} for identifier, state in permissions
            ]
        },
    }


def client(*responses, vehicle=VEHICLE):
    session = Session(*responses)
    obj = api.JaecooApi(session, tokens=TOKENS)
    obj._vehicles = {VIN: vehicle}
    obj._user_token = "private-tsp"
    obj._tuser_id = "private-user-id"
    obj._tsp_account = TOKENS
    obj._tsp_login_confirmed = True
    return commands.CommandClient(obj, session, "44"), session


def accepted(final=None, category=203):
    return (
        Response(authority((category, 1))),
        Response({"code": "000000", "data": [{"vin": VIN}]}),
        Response(GOOD),
        Response({"code": "000000", "data": {"taskId": "synthetic-task"}}),
        Response(final or {"code": "000000"}),
    )


def run(awaitable):
    return asyncio.run(awaitable)


def bodies(session):
    return [json.loads(request["data"]) for _, request in session.calls]


def test_independent_sm4_pin_vector():
    # Independently generated with OpenSSL sm4-ecb, MD5 hexadecimal text, PKCS7.
    assert commands._encode_pin(PIN) == (
        "CvwhiwmDBS41KJCDKvaFvCxX0LT5N5lKpw83VjLrmHYCgYQKval/guvF8fe6O5Z0"
    )


def test_independent_command_signature_vector():
    assert commands._command_body(
        VIN, "synthetic-task", {"lockType": "0"}, 1700000000123
    )["sign"] == ("W9LAXNEWRQYDFMZVVUBRTDRYLT+HHSTQXLVJXGEY+W0=")


@pytest.mark.parametrize("locked,lock_type", [(True, "0"), (False, "1")])
@pytest.mark.parametrize("code", ["000000", "A00079"])
def test_lock_exact_wire_accepted_only(monkeypatch, locked, lock_type, code):
    monkeypatch.setattr(commands.time, "time", lambda: 1700000000.123)
    responses = accepted({"code": code})
    obj, session = client(*responses)
    assert run(obj.async_lock(VIN, PIN, locked)) is None
    assert [url for url, _ in session.calls] == [
        api.BFF + commands.COMMAND_ROUTES[key][1]
        for key in ("authority", "vehicles", "default", "pin")
    ] + [api.TSP + "/asc/vehicleControl/lockControl"]
    authority_body, discovery, default, pin_body, command = bodies(session)
    assert authority_body == {
        "vin": VIN,
        "tUserId": "private-user-id",
        "channelId": "1",
    }
    assert discovery == {} and default == {"vin": VIN}
    assert pin_body == {
        "vin": VIN,
        "tUserId": "private-user-id",
        "channelId": "1",
        "password": commands._encode_pin(PIN),
        "needDecode": 0,
        "scene": 0,
        "type": 0,
    }
    assert command == commands._command_body(
        VIN, "synthetic-task", {"lockType": lock_type}, 1700000000123
    )
    assert set(command) == {
        "lockType",
        "clientType",
        "seq",
        "taskId",
        "vin",
        "appId",
        "sign",
    }
    for _, request in session.calls[:4]:
        assert request["headers"]["Authorization"] == "Bearer private-account"
    assert session.calls[-1][1]["headers"] == {
        "Authorization": "private-tsp",
        "timestamp": "1700000000123",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "okhttp/4.9.2",
    }
    for _, request in session.calls:
        assert request["ssl"] is True and request["allow_redirects"] is False
        assert request["raise_for_status"] is False
        assert request["timeout"].total == 40
        assert isinstance(request["data"], bytes)
        assert b'": ' not in request["data"]
    assert all(response.released for response in responses)
    assert "private" not in repr(obj)


@pytest.mark.parametrize("on,value", [(True, "1"), (False, "0")])
def test_climate_flat_body_off_also_includes_temperature_and_times(on, value):
    obj, session = client(*accepted(category=204))
    assert run(obj.async_climate(VIN, PIN, on, 21.5, 10)) is None
    body = bodies(session)[-1]
    assert body["airControlType"] == value
    assert (
        body["airType"] == "1"
        and body["temperature"] == "21.5"
        and body["times"] == "10"
    )
    assert set(body) == {
        "airControlType",
        "airType",
        "temperature",
        "times",
        "clientType",
        "seq",
        "taskId",
        "vin",
        "appId",
        "sign",
    }
    assert session.calls[-1][0] == api.TSP + "/asc/vehicleControl/airControl"


@pytest.mark.parametrize(
    "vin,pin,flag",
    [
        ("bad", PIN, True),
        (VIN, "", True),
        (VIN, "  ", True),
        (VIN, None, True),
        (VIN, PIN, 1),
        (VIN, PIN, "false"),
        ([], PIN, True),
    ],
)
def test_lock_argument_validation_precedes_all_requests(vin, pin, flag):
    obj, session = client()
    with pytest.raises(commands.CommandError):
        run(obj.async_lock(vin, pin, flag))
    assert not session.calls


@pytest.mark.parametrize(
    "temperature,duration",
    [
        (15, 5),
        (31, 5),
        (21.1, 5),
        (float("nan"), 5),
        (float("inf"), 5),
        (True, 5),
        ("21", 5),
        (21, 6),
        (21, True),
        (21, "5"),
        (21, 5.0),
    ],
)
def test_climate_arguments_fail_before_any_request(temperature, duration):
    obj, session = client()
    with pytest.raises(commands.CommandError):
        run(obj.async_climate(VIN, PIN, True, temperature, duration))
    assert not session.calls


def test_missing_climate_metadata_fails_closed():
    obj, session = client(vehicle=api.Vehicle(VIN, "Test", None, None))
    with pytest.raises(commands.CommandError):
        run(obj.async_climate(VIN, PIN, False, 21, 15))
    assert not session.calls


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"code": "000000"},
        {"code": "A99999", "data": {}},
        authority(),
        authority((203, 0)),
        authority((203, True)),
        authority((203, 1), (203, 0)),
        authority((203, "unknown")),
    ],
)
def test_unknown_or_denied_authority_blocks_pin_and_command(result):
    obj, session = client(Response(result))
    with pytest.raises(commands.CommandError):
        run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == 1


@pytest.mark.parametrize("child", [0, "0", True, "unknown"])
def test_climate_denied_or_malformed_core_authority_blocks(child):
    obj, session = client(Response(authority((204, 1), (2041, child))))
    with pytest.raises(commands.CommandError):
        run(obj.async_climate(VIN, PIN, True, 21, 5))
    assert len(session.calls) == 1


def test_string_authority_nonzero_allowed_and_duration_child_not_guessed():
    responses = list(accepted(category=204))
    responses[0] = Response(authority(("204", "2"), (2041, 1), (2044, 0)))
    obj, session = client(*responses)
    run(obj.async_climate(VIN, PIN, True, 21, 15))
    assert len(session.calls) == 5


@pytest.mark.parametrize("index", [1, 2])
@pytest.mark.parametrize(
    "result", [{}, {"code": "A99999"}, {"code": "000000", "success": False}]
)
def test_discovery_and_default_require_success(index, result):
    responses = list(accepted())[:index] + [Response(result)]
    obj, session = client(*responses)
    with pytest.raises(commands.CommandError) as caught:
        run(obj.async_lock(VIN, PIN, True))
    assert not isinstance(caught.value, commands.PinVerificationError)
    assert len(session.calls) == index + 1


@pytest.mark.parametrize(
    "failure",
    [
        Response({}),
        Response({"code": "A00285", "taskId": "private-task"}),
        Response({"code": "000000"}),
        Response({"code": "000000", "taskId": " "}),
        Response({"data": {"taskId": "private-task"}}),
        Response(status=429),
        Response(error=ValueError("private-response")),
        TimeoutError("private-transport"),
        aiohttp.ClientConnectionError("private-transport"),
    ],
)
def test_pin_rejection_or_inconclusive_blocks_physical_request(failure, caplog):
    obj, session = client(*list(accepted())[:3], failure)
    with pytest.raises(commands.PinVerificationError) as caught:
        run(obj.async_lock(VIN, PIN, True))
    assert "private" not in str(caught.value) and "private" not in caplog.text
    assert caught.value.__suppress_context__ and len(session.calls) == 4


def test_top_level_task_id_accepted():
    responses = list(accepted())
    responses[3] = Response({"code": "000000", "taskId": "top-task"})
    obj, session = client(*responses)
    run(obj.async_lock(VIN, PIN, True))
    assert bodies(session)[-1]["taskId"] == "top-task"


@pytest.mark.parametrize("code", sorted(commands._REJECTED))
def test_known_rejections_do_not_retry(code):
    obj, session = client(*accepted({"code": code, "msg": "private-msg"}))
    with pytest.raises(commands.CommandError) as caught:
        run(obj.async_lock(VIN, PIN, True))
    assert not isinstance(caught.value, commands.CommandOutcomeUnknown)
    assert "private" not in str(caught.value) and len(session.calls) == 5


@pytest.mark.parametrize(
    "failure",
    [
        Response({}),
        Response({"code": "0"}),
        Response({"code": 0}),
        Response({"code": "A99999", "msg": "private-message"}),
        Response([]),
        Response({"code": "000000", "success": False}),
        Response(status=503),
        Response(status=302),
        Response(status=401),
        Response(status=429),
        Response(error=ValueError("private-response")),
        TimeoutError("private-transport"),
        aiohttp.ClientConnectionError("private-transport"),
    ],
)
def test_unknown_outcome_after_send_never_retries(failure, caplog):
    obj, session = client(*list(accepted())[:4], failure)
    with pytest.raises(commands.CommandOutcomeUnknown) as caught:
        run(obj.async_lock(VIN, PIN, True))
    assert "private" not in str(caught.value) and "private" not in caplog.text
    assert len(session.calls) == 5


@pytest.mark.parametrize(
    "index,expected",
    [
        (0, asyncio.CancelledError),
        (1, asyncio.CancelledError),
        (2, asyncio.CancelledError),
        (3, commands.PinVerificationCancelled),
        (4, commands.CommandCancelled),
    ],
)
def test_cancellation_preserves_safety_phase_and_no_retry(index, expected):
    obj, session = client(
        *list(accepted())[:index], asyncio.CancelledError("private-cancel")
    )
    with pytest.raises(expected) as caught:
        run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == index + 1
    if index >= 3:
        assert "private" not in str(caught.value)
        assert caught.value.__suppress_context__


def test_command_route_allowlist_and_no_constructor_requests():
    obj, session = client()
    with pytest.raises(commands.CommandError):
        run(obj._post("wake", {}, {}))
    assert not session.calls
    assert {
        key for key in commands.COMMAND_ROUTES if key in ("wake", "locate", "mqtt")
    } == set()
    assert set(api.ALLOWED_ROUTES) == {
        "token",
        "vehicles",
        "tsp_login",
        "realtime",
        "location",
        "charge_schedule",
        "charge_depth",
    }
    for country in ("0", "44\r\nsecret", None):
        with pytest.raises(commands.CommandError):
            commands.CommandClient(obj._api, session, country)


def test_task_ids_not_cached_across_explicit_commands():
    responses = list(accepted()) + list(accepted())
    responses[8] = Response({"code": "000000", "data": {"taskId": "second-task"}})
    obj, session = client(*responses)

    async def scenario():
        await obj.async_lock(VIN, PIN, True)
        await obj.async_lock(VIN, PIN, False)

    run(scenario())
    assert len(session.calls) == 10
    assert bodies(session)[-1]["taskId"] == "second-task"


def test_full_api_control_session_login_reused():
    session = Session(
        Response({"code": "000000", "data": [{"vin": VIN}]}),
        Response(
            {"code": "000000", "data": {"userToken": "private-tsp", "tUserId": 1234}}
        ),
        *accepted(),
        *accepted(),
    )
    obj = api.JaecooApi(session, tokens=TOKENS)
    control = commands.CommandClient(obj, session, "44")

    async def scenario():
        await obj.async_list_vehicles()
        await control.async_lock(VIN, PIN, True)
        await control.async_lock(VIN, PIN, False)

    run(scenario())
    assert sum(url.endswith(api.TSP_LOGIN_PATH) for url, _ in session.calls) == 1
    assert json.loads(session.calls[2][1]["data"])["tUserId"] == "1234"
    assert len(session.calls) == 12


def test_control_session_unavailable_no_pin_or_command():
    obj, session = client(
        Response({"code": "000000", "data": {"userToken": "private-tsp"}})
    )
    obj._api._user_token = None
    obj._api._tuser_id = None
    with pytest.raises(commands.CommandError):
        run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith(api.TSP_LOGIN_PATH)


def test_unrepresentable_temperature_is_not_silently_rounded():
    vehicle = api.Vehicle(VIN, "Test", None, None, 16.25, 30.25, 0.5, (15,))
    obj, session = client(vehicle=vehicle)
    with pytest.raises(commands.CommandError):
        run(obj.async_climate(VIN, PIN, True, 21.25, 15))
    assert not session.calls


def test_enormous_authority_identifier_fails_closed():
    obj, session = client(Response(authority(("2" * 5000, 1))))
    with pytest.raises(commands.CommandError):
        run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "pin", ["123", "123456789", "abcd", "１２３４", "12 34", " 1234", "1234 ", "1234\n"]
)
@pytest.mark.parametrize("action", ["lock", "climate"])
def test_pin_requires_four_to_eight_ascii_digits_before_network(pin, action):
    obj, session = client()
    with pytest.raises(commands.CommandError):
        if action == "lock":
            run(obj.async_lock(VIN, pin, True))
        else:
            run(obj.async_climate(VIN, pin, True, 21, 15))
    assert not session.calls


@pytest.mark.parametrize("pin", ["0000", "12345678"])
def test_pin_length_boundaries_are_valid(pin):
    obj, session = client(*accepted())
    run(obj.async_lock(VIN, pin, True))
    assert len(session.calls) == 5


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        None,
        "unknown",
        [{"vin": "LTEST123456789013"}],
        {"controlCarList": []},
        {"unexpected": [{"vin": VIN}]},
        [{"vin": "bad"}],
    ],
)
def test_fresh_discovery_must_still_include_selected_vin(data):
    obj, session = client(
        Response(authority((203, 1))),
        Response({"code": "000000", "data": data}),
    )
    with pytest.raises(commands.CommandError, match="absent from current discovery"):
        run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == 2
    assert all(
        not url.endswith(commands.COMMAND_ROUTES[key][1])
        for url, _ in session.calls
        for key in ("default", "pin", "lock")
    )


@pytest.mark.parametrize(
    "data",
    [
        [{"vin": VIN}],
        {"vin": VIN},
        {"authorizedControlCarList": [{"VIN": VIN.lower()}]},
        {"controlCarList": [{"vin": VIN}]},
        {"carList": [{"vin": VIN}]},
        {"list": [{"vin": VIN}]},
        {"vehicles": [{"vin": VIN}]},
    ],
)
def test_fresh_discovery_known_shapes_authorize_selected_vin(data):
    responses = list(accepted())
    responses[1] = Response({"code": "000000", "data": data})
    obj, session = client(*responses)
    run(obj.async_lock(VIN, PIN, True))
    assert len(session.calls) == 5
