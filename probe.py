#!/usr/bin/env python3
"""Read-only EU Omoda/Jaecoo probe. Never sends a vehicle command or PIN.

Protocol reference: chery-connect-ha/omoda9-ha, revision 7d80cd6a7215168f58d147cbd475c82e52cd3944.
See THIRD_PARTY_NOTICES.md. No request occurs at import time.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BFF = "https://legend-oj.omodaauto.nl/api"
TSP = "https://tspconsole-eu.cheryinternational.com"
TOKEN_PATH = "/auth/oauth2/token"
TSP_LOGIN_PATH = "/tsp/v1/app/auth/login"
VEHICLES_PATH = "/tsp/v1/app/vmc/queryList"
REALTIME_PATH = "/asr/manager/realtime"
# Public application protocol constants, NOT account credentials or client certificates.
APP_BASIC = "Basic bGVnZW5kQXBwOmxlZ2VuZEFwcA=="
APP_VERSION = "1.1.9"
SIGN_NONCE = "chery_legend_h5"
SIGN_SECRET = "cX5fR8lJ6pK2xD4uH1eK4pY6wA4xO0sK"
AES_KEY = b"w9R8Ag1KiL0pvMHc"
TSP_SECRET = "EBUJPYr7oDd48C9Te9c755942Y7T48dV293Y4Z931J098X41aYf0"

# Strict route allowlist: no arbitrary endpoints, wake, locate, PIN or control paths.
ALLOWED_ROUTES = {
    "token": (BFF, TOKEN_PATH),
    "vehicles": (BFF, VEHICLES_PATH),
    "tsp_login": (BFF, TSP_LOGIN_PATH),
    "realtime": (TSP, REALTIME_PATH),
}
TELEMETRY_FIELDS = (
    "dumpEnergy", "soc", "electricRange", "pureElectricRange",
    "dynamicPureElectricRange", "pureElectricRangeMile", "rangeUnit",
    "chargeState", "chargeGunState", "chargingPower", "hVoltageState",
    "onlineStatus", "odometer", "vehicleSpeed", "inCarTemperature",
    "outCarTemperature", "doorLock", "frontHVACState", "temperature",
    "timestamp", "updateTime", "collectTime", "reportTime",
)
VEHICLE_LIST_KEYS = ("controlCarList", "authorizedControlCarList", "carList", "list", "vehicles")


class ProbeError(Exception):
    """A safe, body-free error that may be displayed to the user."""


def encode_password(password: str) -> str:
    """Vendor wire encoding, not a replacement for verified HTTPS."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_KEY)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def bff_headers(path: str, country_code: str, access_token: str | None = None,
                timestamp: int | None = None) -> dict[str, str]:
    ts = int(time.time() * 1000) if timestamp is None else timestamp
    signature = hashlib.sha256(f"{SIGN_SECRET}{SIGN_NONCE}{path}{ts}".encode()).hexdigest()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "en-GB", "Accept-Encoding": "gzip, deflate",
        "agent": "android", "version": APP_VERSION, "appversion": APP_VERSION,
        "Authorization": APP_BASIC, "DEPT-ID": country_code,
        "TENANT-ID": "300006", "TENANT-CODE": "300006", "CLIENT-TOC": "Y",
        "tenantCode": "300006", "tenantID": "300006", "channelId": "1", "countryId": "1",
        "User-Agent": "okhttp/4.9.0", "nonce": SIGN_NONCE,
        "timestamp": str(ts), "url": path, "signature": signature,
    }
    if access_token:
        headers.update(Authorization=f"Bearer {access_token}",
                       **{"Content-Type": "application/json; charset=UTF-8"})
    return headers


def realtime_body(vin: str, timestamp: int) -> dict[str, str]:
    """Flat realtime request only; intentionally NOT a generic command signer."""
    body = {"vin": vin, "appId": "eu-1"}
    canonical = "".join(f"{key}={body[key]}&" for key in sorted(body))
    canonical += f"secretKey={TSP_SECRET[::2]}&timestamp={timestamp}"
    body["sign"] = base64.b64encode(hashlib.sha256(canonical.encode()).digest()).decode().upper()
    return body


