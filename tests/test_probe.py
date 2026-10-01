"""Offline safety/protocol tests; all credentials and VINs below are synthetic."""
import json
import os
import socket
import stat

import pytest
import requests

import probe


VIN = "LTEST123456789012"
OTHER_VIN = "LTEST123456789013"
EMAIL = "offline-test@example.invalid"
PASSWORD = "synthetic-password-never-real"
TOKEN = "synthetic-access-token"
USER_TOKEN = "synthetic-user-token"


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail closed, including accidental DNS and requests/urllib3 sockets."""
    def blocked(*args, **kwargs):
        pytest.fail("Network access is forbidden in probe tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


class Response:
    def __init__(self, data=None, status=200, error=None):
        self.data = data
        self.status_code = status
        self.error = error

    def json(self):
        if self.error:
            raise self.error
        return self.data


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, "Unexpected extra request (possibly a retry)"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


@pytest.mark.parametrize("password,expected", [
    ("test-passphrase", "r0BNfwNbw/PD8V3S9oUnyQ=="),
    ("秘密🔋 café", "lc/tWqWrFReykrVHEsZ+4oXGJFUDMHipnBjmMf3/ffs="),
])
def test_aes_independent_openssl_vectors(password, expected):
    # Generated with openssl enc -aes-128-cbc: key/IV hex
    # 773952384167314b694c3070764d4863, default PKCS#7, UTF-8 input.
    assert probe.encode_password(password) == expected


@pytest.mark.parametrize("path,expected", [
    ("/auth/oauth2/token", "5e76aec0e7367e46d10b0d799ca2989c78ecb70b64bfbfbe4d9fcdd72c868f7b"),
    ("/tsp/v1/app/vmc/queryList", "80b6e1e34fb40ce81b88ec81564b2d4b45ef3754b6b58a67643503d3ff062a12"),
])
def test_bff_signature_independent_vectors(path, expected):
    # Fixed SHA-256 vectors; do not derive expectations from probe constants.
    headers = probe.bff_headers(path, "44", timestamp=1700000000123)
    assert headers["signature"] == expected
    assert headers["timestamp"] == "1700000000123"
    assert headers["url"] == path
    assert headers["nonce"] == "chery_legend_h5"
    assert headers["Authorization"] == "Basic bGVnZW5kQXBwOmxlZ2VuZEFwcA=="
    assert headers["DEPT-ID"] == "44"


def test_tsp_signature_independent_vector():
    # SHA-256/base64 uppercase of this independently specified canonical input:
    # appId=eu-1&vin=LTEST123456789012&secretKey=EUProd89ec59274d23491084af&timestamp=1700000000123
    assert probe.realtime_body("LTEST123456789012", 1700000000123) == {
        "vin": "LTEST123456789012", "appId": "eu-1",
        "sign": "GQK6PXJRMF5AEGH99AW7SNBPL48O5H1/BSUELQHBPWA=",
    }


@pytest.mark.parametrize("reply", [{"access_token": TOKEN}, {"data": {"access_token": TOKEN}}])
def test_login_credentials_are_body_only(reply):
    session = Session(Response(reply))
    client = probe.Client("44", session)
    client.login(EMAIL, PASSWORD)
    url, kwargs = session.calls[0]
    assert url == "https://legend-oj.omodaauto.nl/api/auth/oauth2/token"
    assert "?" not in url and "params" not in kwargs
    assert EMAIL not in url and PASSWORD not in url
    assert kwargs["data"] == {
        "username": EMAIL, "password": probe.encode_password(PASSWORD),
        "grant_type": "password", "scope": "server", "needDecode": "1", "loginType": "email",
    }
    assert PASSWORD not in json.dumps(kwargs)
    assert client.access_token == TOKEN
    assert not hasattr(client, "password")
    assert session.trust_env is False


def test_fixed_read_only_allowlist():
    assert probe.ALLOWED_ROUTES == {
        "token": ("https://legend-oj.omodaauto.nl/api", "/auth/oauth2/token"),
        "vehicles": ("https://legend-oj.omodaauto.nl/api", "/tsp/v1/app/vmc/queryList"),
        "tsp_login": ("https://legend-oj.omodaauto.nl/api", "/tsp/v1/app/auth/login"),
        "realtime": ("https://tspconsole-eu.cheryinternational.com", "/asr/manager/realtime"),
    }


@pytest.mark.parametrize("route", ["unlock", "wake", "locate", "pin", "https://example.invalid", "/asr/manager/realtime"])
def test_arbitrary_endpoints_rejected_without_requests(route):
    session = Session()
    with pytest.raises(probe.ProbeError, match="allowlist"):
        probe.Client("44", session)._post(route, {})
    assert session.calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500, 503])
def test_http_errors_no_redirect_or_retry(status):
    session = Session(Response({"access_token": TOKEN}, status=status))
    with pytest.raises(probe.ProbeError, match=f"HTTP {status}"):
        probe.Client("44", session).login(EMAIL, PASSWORD)
    assert len(session.calls) == 1
    kwargs = session.calls[0][1]
    assert kwargs["allow_redirects"] is False
    assert kwargs["verify"] is True
    assert kwargs["timeout"] == (10, 30)


def test_default_requests_adapter_has_no_retries():
    client = probe.Client("44")
    try:
        assert client.session.trust_env is False
        for adapter in client.session.adapters.values():
            assert adapter.max_retries.total == 0
    finally:
        client.close()


def test_owner_delegate_discovery_nested_and_deduplicated():
    reply = {"data": {
        "controlCarList": [{"vin": VIN.lower(), "role": "owner"}, None, "junk"],
        "authorizedControlCarList": [{"VIN": VIN, "role": "delegate"}, {"vin": OTHER_VIN}],
        "carList": [{"vin": "not-a-vin"}, {"vin": "I" * 17}, {"vin": 12}],
    }}
    session = Session(Response(reply))
    client = probe.Client("44", session)
    client.access_token = TOKEN
    assert client.discover() == reply
    assert [v["vin"] for v in client.vehicles] == [VIN, OTHER_VIN]
    assert client.vehicles[0]["role"] == "owner"
    assert session.calls[0][1]["json"] == {}
    assert session.calls[0][1]["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("reply", [
    {"data": [{"vin": VIN}]}, {"data": {"VIN": VIN}},
    {"data": {"vehicles": [{"vin": VIN}]}}, {"data": {"list": [{"vin": VIN}]}},
])
def test_known_discovery_envelopes(reply):
    assert [v["vin"] for v in probe.extract_vehicles(reply)] == [VIN]


@pytest.mark.parametrize("reply", [{}, {"data": None}, {"data": "junk"}, {"body": [{"vin": VIN}]}, {"untrusted": {"vin": VIN}}])
def test_unknown_discovery_envelopes_fail_closed(reply):
    assert probe.extract_vehicles(reply) == []


@pytest.mark.parametrize("logged_in,discovered", [(False, True), (True, False), (False, False)])
def test_telemetry_rejects_undiscovered_or_unauthenticated_vin(logged_in, discovered):
    session = Session()
    client = probe.Client("44", session)
    client.access_token = TOKEN if logged_in else None
    client.vehicles = [{"vin": OTHER_VIN}] if discovered else []
    with pytest.raises(probe.ProbeError, match="discovered"):
        client.realtime({"vin": VIN})
    assert session.calls == []


def test_realtime_uses_only_discovered_vin_and_two_fixed_posts(monkeypatch):
    monkeypatch.setattr(probe.time, "time", lambda: 1700000000.123)
    telemetry = {"data": {"soc": 54}}
    session = Session(Response({"data": {"userToken": USER_TOKEN}}), Response(telemetry))
    client = probe.Client("44", session)
    client.access_token = TOKEN
    client.vehicles = [{"vin": VIN}]
    assert client.realtime(client.vehicles[0]) == telemetry
    assert len(session.calls) == 2
    assert session.calls[0][0] == "https://legend-oj.omodaauto.nl/api/tsp/v1/app/auth/login"
    assert session.calls[0][1]["json"] == {"channelId": "1"}
    url, kwargs = session.calls[1]
    assert url == "https://tspconsole-eu.cheryinternational.com/asr/manager/realtime"
    assert kwargs["headers"]["Authorization"] == USER_TOKEN
    assert kwargs["json"]["vin"] == VIN
    assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
    assert "timestamp" not in kwargs["json"]
    client.close()
    assert client.access_token is None and client.user_token is None and session.closed


@pytest.mark.parametrize("response", [
    Response(error=ValueError(f"malformed {TOKEN} {PASSWORD}")),
    Response([TOKEN, PASSWORD]), Response(TOKEN), Response(None),
    requests.ConnectionError(f"https://example.invalid/?password={PASSWORD}&token={TOKEN}"),
    requests.Timeout(TOKEN), requests.exceptions.SSLError(PASSWORD),
])
def test_bad_json_and_network_exceptions_do_not_leak(response, capsys):
    session = Session(response)
    with pytest.raises(probe.ProbeError) as error:
        probe.Client("44", session).login(EMAIL, PASSWORD)
    assert len(session.calls) == 1
    combined = str(error.value) + str(error.value.__cause__) + str(error.value.__context__ if not error.value.__suppress_context__ else "")
    captured = capsys.readouterr()
    combined += captured.out + captured.err
    for secret in (TOKEN, PASSWORD, EMAIL):
        assert secret not in combined


@pytest.mark.parametrize("reply", [{}, {"access_token": 123}, {"data": {"access_token": ""}}])
def test_missing_login_token_is_safe_and_not_retried(reply):
    session = Session(Response(reply))
    with pytest.raises(probe.ProbeError, match="access token"):
        probe.Client("44", session).login(EMAIL, PASSWORD)
    assert len(session.calls) == 1


def test_discovery_requires_login_without_requests():
    session = Session()
    with pytest.raises(probe.ProbeError, match="Login is required"):
        probe.Client("44", session).discover()
    assert session.calls == []


@pytest.mark.parametrize("reply", [{}, {"data": {"userToken": None}}, {"body": {"userToken": 123}}])
def test_missing_tsp_token_prevents_telemetry_request(reply):
    session = Session(Response(reply))
    client = probe.Client("44", session)
    client.access_token = TOKEN
    client.vehicles = [{"vin": VIN}]
    with pytest.raises(probe.ProbeError, match="no token"):
        client.realtime(client.vehicles[0])
    assert len(session.calls) == 1
    assert client.user_token is None


def test_cli_network_error_output_is_safe_and_client_closed(monkeypatch, capsys):
    session = Session(requests.ConnectionError(f"{EMAIL} {PASSWORD} {TOKEN}"))
    client = probe.Client("44", session)
    monkeypatch.setattr(probe, "Client", lambda country: client)
    monkeypatch.setattr(probe.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt: PASSWORD)
    assert probe.main(["--email", EMAIL]) == 1
    captured = capsys.readouterr()
    for secret in (EMAIL, PASSWORD, TOKEN):
        assert secret not in captured.out + captured.err
    assert "network/TLS failure" in captured.err
    assert len(session.calls) == 1 and session.closed


def test_reduced_report_contains_no_vin_or_gps_values():
    discovery = {"data": {"controlCarList": [{"vin": VIN, "owner": EMAIL}]}}
    realtime = {"data": {
        "soc": 54, "timestamp": 1700000000123,
        "vin": VIN, "latitude": 51.123456, "longitude": -1.987654,
        "position": {"gps": [51.123456, -1.987654]},
        "doorLock": {"vin": VIN}, "secret": TOKEN,
    }}
    report = probe.make_report(discovery, [{"vin": VIN}], realtime)
    serialized = json.dumps(report)
    for sensitive in (VIN, EMAIL, TOKEN, "51.123456", "-1.987654"):
        assert sensitive not in serialized
    assert report["observed_fields"] == {"soc": 54, "timestamp": 1700000000123}
    assert report["vehicle_count"] == 1
    assert report["realtime_schema"]["data"]["vin"] == "string"


@pytest.mark.parametrize("value", [VIN, EMAIL, TOKEN, "GPS: 51.123456,-1.987654", "untrusted server text"])
def test_report_rejects_nonnumeric_strings_in_numeric_telemetry(value):
    report = probe.make_report({}, [{"vin": VIN}], {"data": {"temperature": value, "soc": 54}})
    assert report["observed_fields"] == {"soc": 54}
    assert value not in json.dumps(report)
    assert report["realtime_schema"]["data"]["temperature"] == "string"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "NaN", "Infinity", "1e999"])
def test_report_rejects_nonfinite_telemetry(value):
    report = probe.make_report({}, [], {"data": {"temperature": value}})
    assert report["observed_fields"] == {}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("value", [0, "0", 54, 54.5, "54.5", "-2e1", True, None])
def test_report_retains_safe_numeric_boolean_null_telemetry(value):
    report = probe.make_report({}, [], {"data": {"temperature": value}})
    assert report["observed_fields"] == {"temperature": value}


@pytest.mark.parametrize("value,allowed", [
    ("2026-10-01T12:00:00Z", True), ("2026-10-01", True),
    ("1700000000123", True), (VIN, False), ("yesterday at my house", False),
])
def test_report_timestamp_strings_are_numeric_or_parseable_dates(value, allowed):
    report = probe.make_report({}, [{"vin": VIN}], {"data": {"timestamp": value}})
    assert ("timestamp" in report["observed_fields"]) is allowed


def test_report_status_summaries_do_not_echo_server_messages():
    message = f"private server message {EMAIL} {VIN} {TOKEN}"
    report = probe.make_report(
        {"code": 403, "success": False, "msg": message}, [],
        {"code": "OK", "success": True, "message": message},
    )
    assert report["discovery_status"] == {"code": 403, "success": False}
    assert report["realtime_status"] == {"code": "OK", "success": True}
    serialized = json.dumps(report)
    for secret in (message, EMAIL, VIN, TOKEN):
        assert secret not in serialized


def test_redaction_nested_keys_and_secret_substrings():
    raw = {"access_token": TOKEN, "nested": [
        {"Authorization": f"Bearer {TOKEN}", "refresh-token": "synthetic-refresh",
         "password": PASSWORD, "PIN": "synthetic-pin", "Cookie": "synthetic-cookie"},
        {"message": f"{EMAIL} / {TOKEN} / {USER_TOKEN}", "soc": 42},
    ]}
    redacted = probe.redact(raw, (EMAIL, TOKEN, USER_TOKEN, ""))
    serialized = json.dumps(redacted)
    for secret in (EMAIL, TOKEN, USER_TOKEN, PASSWORD, "synthetic-refresh", "synthetic-pin", "synthetic-cookie"):
        assert secret not in serialized
    assert redacted["nested"][1]["soc"] == 42
    assert raw["access_token"] == TOKEN  # Does not mutate the input.


def test_private_output_owner_only_even_with_permissive_umask(tmp_path):
    path = tmp_path / "private" / "report.json"
    old_umask = os.umask(0)
    try:
        probe.save_private(path, {"soc": 54})
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"soc": 54}


def test_private_output_refuses_overwrite(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("original")
    with pytest.raises(FileExistsError):
        probe.save_private(path, {"soc": 54})
    assert path.read_text() == "original"


@pytest.mark.parametrize("target_exists", [True, False])
def test_private_output_refuses_symlink(tmp_path, target_exists):
    target = tmp_path / "target.json"
    if target_exists:
        target.write_text("original")
    link = tmp_path / "report.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        probe.save_private(link, {"soc": 54})
    assert link.is_symlink()
    if target_exists:
        assert target.read_text() == "original"
    else:
        assert not target.exists()


def test_cli_mocked_happy_path_private_outputs_no_secrets(monkeypatch, capsys, tmp_path):
    discovery = {"code": 0, "data": {"controlCarList": [{
        "vin": VIN, "owner": EMAIL, "nested": {"access_token": TOKEN},
    }]}}
    realtime = {"code": 0, "data": {
        "soc": "0", "temperature": 20.5, "timestamp": "2026-10-01T12:00:00Z",
        "vin": VIN, "latitude": 51.123456, "longitude": -1.987654,
        "nested": [{"userToken": USER_TOKEN, "message": f"{EMAIL} {TOKEN}"}],
    }}
    session = Session(
        Response({"access_token": TOKEN, "refresh_token": "synthetic-refresh"}),
        Response(discovery), Response({"data": {"userToken": USER_TOKEN}}), Response(realtime),
    )
    client = probe.Client("44", session)
    monkeypatch.setattr(probe, "Client", lambda country: client)
    monkeypatch.setattr(probe.sys.stdin, "isatty", lambda: True)
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt: PASSWORD)
    report_path = tmp_path / "captures" / "report.json"
    raw_path = tmp_path / "captures" / "raw.json"
    assert probe.main([
        "--email", EMAIL, "--telemetry", "--output", str(report_path), "--raw-output", str(raw_path),
    ]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Account login succeeded" in captured.out
    assert "Discovered 1 vehicle(s)" in captured.out
    assert len(prompts) == 1
    report = json.loads(report_path.read_text())
    raw = json.loads(raw_path.read_text())
    assert report["observed_fields"] == {
        "soc": "0", "temperature": 20.5, "timestamp": "2026-10-01T12:00:00Z",
    }
    assert report["telemetry_requested"] is True
    assert report["vehicle_count"] == 1
    assert set(raw) == {"discovery", "realtime"}  # Never capture login replies.
    assert raw["discovery"]["data"]["controlCarList"][0]["vin"] == VIN
    assert raw["realtime"]["data"]["latitude"] == 51.123456  # Raw capture is explicitly private.
    combined = captured.out + captured.err + report_path.read_text() + raw_path.read_text()
    for secret in (EMAIL, PASSWORD, TOKEN, USER_TOKEN, "synthetic-refresh"):
        assert secret not in combined
    public = captured.out + report_path.read_text()
    for private in (VIN, "51.123456", "-1.987654"):
        assert private not in public
    for path in (report_path, raw_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(session.calls) == 4
    for _, kwargs in session.calls:
        assert kwargs["verify"] is True
        assert kwargs["allow_redirects"] is False
    assert session.closed and client.access_token is None and client.user_token is None


@pytest.mark.parametrize("telemetry", [False, True])
def test_plan_no_client_prompts_network_or_files(monkeypatch, capsys, tmp_path, telemetry):
    def forbidden(*args, **kwargs):
        pytest.fail("--plan must not create clients, prompt, or write")

    monkeypatch.setattr(probe, "Client", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(probe.getpass, "getpass", forbidden)
    monkeypatch.setattr(probe, "save_private", forbidden)
    output = tmp_path / "report.json"
    args = ["--plan", "--output", str(output)] + (["--telemetry"] if telemetry else [])
    assert probe.main(args) == 0
    text = capsys.readouterr().out
    assert "No network requests made" in text
    assert "https://legend-oj.omodaauto.nl/api/auth/oauth2/token" in text
    assert "https://legend-oj.omodaauto.nl/api/tsp/v1/app/vmc/queryList" in text
    assert ("https://tspconsole-eu.cheryinternational.com/asr/manager/realtime" in text) is telemetry
    assert not output.exists()
