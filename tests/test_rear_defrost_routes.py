"""Offline rear-window permission and coupled-route contract tests."""

import asyncio

import pytest
from test_commands import (
    PIN,
    VIN,
    Response,
    accepted,
    api,
    authority,
    block_network,  # noqa: F401 — pytest fixture imported for network isolation.
    bodies,
    client,
    commands,
    run,
)

pytestmark = pytest.mark.usefixtures("block_network")


def rear_request(obj, coupled, on=True, **changes):
    if not coupled:
        return obj.async_rear_defrost(VIN, PIN, on, 10)
    args = {"temperature": 23.5, "duration": 10, "acknowledge_climate_change": True}
    args.update(changes)
    return obj.async_rear_defrost_with_climate(VIN, PIN, on, **args)


@pytest.mark.parametrize("climate_core", [0, "0", True, "unknown"])
def test_dedicated_rear_permission_is_independent_of_climate_permission(climate_core):
    responses = list(accepted(category=232))
    responses[0] = Response(
        authority((232, 1), (2321, 1), (204, 0), (2041, climate_core))
    )
    obj, session = client(*responses)
    run(rear_request(obj, False, False))
    assert len(session.calls) == 5
    assert session.calls[-1][0].endswith("/backDefrostingControl")
    assert "airControlType" not in bodies(session)[-1]


@pytest.mark.parametrize(
    "coupled,required", [(False, (232, 2321)), (True, (204, 2041, 20411))]
)
@pytest.mark.parametrize("bad", [None, 0, "0", True, "unknown", -1])
def test_rear_routes_fail_closed_for_each_required_permission(coupled, required, bad):
    for missing in required:
        perms = [(key, 1) for key in required if key != missing]
        if bad is not None:
            perms.append((missing, bad))
        perms += [(key, 1) for key in ((232, 2321) if coupled else (204, 2041, 20411))]
        obj, session = client(Response(authority(*perms)))
        with pytest.raises(commands.CommandError):
            run(rear_request(obj, coupled))
        assert len(session.calls) == 1  # No discovery, PIN or physical command.


@pytest.mark.parametrize(
    "coupled,required", [(False, (232, 2321)), (True, (204, 2041, 20411))]
)
def test_rear_route_conflicting_permission_duplicates_fail_closed(coupled, required):
    for key in required:
        obj, session = client(
            Response(authority(*[(k, 1) for k in required], (key, 0), (key, 1)))
        )
        with pytest.raises(commands.CommandError):
            run(rear_request(obj, coupled))
        assert len(session.calls) == 1


@pytest.mark.parametrize("on,value", [(True, "1"), (False, "0")])
def test_alternate_rear_route_is_explicitly_coupled_and_preserves_parameters(on, value):
    responses = list(accepted())
    responses[0] = Response(authority((204, 1), (2041, 1), (20411, 1), (232, 0)))
    obj, session = client(*responses)
    run(rear_request(obj, True, on))
    assert session.calls[-1][0] == api.TSP + "/asc/vehicleControl/airControl"
    body = bodies(session)[-1]
    common = {"clientType", "seq", "taskId", "vin", "appId", "sign"}
    specific = {key: value for key, value in body.items() if key not in common}
    assert specific == {
        "airControlType": value,
        "airType": "1",
        "temperature": "23.5",
        "times": "10",
        "backDefrosting": value,
    }
    assert len(session.calls) == 5


@pytest.mark.parametrize("ack", [False, None, 1, "true"])
def test_alternate_route_requires_explicit_side_effect_acknowledgement(ack):
    obj, session = client()
    with pytest.raises(commands.CommandError, match="acknowledgement"):
        run(rear_request(obj, True, acknowledge_climate_change=ack))
    assert not session.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"temperature": 31},
        {"temperature": 21.1},
        {"temperature": None},
        {"duration": 6},
        {"duration": None},
        {"duration": True},
    ],
)
def test_alternate_route_never_guesses_or_clamps_settings(changes):
    obj, session = client()
    with pytest.raises(commands.CommandError):
        run(rear_request(obj, True, **changes))
    assert not session.calls


@pytest.mark.parametrize("coupled", [False, True])
@pytest.mark.parametrize(
    "final,exception",
    [
        (Response({"code": "A00084", "msg": "private"}), commands.CommandError),
        (TimeoutError(), commands.CommandOutcomeUnknown),
        (Response({"code": "A99999"}), commands.CommandOutcomeUnknown),
        (asyncio.CancelledError(), commands.CommandCancelled),
    ],
)
def test_neither_rear_route_retries_or_falls_back_after_submission(
    coupled, final, exception
):
    responses = list(accepted())[:4]
    responses[0] = Response(
        authority((232, 1), (2321, 1), (204, 1), (2041, 1), (20411, 1))
    )
    obj, session = client(*responses, final)
    with pytest.raises(exception) as caught:
        run(rear_request(obj, coupled))
    if isinstance(final, Response) and final.data.get("code") == "A00084":
        assert "(A00084)" in str(caught.value)
        assert not isinstance(caught.value, commands.CommandOutcomeUnknown)
    assert "private" not in str(caught.value)
    assert len(session.calls) == 5
