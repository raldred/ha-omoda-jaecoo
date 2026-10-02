"""Synthetic four-way authentication tests. All socket access is forbidden."""

import asyncio
import base64
import hashlib
import importlib
import io
import json
import socket
import sys
import threading
import types
from pathlib import Path

import aiohttp
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageChops, ImageDraw, ImageFilter

# A synthetic package permits relative imports without importing HA's __init__.
_PACKAGE = "_offline_auth_client"
package = types.ModuleType(_PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components/omoda_jaecoo")]
sys.modules[_PACKAGE] = package
api = importlib.import_module(_PACKAGE + ".api")
identity = importlib.import_module(_PACKAGE + ".identity")
captcha = importlib.import_module(_PACKAGE + ".captcha")
otp = importlib.import_module(_PACKAGE + ".otp")
EMAIL = "synthetic@example.invalid"
PHONE = "07700 900123"
PASSWORD = "not-a-real-password"
CODE = "001234"
TOKEN_RESPONSE = {
    "access_token": "test-access",
    "refresh_token": "test-refresh",
    "expires_in": 3600,
}


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Socket access forbidden in synthetic auth tests")

    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, blocked)
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)


class Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class Response:
    def __init__(self, data=None, status=200, *, body=None, headers=None, error=None):
        self.data, self.status, self.error = data, status, error
        self.headers = headers or {}
        self.content = Content(json.dumps(data).encode() if body is None else body)
        self.content_length = len(self.content.body)
        self.released = False

    async def __aenter__(self):
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

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.responses, "Unexpected request or unsafe retry"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def run(coro):
    return asyncio.run(coro)


