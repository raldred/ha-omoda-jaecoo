"""Synthetic, fully offline API tests (no HA runtime or real captures required)."""

import asyncio
import importlib.util
import socket
import sys
from pathlib import Path

import aiohttp
import pytest

# Loading the leaf module directly keeps the baseline suite independent of HA's
# Python version and avoids executing custom_components/omoda_jaecoo/__init__.py.
_SPEC = importlib.util.spec_from_file_location(
    "_offline_jaecoo_api",
    Path(__file__).parents[1] / "custom_components/omoda_jaecoo/api.py",
)
api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = api
_SPEC.loader.exec_module(api)

VIN = "LTEST123456789012"
OTHER_VIN = "LTEST123456789013"
EMAIL = "offline-test@example.invalid"
PASSWORD = "synthetic-password-never-real"
TOKENS = api.TokenSet("synthetic-access", "synthetic-refresh", None)
VEHICLES = {
    "code": "000000",
    "data": [{"vin": VIN, "nickname": "Test car", "powerType": 2}],
}
TSP_LOGIN = {"code": "000000", "data": {"userToken": "synthetic-tsp-token"}}


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in API tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


class Response:
    def __init__(self, data=None, status=200, error=None, delay=False):
        self.data, self.status, self.error, self.delay = data, status, error, delay
        self.released = False

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(0)
        return self

    async def __aexit__(self, *args):
        self.released = True

    async def json(self):
        if self.error:
            raise self.error
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, "Unexpected extra request (possibly an unsafe retry)"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


def run(awaitable):
    return asyncio.run(awaitable)


def client(*responses, tokens=TOKENS, **kwargs):
    session = Session(*responses)
    return api.JaecooApi(session, tokens=tokens, **kwargs), session


@pytest.mark.parametrize(
    "password,expected",
    [
        ("test-passphrase", "r0BNfwNbw/PD8V3S9oUnyQ=="),
        ("秘密🔋 café", "lc/tWqWrFReykrVHEsZ+4oXGJFUDMHipnBjmMf3/ffs="),
    ],
)
def test_independent_aes_vectors(password, expected):
    assert api._encode_password(password) == expected


def test_independent_signature_vectors(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000.123)
    headers = api._bff_headers(api.TOKEN_PATH, "44")
    assert (
        headers["signature"]
        == "5e76aec0e7367e46d10b0d799ca2989c78ecb70b64bfbfbe4d9fcdd72c868f7b"
    )
    assert headers["timestamp"] == "1700000000123"
    assert headers["Authorization"] == "Basic bGVnZW5kQXBwOmxlZ2VuZEFwcA=="
    assert headers["DEPT-ID"] == "44"
    assert api._realtime_body(VIN, 1700000000123) == {
        "vin": VIN,
        "appId": "eu-1",
        "sign": "GQK6PXJRMF5AEGH99AW7SNBPL48O5H1/BSUELQHBPWA=",
    }


def test_minimal_serialization_and_redacted_token_repr():
    tokens = api.TokenSet.from_dict(
        {**TOKENS.to_dict(), "password": PASSWORD, "userToken": "tsp"}
    )
    assert tokens == TOKENS
    assert set(tokens.to_dict()) == {"access_token", "refresh_token", "expires_at"}
    assert "synthetic" not in repr(tokens)
    vehicle = api.Vehicle(VIN, "Test", "J7", 2)
    assert api.Vehicle.from_dict({**vehicle.to_dict(), "ownerEmail": EMAIL}) == vehicle
    assert set(vehicle.to_dict()) == {
        "vin",
        "name",
        "model",
        "power_type",
        "min_temperature",
        "max_temperature",
        "temperature_step",
        "allowed_air_durations",
    }
    with pytest.raises(api.ApiError):
        api.TokenSet.from_dict({"access_token": PASSWORD * 0})
    with pytest.raises(api.ApiError):
        api.Vehicle.from_dict({"vin": "bad " + EMAIL})


