"""Explicit one-attempt OTP delivery on a separate, fixed marketing allowlist.

No account polling, retries, fingerprint changes, environment overrides, or
unverified TLS. The caller owns the session; nothing performs I/O on import.
Wire reference: upstream core/{captcha_solver,login_omoda,prova_token}.py.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .api import (
    APP_BASIC,
    APP_VERSION,
    BFF,
    ApiError,
    AuthenticationError,
    CaptchaError,
    OtpDeliveryError,
    OtpDeliveryUnknown,
    RateLimitError,
    _retry_after,
)
from .captcha import solve_challenge
from .identity import normalize_phone

SM4_KEY = b"mHU80av2zFtf4OY6"
MARKETING_SECRET = "5c7af05e6fbf562842ef483ee96e06a0"
MARKETING_NONCE = "chery_legend_marketing"
OTP_ROUTES = frozenset(
    {
        "/code/create",
        "/code/check",
        "/marketing/v2/app/code/sendMailCode",
        "/marketing/v2/app/code/sendSmsCode",
    }
)
MAX_CAPTCHA_RESPONSE = 1_200_000
MAX_DELIVERY_RESPONSE = 65_536


def _identifier(identifier: str, account_type: str, country_code: str) -> str:
    if account_type == "phone":
        return normalize_phone(identifier, country_code)
    if account_type != "email":
        raise ApiError("Invalid account type.")
    if (
        not isinstance(identifier, str)
        or not identifier
        or len(identifier) > 254
        or identifier.count("@") != 1
        or not all(identifier.split("@"))
        or any(ord(c) < 33 or ord(c) == 127 for c in identifier)
    ):
        raise AuthenticationError("A valid account identifier is required.")
    return identifier


def encode_code(code: str) -> str:
    """SM4 ECB/PKCS7 of the UTF-8 OTP itself (never PIN MD5)."""
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,8}", code):
        raise AuthenticationError("A valid verification code is required.")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(code.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.SM4(SM4_KEY), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode(
        "ascii"
    )


def token_fields(
    identifier: str, code: str, account_type: str, country_code: str
) -> dict[str, str]:
    identifier = _identifier(identifier, account_type, country_code)
    mobile = account_type == "phone"
    kind = "mobile" if mobile else "email"
    identity = f"{country_code}_{identifier}" if mobile else identifier
    return {
        kind: "APP-LOGIN@" + identity,
        "code": encode_code(code),
        "needDecode": "0",
        "grant_type": kind,
        "scope": "server",
        "loginType": kind,
        "loginAction": "1",
    }


def marketing_headers(
    path: str, params: dict[str, str] | None = None, *, form: bool = False
) -> dict[str, str]:
    if path not in OTP_ROUTES:
        raise ApiError("Endpoint is not in the OTP allowlist.")
    ts = str(int(time.time() * 1000))
    canonical = MARKETING_SECRET + MARKETING_NONCE + path + ts
    headers = {
        "Authorization": APP_BASIC,
        "tenant": "300006",
        "channelId": "1",
        "countryId": "1",
        "appversion": APP_VERSION,
        "User-Agent": "okhttp/4.9.0",
        "nonce": MARKETING_NONCE,
        "timestamp": ts,
        "url": path,
        "Content-Type": "application/json",
    }
    if params is not None:
        headers["keys"] = ",".join(params)
        canonical += "[" + ",".join(params.values()) + "]"
    if form:
        headers.update(
            {
                "TENANT-CODE": "300006",
                "TENANT-ID": "300006",
                "tenantCode": "300006",
                "tenantID": "300006",
                "Accept-Language": "en-GB",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
    headers["signature"] = hashlib.md5(
        canonical.encode(), usedforsecurity=False
    ).hexdigest()
    return headers


async def _post(
    session: aiohttp.ClientSession, path: str, *, sending: bool = False, **kwargs: Any
) -> dict[str, Any]:
    """Bound decompressed response bytes; hide secret-bearing URL exceptions."""
    if path not in OTP_ROUTES:
        raise ApiError("Endpoint is not in the OTP allowlist.")
    error = OtpDeliveryUnknown if sending else CaptchaError
    cap = MAX_DELIVERY_RESPONSE if sending else MAX_CAPTCHA_RESPONSE
    try:
        async with session.post(
            BFF + path,
            timeout=aiohttp.ClientTimeout(total=20, connect=10),
            allow_redirects=False,
            ssl=True,
            raise_for_status=False,
            **kwargs,
        ) as response:
            if response.status == 429:
                raise RateLimitError(
                    "Backend rate limit reached.",
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if response.status >= 500:
                raise error(
                    "Backend did not confirm code delivery."
                    if sending
                    else "Backend captcha is unavailable."
                )
            if not 200 <= response.status < 300:
                if sending:
                    raise OtpDeliveryError(
                        "Backend refused code delivery; use another supported sign-in method."
                    )
                raise CaptchaError("Backend refused the captcha request.")
            if response.content_length is not None and response.content_length > cap:
                raise error("Backend returned an unusable response.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(16_384):
                body.extend(chunk)
                if len(body) > cap:
                    raise error("Backend returned an unusable response.")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise error("Backend returned an unusable response.") from None
            if not isinstance(result, dict):
                raise error("Backend returned an unusable response.")
            if str(result.get("code")) == "429":
                raise RateLimitError(
                    "Backend rate limit reached.",
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if not sending and (
                result.get("ok") is False
                or result.get("success") is False
                or result.get("error")
                or (
                    result.get("code") is not None
                    and (
                        isinstance(result["code"], bool)
                        or str(result["code"]) not in ("0", "200", "000000")
                    )
                )
            ):
                raise CaptchaError("Backend refused the captcha request.")
            return result
    except aiohttp.ClientSSLError:
        if sending:
            raise OtpDeliveryError(
                "Secure code delivery was refused; use another supported sign-in method."
            ) from None
        raise CaptchaError("Secure backend captcha transport was refused.") from None
    except (aiohttp.ClientError, TimeoutError):
        raise error(
            "Backend did not confirm code delivery."
            if sending
            else "Unable to obtain a backend captcha."
        ) from None


async def request_otp(
    session: aiohttp.ClientSession,
    identifier: str,
    account_type: str,
    country_code: str,
) -> None:
    """One create/solve/check, then at most one explicit code-send request."""
    identifier = await asyncio.to_thread(
        _identifier, identifier, account_type, country_code
    )
    created = await _post(
        session,
        "/code/create",
        headers=marketing_headers("/code/create"),
        json={"captchaType": "blockPuzzle"},
    )
    data = created.get("data")
    rep = data.get("repData") if isinstance(data, dict) else None
    token, point, verification = await asyncio.to_thread(solve_challenge, rep)
    params = {"captchaType": "blockPuzzle", "pointJson": point, "token": token}
    checked = await _post(
        session,
        "/code/check",
        params=params,
        headers=marketing_headers("/code/check", params),
    )
    data = checked.get("data")
    if not isinstance(data, dict) or data.get("repCode") != "0000":
        raise CaptchaError("Backend captcha verification failed.")
    mobile = account_type == "phone"
    path = (
        "/marketing/v2/app/code/sendSmsCode"
        if mobile
        else "/marketing/v2/app/code/sendMailCode"
    )
    body = {"mobile" if mobile else "email": identifier}
    if mobile:
        body["areaCode"] = country_code
    body.update(module="APP-LOGIN", captchaVerification=verification)
    result = await _post(
        session,
        path,
        sending=True,
        headers=marketing_headers(path, form=True),
        data=body,
    )
    if (
        result.get("ok") is False
        or result.get("success") is False
        or result.get("error")
    ):
        raise OtpDeliveryError("Backend rejected code delivery.")
    if result.get("code") is not None and (
        isinstance(result["code"], bool)
        or str(result["code"]) not in ("0", "200", "000000")
    ):
        raise OtpDeliveryError("Backend rejected code delivery.")
    if (
        result.get("ok") is True
        or result.get("success") is True
        or result.get("key") == "operation.successful"
    ):
        return
    if isinstance(result.get("key"), str) and result["key"]:
        raise OtpDeliveryError("Backend rejected code delivery.")
    raise OtpDeliveryUnknown("Backend did not confirm code delivery.")