def image_b64(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def puzzle(*, ambiguous=False):
    piece = Image.new("RGBA", (35, 30), (0, 0, 0, 0))
    draw = ImageDraw.Draw(piece)
    draw.ellipse((4, 4, 26, 23), fill=(120, 120, 120, 255))
    draw.rectangle((14, 4, 26, 13), fill=(0, 0, 0, 0))
    alpha = piece.getchannel("A")
    silhouette = alpha.crop(alpha.getbbox())
    outline = ImageChops.subtract(
        silhouette.filter(ImageFilter.MaxFilter(3)),
        silhouette.filter(ImageFilter.MinFilter(3)),
    )
    background = Image.new("RGB", (160, 80), (30, 30, 30))
    background.paste(Image.merge("RGB", (outline, outline, outline)), (75, 20))
    if ambiguous:
        background.paste(Image.merge("RGB", (outline, outline, outline)), (115, 20))
    return {
        "originalImageBase64": image_b64(background),
        "jigsawImageBase64": image_b64(piece),
        "token": "synthetic-challenge",
        "secretKey": "0123456789abcdef",
    }


def challenge_responses(*, final=None):
    return [
        Response({"data": {"repData": puzzle()}}),
        Response({"data": {"repCode": "0000"}}),
        final or Response({"key": "operation.successful", "ok": True}),
    ]


def decrypt(encoded, algorithm, key, mode):
    decryptor = Cipher(algorithm(key), mode).decryptor()
    plain = decryptor.update(base64.b64decode(encoded)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(plain) + unpadder.finalize()).decode()


@pytest.mark.parametrize(
    "value,country,national,e164",
    [
        (PHONE, "44", "7700900123", "+447700900123"),
        ("+44 7700 900123", "44", "7700900123", "+447700900123"),
        ("0044 7700 900123", "44", "7700900123", "+447700900123"),
        ("02 1234 5678", "39", "0212345678", "+390212345678"),
        ("+39 02 1234 5678", "39", "0212345678", "+390212345678"),
        ("030 123456", "49", "30123456", "+4930123456"),
        ("06 12 34 56 78", "33", "612345678", "+33612345678"),
    ],
)
def test_phone_canonicalization(value, country, national, e164):
    assert identity.normalize_phone(value, country) == national
    assert identity.phone_identity(value, country) == e164
    assert identity.normalize_phone(national, country) == national


@pytest.mark.parametrize(
    "value,country",
    [
        ("+39 02 1234 5678", "44"),
        ("123", "44"),
        ("", "44"),
        ("+44 7700 900123 ext. 1", "44"),
        ("abc", "44"),
        (None, "44"),
        (PHONE, "+44"),
        (PHONE, "999"),
        ("++447700900123", "44"),
        (PHONE, 44),
        (PHONE + "\nsecret", "44"),
    ],
)
def test_phone_rejections_safe(value, country):
    with pytest.raises(identity.InvalidPhoneNumber, match=r"^Invalid phone number\.$"):
        identity.normalize_phone(value, country)


@pytest.mark.parametrize("account_type", ["email", "phone"])
def test_password_grants_preserved(account_type):
    session = Session(Response(TOKEN_RESPONSE))
    received = []
    obj = api.JaecooApi(session, on_tokens=received.append)
    tokens = run(
        obj.async_login(EMAIL, PASSWORD)
        if account_type == "email"
        else obj.async_login_phone(PHONE, PASSWORD)
    )
    assert tokens is obj.tokens and received == [tokens]
    url, kwargs = session.calls[0]
    assert url == api.BFF + api.TOKEN_PATH
    assert "params" not in kwargs
    body = kwargs["data"]
    expected = {
        "username": EMAIL if account_type == "email" else "7700900123",
        "password": api._encode_password(PASSWORD),
        "grant_type": "password",
        "scope": "server",
        "needDecode": "1",
        "loginType": "email" if account_type == "email" else "mobile",
    }
    if account_type == "phone":
        expected["areaCode"] = "44"
    assert body == expected
    assert (
        decrypt(body["password"], algorithms.AES, api.AES_KEY, modes.CBC(api.AES_KEY))
        == PASSWORD
    )
    assert (
        EMAIL not in repr(vars(obj))
        and PASSWORD not in repr(vars(obj))
        and PHONE not in repr(vars(obj))
    )


@pytest.mark.parametrize(
    "account_type,identifier,field,expected,location",
    [
        ("email", EMAIL, "email", "APP-LOGIN@" + EMAIL, "params"),
        ("phone", PHONE, "mobile", "APP-LOGIN@44_7700900123", "data"),
        ("phone", "+447700900123", "mobile", "APP-LOGIN@44_7700900123", "data"),
    ],
)
def test_otp_verification_grants(
    account_type, identifier, field, expected, location, monkeypatch
):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000.123)
    received = []
    session = Session(Response({"data": TOKEN_RESPONSE}))
    obj = api.JaecooApi(session, on_tokens=received.append)
    tokens = run(obj.async_login_otp(identifier, CODE, account_type))
    assert tokens is obj.tokens and received == [tokens]
    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url == api.BFF + api.TOKEN_PATH
    other = "params" if location == "data" else "data"
    assert other not in request
    body = request[location]
    assert body == {
        field: expected,
        "code": otp.encode_code(CODE),
        "needDecode": "0",
        "grant_type": field,
        "scope": "server",
        "loginType": field,
        "loginAction": "1",
    }
    assert decrypt(body["code"], algorithms.SM4, otp.SM4_KEY, modes.ECB()) == CODE
    assert (
        request["headers"]["signature"]
        == "5e76aec0e7367e46d10b0d799ca2989c78ecb70b64bfbfbe4d9fcdd72c868f7b"
    )
    assert request["headers"]["DEPT-ID"] == "44"
    assert CODE not in repr(vars(obj)) and identifier not in repr(vars(obj))
    assert set(tokens.to_dict()) == {"access_token", "refresh_token", "expires_at"}


def test_sm4_standard_independent_vector():
    key = bytes.fromhex("0123456789abcdeffedcba9876543210")
    encryptor = Cipher(algorithms.SM4(key), modes.ECB()).encryptor()
    assert (
        encryptor.update(key) + encryptor.finalize()
    ).hex() == "681edf34d206965e86b3e94f536e4246"