@pytest.mark.parametrize("envelope", [None, "data", "body"])
def test_login_envelopes_no_retained_password_or_email(monkeypatch, envelope):
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    received = []
    data = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": "3600",
    }
    response = data if envelope is None else {envelope: data}
    obj, session = client(Response(response), tokens=None, on_tokens=received.append)
    result = run(obj.async_login(EMAIL, PASSWORD))
    assert result == api.TokenSet("new-access", "new-refresh", 4600)
    assert obj.tokens is result
    assert received == [result]
    assert PASSWORD not in repr(vars(obj))
    assert EMAIL not in repr(vars(obj))
    url, request = session.calls[0]
    assert url == api.BFF + api.TOKEN_PATH
    assert request["data"] == {
        "username": EMAIL,
        "password": api._encode_password(PASSWORD),
        "grant_type": "password",
        "scope": "server",
        "needDecode": "1",
        "loginType": "email",
    }
    assert request["headers"]["Authorization"] == api.APP_BASIC
    assert not session.closed


@pytest.mark.parametrize("expiry", [None, "bad", float("inf"), -1, True])
def test_no_invented_expiry(expiry):
    obj, _ = client(
        Response({"access_token": "new", "expires_in": expiry}), tokens=None
    )
    assert run(obj.async_login(EMAIL, PASSWORD)).expires_at is None


@pytest.mark.parametrize(
    "shape",
    [
        [{"vin": VIN}],
        {"vin": VIN},
        {"controlCarList": [{"vin": VIN}]},
        {"authorizedControlCarList": [{"VIN": VIN.lower()}]},
        {"list": [{"vin": VIN}]},
        {"vehicles": [{"vin": VIN}]},
        {"carList": [{"vin": VIN}]},
    ],
)
def test_discovery_known_shapes(shape):
    obj, session = client(Response({"data": shape}))
    assert run(obj.async_list_vehicles()) == [api.Vehicle(VIN, "Vehicle", None, None)]
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer synthetic-access"
    assert session.calls[0][1]["json"] == {}


def test_discovery_dedup_and_minimal_metadata():
    obj, _ = client(
        Response(
            {
                "data": {
                    "controlCarList": [
                        {
                            "vin": VIN.lower(),
                            "nickname": "Test",
                            "modelName": "J7",
                            "powerType": "2",
                            "email": EMAIL,
                            "userToken": "private",
                        }
                    ],
                    "authorizedControlCarList": [
                        {"vin": VIN, "nickname": "Duplicate"},
                        {"vin": OTHER_VIN, "fullName": "Other", "powerType": True},
                        {"vin": "invalid"},
                        None,
                        "unexpected",
                    ],
                }
            }
        )
    )
    assert run(obj.async_list_vehicles()) == [
        api.Vehicle(VIN, "Test", "J7", 2),
        api.Vehicle(OTHER_VIN, "Other", None, None),
    ]
    assert EMAIL not in repr(vars(obj))


@pytest.mark.parametrize(
    "shape", [[], {"controlCarList": []}, {"authorizedControlCarList": []}]
)
def test_empty_discovery(shape):
    obj, _ = client(Response({"data": shape}))
    assert run(obj.async_list_vehicles()) == []


@pytest.mark.parametrize("shape", [None, "private", {}, {"unexpected": []}])
def test_unknown_discovery_is_not_empty_success(shape):
    obj, _ = client(Response({"data": shape}))
    with pytest.raises(api.ApiError, match="unrecognized"):
        run(obj.async_list_vehicles())


def test_realtime_tsp_cached_and_safe_wire(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000.123)
    responses = [
        Response(VEHICLES),
        Response(TSP_LOGIN),
        Response({"body": {"soc": 42}}),
        Response({"data": {"soc": 43}}),
    ]
    obj, session = client(*responses)

    async def scenario():
        await obj.async_list_vehicles()
        assert await obj.async_realtime(VIN) == {"soc": 42}
        assert await obj.async_realtime(VIN) == {"soc": 43}

    run(scenario())
    assert len(session.calls) == 4
    tsp_call = session.calls[1]
    assert tsp_call[0] == api.BFF + api.TSP_LOGIN_PATH
    assert tsp_call[1]["json"] == {"channelId": "1"}
    for url, request in session.calls[2:]:
        assert url == api.TSP + api.REALTIME_PATH
        assert request["headers"]["Authorization"] == "synthetic-tsp-token"
        assert request["json"] == api._realtime_body(VIN, 1700000000123)
    for _, request in session.calls:
        assert request["allow_redirects"] is False
        assert request["ssl"] is True
        assert request["raise_for_status"] is False
        assert request["timeout"].total == 40
    assert all(response.released for response in responses)
    assert "userToken" not in obj.tokens.to_dict()
    assert not session.closed


