"""Optional cached-data reads share auth but never invoke vehicle controls."""

import socket

import pytest
from test_api import OTHER_VIN, TSP_LOGIN, VEHICLES, VIN, Response, api, client, run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Real network forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.mark.parametrize(
    "method,path",
    [
        ("async_location", "/asc/vehicleControl/queryVehicleLocation"),
        ("async_charge_schedule", "/asd/chargeAppointManage/chargeAppointQuery"),
        ("async_charge_depth", "/asd/chargeDepthManage/chargeDepthQuery"),
    ],
)
def test_optional_query_uses_known_vin_signed_read_only_body(method, path):
    obj, session = client(
        Response(VEHICLES),
        Response(TSP_LOGIN),
        Response({"code": "000000", "body": {"sample": 1}}),
    )

    async def scenario():
        await obj.async_list_vehicles()
        return await getattr(obj, method)(VIN)

    assert run(scenario()) == {"sample": 1}
    url, request = session.calls[-1]
    assert url == api.TSP + path
    assert set(request["json"]) == {"vin", "appId", "sign"}
    assert request["json"]["vin"] == VIN
    assert request["headers"]["Authorization"] == "synthetic-tsp-token"
    assert request["ssl"] is True and request["allow_redirects"] is False
    assert all(
        "checkPassword" not in url and "smsAwaken" not in url
        for url, _ in session.calls
    )
    assert not session.closed


@pytest.mark.parametrize(
    "method", ["async_location", "async_charge_schedule", "async_charge_depth"]
)
def test_undiscovered_vehicle_never_queried(method):
    obj, session = client()
    with pytest.raises(api.ApiError):
        run(getattr(obj, method)(OTHER_VIN))
    assert not session.calls


def test_optional_denial_preserves_primary_vehicle_session_and_no_retry():
    obj, session = client(
        Response(VEHICLES),
        Response(TSP_LOGIN),
        Response({}, status=404),
        Response({"code": "000000", "body": {"dumpEnergy": "60"}}),
    )

    async def scenario():
        await obj.async_list_vehicles()
        with pytest.raises(api.ApiError):
            await obj.async_location(VIN)
        return await obj.async_realtime(VIN)

    assert run(scenario()) == {"dumpEnergy": "60"}
    assert len(session.calls) == 4
    assert sum(url.endswith(api.TSP_LOGIN_PATH) for url, _ in session.calls) == 1


@pytest.mark.parametrize("reply", [{"code": "A07900"}, {"code": "000000", "body": {}}])
def test_optional_no_data_is_not_a_physical_wake_or_auth_failure(reply):
    obj, session = client(Response(VEHICLES), Response(TSP_LOGIN), Response(reply))

    async def scenario():
        await obj.async_list_vehicles()
        return await obj.async_location(VIN)

    assert run(scenario()) is None
    assert len(session.calls) == 3


def test_optional_helper_cannot_be_used_as_generic_endpoint_caller():
    obj, session = client()
    with pytest.raises(api.ApiError):
        run(obj._optional_vehicle_query(VIN, "token"))
    assert not session.calls