def payload(response: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "body"):
        value = response.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def extract_vehicles(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept only known discovery envelopes; deduplicate VINs across owner/delegate lists."""
    data = response.get("data")
    candidates: list[Any] = []
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        if data.get("vin") or data.get("VIN"):
            candidates.append(data)
        for key in VEHICLE_LIST_KEYS:
            if isinstance(data.get(key), list):
                candidates.extend(data[key])
    vehicles = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        vin = item.get("vin") or item.get("VIN")
        if isinstance(vin, str) and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin.upper()):
            vehicles.setdefault(vin.upper(), {**item, "vin": vin.upper()})
    return list(vehicles.values())


def field_schema(value: Any, depth: int = 0) -> Any:
    """Types only, never field values; bounded depth/list sampling for large replies."""
    if depth >= 8:
        return "..."
    if isinstance(value, dict):
        return {str(k): field_schema(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return {"type": "array", "length": len(value),
                "sample_types": [field_schema(v, depth + 1) for v in value[:3]]}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (float, int)):
        return "number"
    return "string"


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Best effort for PRIVATE raw captures; not a guarantee they are safe to publish."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(word in normalized for word in ("token", "password", "secret", "authorization", "pin", "cookie")):
                result[key] = "<redacted>"
            else:
                result[key] = redact(item, secrets)
        return result
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "<redacted>")
    return value


def save_private(path: Path, report: dict[str, Any]) -> None:
    """Exclusive, owner-only creation: no overwrite or symlink following."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=True, allow_nan=False)
        handle.write("\n")


class Client:
    def __init__(self, country_code: str, session: requests.Session | None = None):
        self.country_code = country_code
        self.session = session or requests.Session()
        # Ignore proxy/.netrc environment configuration to avoid credential forwarding.
        self.session.trust_env = False
        self.access_token: str | None = None
        self.user_token: str | None = None
        self.vehicles: list[dict[str, Any]] = []

    def close(self) -> None:
        self.access_token = self.user_token = None
        self.session.close()

    def _post(self, route: str, headers: dict[str, str], **kwargs: Any) -> dict[str, Any]:
        if route not in ALLOWED_ROUTES:
            raise ProbeError("Blocked: endpoint is not in the read-only allowlist.")
        host, path = ALLOWED_ROUTES[route]
        try:
            response = self.session.post(host + path, headers=headers, timeout=(10, 30),
                                         allow_redirects=False, verify=True, **kwargs)
        except requests.RequestException as exc:
            # Never expose exception text, request bodies, response bodies or credentials.
            raise ProbeError(f"{route}: network/TLS failure ({type(exc).__name__}); not retried.") from None
        if not 200 <= response.status_code < 300:
            raise ProbeError(f"{route}: HTTP {response.status_code}; not retried. "
                             "Authentication, regional routing or server policy may need checking.")
        try:
            result = response.json()
        except ValueError:
            raise ProbeError(f"{route}: server returned non-JSON; response body withheld.") from None
        if not isinstance(result, dict):
            raise ProbeError(f"{route}: unexpected JSON shape; response body withheld.")
        return result

    def login(self, email: str, password: str) -> None:
        result = self._post("token", bff_headers(TOKEN_PATH, self.country_code), data={
            "username": email, "password": encode_password(password),
            "grant_type": "password", "scope": "server", "needDecode": "1", "loginType": "email",
        })
        token = result.get("access_token") or payload(result).get("access_token")
        if not isinstance(token, str) or not token:
            raise ProbeError("Login did not return an access token. Check your credentials/region; "
                             "a challenge or different app backend may be required. Not retried.")
        self.access_token = token
        # Refresh tokens/password are intentionally not retained by this one-shot probe.

    def discover(self) -> dict[str, Any]:
        if not self.access_token:
            raise ProbeError("Login is required before discovery.")
        result = self._post("vehicles", bff_headers(VEHICLES_PATH, self.country_code, self.access_token), json={})
        self.vehicles = extract_vehicles(result)
        return result

    def realtime(self, vehicle: dict[str, Any]) -> dict[str, Any]:
        vin = vehicle.get("vin")
        if not self.access_token or not any(v["vin"] == vin for v in self.vehicles):
            raise ProbeError("Telemetry is restricted to vehicles discovered for this account.")
        login = self._post("tsp_login", bff_headers(TSP_LOGIN_PATH, self.country_code, self.access_token),
                           json={"channelId": "1"})
        token = payload(login).get("userToken")
        if not isinstance(token, str) or not token:
            raise ProbeError("Account login succeeded but vehicle-service login returned no token. "
                             "Account binding/region may need checking; not retried.")
        self.user_token = token
        ts = int(time.time() * 1000)
        return self._post("realtime", {
            "Authorization": token, "timestamp": str(ts), "x-TenantId": "",
            "Content-Type": "application/json; charset=UTF-8", "Accept": "application/json",
            "User-Agent": "okhttp/4.9.0", "version": APP_VERSION, "agent": "android",
        }, json=realtime_body(vin, ts))


def response_status(response: dict[str, Any]) -> dict[str, Any]:
    """Only short machine status identifiers; never free-form server messages."""
    status = {}
    for key in ("code", "key", "error", "ok", "success"):
        value = response.get(key)
        if isinstance(value, (bool, int)) or (
            isinstance(value, str) and len(value) <= 64
            and re.fullmatch(r"[A-Za-z0-9_.-]+", value)
        ):
            status[key] = value
    return status


def safe_observation(key: str, value: Any) -> bool:
    """Reject free-form strings in telemetry rather than risk echoing personal data."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if not isinstance(value, str) or len(value) > 40:
        return False
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return math.isfinite(float(value))
    if key in ("timestamp", "updateTime", "collectTime", "reportTime"):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            pass
    return False


def make_report(discovery: dict[str, Any], vehicles: list[dict[str, Any]],
                realtime: dict[str, Any] | None) -> dict[str, Any]:
    data = payload(realtime or {})
    # Only known telemetry fields with numeric/date values. Unknown values remain types only.
    vins = {v.get("vin") for v in vehicles if isinstance(v.get("vin"), str)}
    observed = {k: data[k] for k in TELEMETRY_FIELDS if k in data and
                safe_observation(k, data[k]) and str(data[k]) not in vins}
    return {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "backend": "omoda-jaecoo-eu", "vehicle_count": len(vehicles),
        "warning": "Cloud snapshot only. Fetch time is NOT vehicle observation time. "
                   "Units, sentinels and freshness are not validated; no wake/command was sent.",
        "discovery_status": response_status(discovery),
        "discovery_schema": field_schema(discovery),
        "telemetry_requested": realtime is not None,
        "realtime_status": response_status(realtime or {}),
        "telemetry_payload_present": bool(data),
        "observed_fields": observed,
        "realtime_schema": field_schema(realtime) if realtime is not None else None,
    }


def country_code(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        raise argparse.ArgumentTypeError("Use the account country dialling code, e.g. 44 (no +).")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--email", help="Account email; prompts if omitted. Password always uses a hidden prompt.")
    result.add_argument("--country-code", type=country_code, default="44", help="Account country dialling code (default: 44, UK); EU backend only.")
    result.add_argument("--telemetry", action="store_true", help="Also read one selected vehicle's cloud snapshot; no wake or commands.")
    result.add_argument("--output", type=Path, help="Save a reduced report; use captures/report.json (gitignored). Refuses overwrite.")
    result.add_argument("--raw-output", type=Path, help="PRIVATE discovery/telemetry capture, never auth replies. May contain VIN/GPS/PII. Use captures/raw.json.")
    result.add_argument("--plan", action="store_true", help="Print request plan without prompting for credentials or contacting the cloud.")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.plan:
        names = ["token", "vehicles"] + (["tsp_login", "realtime"] if args.telemetry else [])
        print("No network requests made. Planned POSTs:")
        for name in names:
            print("  " + "".join(ALLOWED_ROUTES[name]))
        print("No PIN, MQTT, wake, locate, lock, unlock or climate calls.")
        return 0
    if not sys.stdin.isatty():
        print("Use an interactive terminal for hidden password entry; no stdin piping.", file=sys.stderr)
        return 2
    for path in (args.output, args.raw_output):
        if path and path.exists():
            print("An output file already exists. Choose new paths; nothing was sent.", file=sys.stderr)
            return 2
    if args.output and args.raw_output and args.output.absolute() == args.raw_output.absolute():
        print("Use different report and raw capture paths; nothing was sent.", file=sys.stderr)
        return 2
    client = Client(args.country_code)
    try:
        print("EU OMODA/JAECOO account probe — no vehicle commands or PIN checks.")
        print("Signing in may invalidate your official app session. Requests are not retried.")
        if args.raw_output:
            print("WARNING: raw capture can include VIN, GPS and personal data. Keep it PRIVATE.")
        if input("Continue with a real account login? [y/N] ").strip().lower() != "y":
            return 0
        email = (args.email or input("Account email: ")).strip()
        if not email or "@" not in email:
            raise ProbeError("An account email address is required (phone login is not implemented).")
        password = getpass.getpass("Account password (hidden): ")
        if not password:
            raise ProbeError("Password must not be empty.")
        # Password is never printed, written, placed in a URL or accepted via command-line flags.
        client.login(email, password)
        del password
        print("Account login succeeded. Discovering vehicles…")
        discovery = client.discover()
        vehicles = client.vehicles
        print(f"Discovered {len(vehicles)} vehicle(s).")
        realtime = None
        if args.telemetry and vehicles:
            for index, vehicle in enumerate(vehicles, 1):
                # Only masked VIN, no free-form nickname/model printed to the terminal.
                print(f"  {index}: vehicle …{vehicle['vin'][-4:]}")
            selected = 1 if len(vehicles) == 1 else int(input("Vehicle number to read: "))
            if not 1 <= selected <= len(vehicles):
                raise ProbeError("Invalid vehicle selection; no telemetry requested.")
            print("Reading existing cloud telemetry (no wake)…")
            realtime = client.realtime(vehicles[selected - 1])
        report = make_report(discovery, vehicles, realtime)
        secrets = (email, client.access_token or "", client.user_token or "")
        report = redact(report, secrets)
        print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))
        if args.output:
            save_private(args.output, report)
            print("Reduced report saved. Review it before sharing.")
        if args.raw_output:
            save_private(args.raw_output, redact({"discovery": discovery, "realtime": realtime}, secrets))
            print("PRIVATE raw capture saved. Do not publish it.")
        if not vehicles:
            print("No recognized vehicle records. This may be an empty account, API rejection, "
                  "permissions or an unknown response shape; it is NOT proof you have no car.", file=sys.stderr)
            return 3
        if args.telemetry and not payload(realtime or {}):
            print("No telemetry payload returned. The car may be asleep or the request rejected; "
                  "no wake/retry was attempted.", file=sys.stderr)
            return 3
        return 0
    except (ProbeError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ProbeError) else f"Local input/output error ({type(exc).__name__})."
        print(message, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. No automatic retry.", file=sys.stderr)
        return 130
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