@pytest.mark.parametrize(
    "response",
    [{}, {"data": {}}, {"body": None}, {"code": "A07900", "body": {"soc": 99}}],
)
def test_sleep_and_no_data_not_auth_or_retry(response):
    obj, session = client(Response(VEHICLES), Response(TSP_LOGIN), Response(response))

    async def scenario():
        await obj.async_list_vehicles()
        assert await obj.async_realtime(VIN) == {}

    run(scenario())
    assert len(session.calls) == 3
    assert obj.tokens == TOKENS


def test_unauthorized_vin_never_sent_and_removed_vehicle_revoked():
    obj, session = client(Response(VEHICLES), Response({"data": []}))

    async def scenario():
        for vin in (VIN, OTHER_VIN, "bad"):
            with pytest.raises(api.ApiError):
                await obj.async_realtime(vin)
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError):
            await obj.async_realtime(OTHER_VIN)
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError):
            await obj.async_realtime(VIN)

    run(scenario())
    assert len(session.calls) == 2


@pytest.mark.parametrize(
    "rejection",
    [
        Response(status=401),
        Response(status=424),
        Response({"code": "401"}),
        Response({"error": "invalid_token"}),
        Response({"key": "invalid_grant"}),
    ],
)
def test_reactive_refresh_exactly_once(rejection):
    received = []
    obj, session = client(
        rejection,
        Response({"access_token": "rotated", "refresh_token": "rotated-refresh"}),
        Response(VEHICLES),
        on_tokens=received.append,
    )
    assert run(obj.async_list_vehicles())[0].vin == VIN
    assert len(session.calls) == 3
    assert session.calls[1][1]["params"] == {
        "grant_type": "refresh_token",
        "refresh_token": TOKENS.refresh_token,
        "scope": "server",
    }
    assert "data" not in session.calls[1][1]
    assert session.calls[1][1]["allow_redirects"] is False
    assert session.calls[1][1]["ssl"] is True
    assert session.calls[2][1]["headers"]["Authorization"] == "Bearer rotated"
    assert received == [obj.tokens]


def test_retry_rejection_does_not_loop():
    obj, session = client(
        Response(status=401), Response({"access_token": "new"}), Response(status=401)
    )
    with pytest.raises(api.AuthenticationError):
        run(obj.async_list_vehicles())
    assert len(session.calls) == 3