@pytest.mark.parametrize(
    "code", ["123", "123456789", " 001234", "１２３４", "12a4", None, 1234]
)
def test_invalid_code_no_network(code):
    session = Session()
    with pytest.raises(api.AuthenticationError):
        run(api.JaecooApi(session).async_login_otp(EMAIL, code, "email"))
    assert not session.calls


@pytest.mark.parametrize(
    "response",
    [
        Response(status=401),
        Response(status=424),
        Response({"error": "invalid_grant"}, status=400),
        Response({"key": "invalid_token"}),
    ],
)
def test_wrong_or_expired_otp_no_retry(response):
    session = Session(response)
    with pytest.raises(api.AuthenticationError):
        run(api.JaecooApi(session).async_login_otp(EMAIL, CODE, "email"))
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "account_type,identifier", [("email", EMAIL), ("phone", PHONE)]
)
def test_explicit_otp_one_captcha_one_send(
    account_type, identifier, monkeypatch, caplog
):
    main_thread = threading.get_ident()
    worker_threads = []
    original = otp.solve_challenge

    def worker(rep):
        worker_threads.append(threading.get_ident())
        return original(rep)

    monkeypatch.setattr(otp, "solve_challenge", worker)
    monkeypatch.setattr(otp.time, "time", lambda: 1700000000.123)
    responses = challenge_responses()
    session = Session(*responses)
    obj = api.JaecooApi(session)
    assert not session.calls  # importing/constructing must not request anything
    assert run(obj.async_request_otp(identifier, account_type)) is None
    assert worker_threads and worker_threads[0] != main_thread
    assert obj.tokens is None
    assert len(session.calls) == 3 and all(r.released for r in responses)
    create, check, send = session.calls
    assert create[0] == api.BFF + "/code/create"
    assert create[1]["json"] == {"captchaType": "blockPuzzle"}
    assert check[0] == api.BFF + "/code/check"
    params = check[1]["params"]
    assert set(params) == {"captchaType", "pointJson", "token"}
    point = decrypt(
        params["pointJson"], algorithms.AES, b"0123456789abcdef", modes.ECB()
    )
    assert point == '{"x":71,"y":5}'
    headers = check[1]["headers"]
    assert headers["keys"] == "captchaType,pointJson,token"
    signed = (
        otp.MARKETING_SECRET
        + otp.MARKETING_NONCE
        + "/code/check"
        + "1700000000123["
        + ",".join(params.values())
        + "]"
    )
    assert headers["signature"] == hashlib.md5(signed.encode()).hexdigest()
    assert send[0] == api.BFF + (
        "/marketing/v2/app/code/sendMailCode"
        if account_type == "email"
        else "/marketing/v2/app/code/sendSmsCode"
    )
    body = send[1]["data"]
    assert body["module"] == "APP-LOGIN"
    assert (
        decrypt(
            body["captchaVerification"],
            algorithms.AES,
            b"0123456789abcdef",
            modes.ECB(),
        )
        == params["token"] + "---" + point
    )
    if account_type == "email":
        assert body["email"] == EMAIL and "areaCode" not in body
    else:
        assert body["mobile"] == "7700900123" and body["areaCode"] == "44"
    for _, kwargs in session.calls:
        assert kwargs["ssl"] is True and kwargs["allow_redirects"] is False
        assert kwargs["raise_for_status"] is False
        assert kwargs["timeout"].total == 20
    assert not caplog.text
    assert "challenge" not in repr(vars(obj)) and identifier not in repr(vars(obj))