def test_proactive_refresh_known_expiry_serialized(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    received = []
    obj, session = client(
        Response({"access_token": "new", "expires_in": 3600}, delay=True),
        Response(VEHICLES),
        Response(VEHICLES),
        tokens=api.TokenSet("old", "refresh", 1050),
        on_tokens=received.append,
    )

    async def scenario():
        result = await asyncio.gather(
            obj.async_list_vehicles(), obj.async_list_vehicles()
        )
        assert all(vehicles[0].vin == VIN for vehicles in result)

    run(scenario())
    assert len(session.calls) == 3
    assert obj.tokens == api.TokenSet("new", "refresh", 4600)
    assert len(received) == 1


def test_unknown_expiry_not_refreshed(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 10**12)
    obj, session = client(Response(VEHICLES))
    run(obj.async_list_vehicles())
    assert len(session.calls) == 1


def test_refresh_invalidates_tsp_cache(monkeypatch):
    now = [1000]
    monkeypatch.setattr(api.time, "time", lambda: now[0])
    obj, session = client(
        Response(VEHICLES),
        Response(TSP_LOGIN),
        Response({"data": {"soc": 1}}),
        Response({"access_token": "new", "expires_in": 3600}),
        Response({"data": {"userToken": "new-tsp"}}),
        Response({"data": {"soc": 2}}),
        tokens=api.TokenSet("old", "refresh", 1300),
    )

    async def scenario():
        await obj.async_list_vehicles()
        await obj.async_realtime(VIN)
        now[0] = 1300
        assert await obj.async_realtime(VIN) == {"soc": 2}

    run(scenario())
    assert len(session.calls) == 6
    assert session.calls[-1][1]["headers"]["Authorization"] == "new-tsp"


@pytest.mark.parametrize(
    "failure,expected",
    [
        (Response(status=429), api.RateLimitError),
        (Response({"code": 429}), api.RateLimitError),
        (Response(status=503), api.CannotConnect),
        (Response({"code": "A99999", "message": PASSWORD}), api.ApiError),
        (Response({"code": "A00000"}), api.ApiError),
        (Response({"data": {}}, status=403), api.ApiError),
        (Response({"error": "server_error"}), api.ApiError),
        (Response(error=ValueError(PASSWORD)), api.ApiError),
        (Response([PASSWORD]), api.ApiError),
        (Response(status=302), api.ApiError),
        (aiohttp.ClientConnectionError(PASSWORD), api.CannotConnect),
        (TimeoutError(PASSWORD), api.CannotConnect),
    ],
)
def test_failures_safe_non_auth_no_retry(failure, expected, caplog):
    obj, session = client(failure)
    with pytest.raises(expected) as caught:
        run(obj.async_list_vehicles())
    assert not isinstance(caught.value, api.AuthenticationError)
    assert PASSWORD not in str(caught.value)
    assert PASSWORD not in caplog.text
    assert len(session.calls) == 1
    assert obj.tokens == TOKENS


@pytest.mark.parametrize(
    "failure,expected",
    [
        (Response({"error": "invalid_grant"}, status=400), api.AuthenticationError),
        (Response(status=429), api.RateLimitError),
        (aiohttp.ClientConnectionError(PASSWORD), api.CannotConnect),
        (Response({"code": "unknown"}), api.ApiError),
    ],
)
def test_refresh_failure_no_password_fallback(failure, expected):
    obj, session = client(Response(status=401), failure)
    with pytest.raises(expected):
        run(obj.async_list_vehicles())
    assert len(session.calls) == 2
    assert all(
        request.get("data", {}).get("grant_type") != "password"
        for _, request in session.calls
    )
    assert obj.tokens == TOKENS


def test_refresh_url_not_exposed_by_transport_error(caplog):
    secret_url = api.BFF + api.TOKEN_PATH + "?refresh_token=" + TOKENS.refresh_token
    obj, session = client(
        Response(status=401), aiohttp.ClientConnectionError(secret_url)
    )
    with pytest.raises(api.CannotConnect) as caught:
        run(obj.async_list_vehicles())
    assert TOKENS.refresh_token not in str(caught.value)
    assert secret_url not in caplog.text
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    assert len(session.calls) == 2


def test_rejected_no_refresh_token_and_no_tokens():
    obj, session = client(Response(status=401), tokens=api.TokenSet("old", None, None))
    with pytest.raises(api.AuthenticationError):
        run(obj.async_list_vehicles())
    assert len(session.calls) == 1
    obj, session = client(tokens=None)
    with pytest.raises(api.AuthenticationError):
        run(obj.async_list_vehicles())
    assert not session.calls


@pytest.mark.parametrize(
    "failure",
    [
        Response(status=401),
        Response({"code": "invalid_token"}),
        Response({"code": "A99999"}),
    ],
)
def test_tsp_rejection_is_not_account_reauth(failure):
    obj, session = client(Response(VEHICLES), Response(TSP_LOGIN), failure)

    async def scenario():
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError) as caught:
            await obj.async_realtime(VIN)
        assert not isinstance(caught.value, api.AuthenticationError)

    run(scenario())
    assert len(session.calls) == 3
    assert obj.tokens == TOKENS


def test_missing_tsp_token_no_refresh_or_retry():
    obj, session = client(Response(VEHICLES), Response({"data": {}}))

    async def scenario():
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError) as caught:
            await obj.async_realtime(VIN)
        assert not isinstance(caught.value, api.AuthenticationError)

    run(scenario())
    assert len(session.calls) == 2


def test_reactive_refresh_concurrent_requests_only_refresh_once():
    obj, session = client(
        Response(status=401, delay=True),
        Response(status=401, delay=True),
        Response({"access_token": "new"}, delay=True),
        Response(VEHICLES),
        Response(VEHICLES),
    )

    async def scenario():
        results = await asyncio.gather(
            obj.async_list_vehicles(), obj.async_list_vehicles()
        )
        assert all(result[0].vin == VIN for result in results)

    run(scenario())
    assert len(session.calls) == 5
    assert sum(url.endswith(api.TOKEN_PATH) for url, _ in session.calls) == 1


def test_revoked_refresh_not_repeated_until_explicit_login(monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    obj, session = client(
        Response({"error": "invalid_grant"}),
        Response(
            {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
        ),
        Response(VEHICLES),
        tokens=api.TokenSet("old", "refresh", 1000),
    )

    async def scenario():
        for _ in range(2):
            with pytest.raises(api.AuthenticationError):
                await obj.async_list_vehicles()
        await obj.async_login(EMAIL, PASSWORD)
        assert (await obj.async_list_vehicles())[0].vin == VIN

    run(scenario())
    assert len(session.calls) == 3


def test_concurrent_realtime_tsp_login_cached_once():
    obj, session = client(
        Response(VEHICLES),
        Response(TSP_LOGIN, delay=True),
        Response({"data": {"soc": 1}}),
        Response({"body": {"soc": 2}}),
    )

    async def scenario():
        await obj.async_list_vehicles()
        assert await asyncio.gather(
            obj.async_realtime(VIN), obj.async_realtime(VIN)
        ) == [{"soc": 1}, {"soc": 2}]

    run(scenario())
    assert len(session.calls) == 4


def test_cancellation_propagates_without_auth_conversion():
    obj, session = client(asyncio.CancelledError())

    # CancelledError is a BaseException rather than an Exception.
    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    session.post = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(obj.async_list_vehicles())
    assert obj.tokens == TOKENS


def test_public_async_surface_has_explicit_auth_but_no_vehicle_writes():
    assert {name for name in dir(api.JaecooApi) if name.startswith("async_")} == {
        "async_login",
        "async_list_vehicles",
        "async_realtime",
        "async_control_session",
        "async_login_phone",
        "async_request_otp",
        "async_login_otp",
        "async_location",
        "async_charge_schedule",
        "async_charge_depth",
    }
    assert set(api.ALLOWED_ROUTES) == {
        "token",
        "vehicles",
        "tsp_login",
        "realtime",
        "location",
        "charge_schedule",
        "charge_depth",
    }


def test_block_unknown_route_and_country():
    obj, session = client()
    with pytest.raises(api.ApiError):
        run(obj._post("command", {}))
    assert not session.calls
    for country in (
        "+44",
        "0",
        "44\r\nAuthorization: secret",
        "https://example.invalid",
    ):
        with pytest.raises(api.ApiError):
            api.JaecooApi(session, country_code=country)


@pytest.mark.parametrize(
    "minimum,maximum,step,durations,expected",
    [
        ("16", "30", "0.5", "5,10,15", (16.0, 30.0, 0.5, (5, 10, 15))),
        (14, 33, 1, "15,5,15", (14.0, 33.0, 1.0, (5, 15))),
        (13, 30, 2, "5,61", (None, None, None, ())),
        (30, 16, None, "5,bad", (None, None, None, ())),
        (float("nan"), 30, True, "5,", (None, None, None, ())),
        (16, 30, 1, "1,60", (16.0, 30.0, 1.0, (1, 60))),
        (16, 30, 1, 15, (16.0, 30.0, 1.0, ())),
    ],
)
def test_discovery_climate_metadata(minimum, maximum, step, durations, expected):
    obj, _ = client(
        Response(
            {
                "data": [
                    {
                        "vin": VIN,
                        "minTemperature": minimum,
                        "maxTemperature": maximum,
                        "temperatureStepLength": step,
                        "maxAirDuration": durations,
                        "ownerEmail": EMAIL,
                        "car_token": "synthetic-secret",
                    }
                ]
            }
        )
    )
    vehicle = run(obj.async_list_vehicles())[0]
    assert (
        vehicle.min_temperature,
        vehicle.max_temperature,
        vehicle.temperature_step,
        vehicle.allowed_air_durations,
    ) == expected
    assert api.Vehicle.from_dict(vehicle.to_dict()) == vehicle
    assert "car_token" not in vehicle.to_dict()
    assert EMAIL not in repr(vehicle)


def test_control_session_reuses_realtime_login_and_safe_repr():
    obj, session = client(
        Response(VEHICLES),
        Response(
            {
                "code": "000000",
                "data": {"userToken": "private-tsp", "tUserId": "private-id"},
            }
        ),
        Response({"data": {"soc": 42}}),
    )

    async def scenario():
        await obj.async_list_vehicles()
        assert obj.get_vehicle(VIN).vin == VIN
        await obj.async_realtime(VIN)
        control = await obj.async_control_session(VIN)
        assert control.account is obj.tokens
        assert control.tokens is control.account
        assert control.user_token == "private-tsp"
        assert control.tuser_id == "private-id"
        assert "private" not in repr(control)
        assert "synthetic" not in repr(control)
        assert await obj.async_control_session(VIN) == control

    run(scenario())
    assert len(session.calls) == 3


def test_control_session_unknown_vin_and_missing_id_fail_closed():
    obj, session = client(Response(VEHICLES), Response(TSP_LOGIN))

    async def scenario():
        for vin in (VIN, OTHER_VIN, None, []):
            with pytest.raises(api.ApiError):
                await obj.async_control_session(vin)
        assert not session.calls
        await obj.async_list_vehicles()
        for _ in range(2):
            with pytest.raises(api.ApiError):
                await obj.async_control_session(VIN)

    run(scenario())
    assert len(session.calls) == 2


def test_refresh_invalidates_control_identity(monkeypatch):
    now = [1000]
    monkeypatch.setattr(api.time, "time", lambda: now[0])
    obj, session = client(
        Response(VEHICLES),
        Response(
            {"code": "000000", "data": {"userToken": "first", "tUserId": "first-id"}}
        ),
        Response({"access_token": "new-account", "expires_in": 3600}),
        Response(
            {"code": "000000", "data": {"userToken": "second", "tUserId": "second-id"}}
        ),
        tokens=api.TokenSet("old", "refresh", 1300),
    )

    async def scenario():
        await obj.async_list_vehicles()
        first = await obj.async_control_session(VIN)
        now[0] = 1300
        second = await obj.async_control_session(VIN)
        assert second.account is obj.tokens and second.account is not first.account
        assert second.user_token == "second" and second.tuser_id == "second-id"

    run(scenario())
    assert len(session.calls) == 4


@pytest.mark.parametrize("identifier,expected", [(12345, "12345"), ("12345", "12345")])
def test_control_session_numeric_user_id(identifier, expected):
    obj, session = client(
        Response(VEHICLES),
        Response(
            {
                "code": "000000",
                "data": {
                    "userToken": "private-tsp",
                    "tUserId": identifier,
                },
            }
        ),
    )

    async def scenario():
        await obj.async_list_vehicles()
        assert (await obj.async_control_session(VIN)).tuser_id == expected

    run(scenario())
    assert len(session.calls) == 2


def test_inflight_tsp_login_cannot_mix_account_sessions():
    obj, session = client(Response(VEHICLES))

    class ConcurrentRefresh(Response):
        async def json(self):
            obj._accept_tokens({"access_token": "concurrently-refreshed"})
            return {
                "data": {"userToken": "old-session-tsp", "tUserId": "old-session-id"}
            }

    session.responses.extend(
        [
            ConcurrentRefresh(),
            Response(
                {
                    "code": "000000",
                    "data": {
                        "userToken": "new-session-tsp",
                        "tUserId": "new-session-id",
                    },
                }
            ),
        ]
    )

    async def scenario():
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError, match="session changed"):
            await obj.async_control_session(VIN)
        assert obj._user_token is None and obj._tuser_id is None
        control = await obj.async_control_session(VIN)
        assert control.account is obj.tokens
        assert control.user_token == "new-session-tsp"
        assert control.tuser_id == "new-session-id"

    run(scenario())
    assert len(session.calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        {"data": {"userToken": "private-tsp", "tUserId": "private-id"}},
        {"code": "000000", "data": {"userToken": "private-tsp", "tUserId": " "}},
        {"code": "000000", "data": {"userToken": " ", "tUserId": "private-id"}},
    ],
)
def test_control_login_requires_explicit_success_and_nonblank_credentials(response):
    obj, session = client(
        Response(VEHICLES), Response(response), Response({"data": {"soc": 42}})
    )

    async def scenario():
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError):
            await obj.async_control_session(VIN)
        # Control context remains fail-closed, but legacy read behavior is unchanged.
        assert await obj.async_realtime(VIN) == {"soc": 42}
        with pytest.raises(api.ApiError):
            await obj.async_control_session(VIN)

    run(scenario())
    assert len(session.calls) == 3