@pytest.mark.parametrize(
    "final,expected",
    [
        (Response({"ok": False, "msg": PASSWORD}), api.OtpDeliveryError),
        (Response({"key": "email.not.exists", "msg": PASSWORD}), api.OtpDeliveryError),
        (Response(status=403, body=b"backend anti-bot page"), api.OtpDeliveryError),
        (Response(status=302), api.OtpDeliveryError),
        (Response(status=503), api.OtpDeliveryUnknown),
        (Response(body=b"not json"), api.OtpDeliveryUnknown),
        (Response({}), api.OtpDeliveryUnknown),
        (Response([PASSWORD]), api.OtpDeliveryUnknown),
        (Response({"code": "0"}), api.OtpDeliveryUnknown),
        (aiohttp.ClientConnectionError(PASSWORD), api.OtpDeliveryUnknown),
        (TimeoutError(PASSWORD), api.OtpDeliveryUnknown),
        (aiohttp.ClientSSLError(None, OSError(PASSWORD)), api.OtpDeliveryError),
    ],
)
def test_send_failures_no_retry_safe(final, expected, caplog):
    session = Session(*challenge_responses(final=final))
    with pytest.raises(expected) as caught:
        run(api.JaecooApi(session).async_request_otp(PHONE, "phone"))
    assert len(session.calls) == 3
    assert PASSWORD not in str(caught.value) and not caplog.text
    assert caught.value.__cause__ is None
    if isinstance(final, BaseException):
        assert caught.value.__suppress_context__


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_retry_after_every_stage(stage):
    responses = challenge_responses()
    responses[stage] = Response(status=429, headers={"Retry-After": "120"})
    session = Session(*responses)
    with pytest.raises(api.RateLimitError) as caught:
        run(api.JaecooApi(session).async_request_otp(EMAIL, "email"))
    assert caught.value.retry_after == 120
    assert len(session.calls) == stage + 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("60", 60),
        ("0", 0),
        ("-1", 0),
        ("invalid", None),
        ("nan", None),
        ("inf", None),
        (None, None),
        ("Tue, 14 Nov 2023 22:15:20 GMT", 120),
    ],
)
def test_retry_after_parser(value, expected, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000)
    assert api._retry_after(value) == expected
    assert api.RateLimitError().retry_after is None
    assert str(api.RateLimitError("same constructor")) == "same constructor"


def test_token_rate_limit_header():
    session = Session(Response(status=429, headers={"Retry-After": "90"}))
    with pytest.raises(api.RateLimitError) as caught:
        run(api.JaecooApi(session).async_login_otp(EMAIL, CODE, "email"))
    assert caught.value.retry_after == 90


@pytest.mark.parametrize(
    "created", [{}, {"data": {}}, {"data": {"repData": {}}}, {"data": []}]
)
def test_bad_create_never_check_or_send(created):
    session = Session(Response(created))
    with pytest.raises(api.CaptchaError):
        run(api.JaecooApi(session).async_request_otp(EMAIL, "email"))
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "checked",
    [{}, {"data": {"repCode": "0001"}}, {"data": {"repCode": 0}}, {"data": None}],
)
def test_unconfirmed_captcha_never_send(checked):
    session = Session(challenge_responses()[0], Response(checked))
    with pytest.raises(api.CaptchaError):
        run(api.JaecooApi(session).async_request_otp(EMAIL, "email"))
    assert len(session.calls) == 2


@pytest.mark.parametrize("sending", [False, True])
def test_response_size_cap(sending):
    cap = otp.MAX_DELIVERY_RESPONSE if sending else otp.MAX_CAPTCHA_RESPONSE
    response = Response(body=b" " * (cap + 1))
    response.content_length = None  # also enforce cap for chunked decompressed data
    session = Session(response)
    with pytest.raises(api.OtpDeliveryUnknown if sending else api.CaptchaError):
        run(
            otp._post(
                session,
                "/marketing/v2/app/code/sendMailCode" if sending else "/code/create",
                sending=sending,
            )
        )
    assert len(session.calls) == 1


def test_captcha_images_and_work_limits(monkeypatch):
    rep = puzzle()
    assert captcha.solve_gap(rep["originalImageBase64"], rep["jigsawImageBase64"]) == 71
    ambiguous = puzzle(ambiguous=True)
    with pytest.raises(api.CaptchaError):
        captcha.solve_challenge(ambiguous)
    with pytest.raises(api.CaptchaError):
        captcha.solve_gap(
            rep["originalImageBase64"], image_b64(Image.new("RGBA", (35, 30)))
        )
    with pytest.raises(api.CaptchaError):
        captcha.solve_gap(
            image_b64(Image.new("RGB", (160, 80))), rep["jigsawImageBase64"]
        )
    with pytest.raises(api.CaptchaError):
        captcha.solve_gap("!invalid!", rep["jigsawImageBase64"])
    with pytest.raises(api.CaptchaError):
        captcha.solve_gap(
            "a" * (captcha.MAX_ENCODED_BYTES + 1), rep["jigsawImageBase64"]
        )
    with pytest.raises(api.CaptchaError):
        captcha.solve_gap(
            image_b64(Image.new("RGB", (captcha.MAX_WIDTH + 1, 10))),
            rep["jigsawImageBase64"],
        )
    monkeypatch.setattr(captcha, "MAX_WORK", 10)
    with pytest.raises(api.CaptchaError):
        captcha.solve_challenge(rep)


@pytest.mark.parametrize("key", ["short", "x" * 17, None])
def test_captcha_invalid_key_safe(key):
    with pytest.raises(api.CaptchaError):
        captcha.aes_b64("synthetic", key)


def test_otp_routes_strict_and_read_allowlist_unchanged():
    assert set(api.ALLOWED_ROUTES) == {
        "token",
        "vehicles",
        "tsp_login",
        "realtime",
        "location",
        "charge_schedule",
        "charge_depth",
    }
    assert otp.OTP_ROUTES == {
        "/code/create",
        "/code/check",
        "/marketing/v2/app/code/sendMailCode",
        "/marketing/v2/app/code/sendSmsCode",
    }
    session = Session()
    for path in (
        "https://example.invalid",
        "/code/create?secret=1",
        "/command",
        api.TOKEN_PATH,
    ):
        with pytest.raises(api.ApiError):
            run(otp._post(session, path))
        with pytest.raises(api.ApiError):
            otp.marketing_headers(path)
    assert not session.calls


@pytest.mark.parametrize("account_type", ["mobile", "sms", "other", "", None])
def test_invalid_account_type_no_network(account_type):
    session = Session()
    obj = api.JaecooApi(session)
    with pytest.raises(api.ApiError):
        run(obj.async_request_otp(EMAIL, account_type))
    with pytest.raises(api.ApiError):
        run(obj.async_login_otp(EMAIL, CODE, account_type))
    assert not session.calls


def test_transport_query_exception_redacted(caplog):
    secret_url = api.BFF + api.TOKEN_PATH + "?email=" + EMAIL + "&code=" + CODE
    session = Session(aiohttp.ClientConnectionError(secret_url))
    with pytest.raises(api.CannotConnect) as caught:
        run(api.JaecooApi(session).async_login_otp(EMAIL, CODE, "email"))
    assert EMAIL not in str(caught.value) and CODE not in str(caught.value)
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
    assert not caplog.text and len(session.calls) == 1


@pytest.mark.parametrize(
    "account_type,identifier", [("email", EMAIL), ("phone", PHONE)]
)
def test_otp_token_poll_and_refresh_never_send_code(
    account_type, identifier, monkeypatch
):
    now = [1000]
    monkeypatch.setattr(api.time, "time", lambda: now[0])
    session = Session(
        Response(
            {"access_token": "first", "refresh_token": "refresh", "expires_in": 3600}
        ),
        Response({"data": []}),
        Response(
            {"access_token": "rotated", "refresh_token": "refresh", "expires_in": 3600}
        ),
        Response({"data": []}),
    )
    obj = api.JaecooApi(session)

    async def scenario():
        await obj.async_login_otp(identifier, CODE, account_type)
        assert await obj.async_list_vehicles() == []
        now[0] = 4600
        assert await obj.async_list_vehicles() == []

    run(scenario())
    assert len(session.calls) == 4
    assert all(
        url in {api.BFF + api.TOKEN_PATH, api.BFF + api.VEHICLES_PATH}
        for url, _ in session.calls
    )
    assert session.calls[2][1]["params"]["grant_type"] == "refresh_token"
    assert "code" not in session.calls[2][1]["params"]
    assert obj.tokens.access_token == "rotated"


def test_cancellation_propagates():
    session = Session(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run(api.JaecooApi(session).async_request_otp(EMAIL, "email"))
    assert len(session.calls) == 1
