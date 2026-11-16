"""Read-only rear-defrost authority inspection, with no auth or actuator path."""

from __future__ import annotations

import time
from typing import Any

from .api import ControlSession, TokenSet
from .commands import CommandClient, CommandError, _permission_integer, _successful

PERMISSION_IDS = (204, 2041, 20411, 232, 2321)


def _upstream_integer(value: Any) -> int | None:
    """Compare upstream permessi._num, never emit its unbounded result."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _kind(value: Any) -> str:
    for cls, name in ((bool, "boolean"), (int, "integer"), (float, "float"),
                      (str, "string"), (list, "list"), (dict, "object")):
        if isinstance(value, cls):
            return name
    return "null" if value is None else "other"


def _state(value: int | None, present: bool) -> str:
    if not present:
        return "missing"
    if value is None or not 0 <= value <= 9:
        return "unknown"
    return "denied" if value == 0 else "allowed"


def permission_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    """Emit only fixed labels, five IDs and permission-domain integers 0..9.

    Local parsing uses the command gate's strict integer parser and conflicting
    duplicate rule. Upstream uses int(str(x).strip()), skips malformed states and
    keeps the last valid duplicate. Neither policy here authorizes a command.
    """
    data = result.get("data")
    rows = data.get("permissionList") if isinstance(data, dict) else None
    valid = _successful(result) and isinstance(rows, list)
    rows = rows if valid and isinstance(rows, list) else []
    report = {}
    for identifier in PERMISSION_IDS:
        local: dict[int, int | None] = {}
        upstream: dict[int, int] = {}
        seen: list[dict[str, Any]] = []
        local_values = []
        upstream_values = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_id, raw_state = row.get("id"), row.get("state")
            lid, uid = _permission_integer(raw_id), _upstream_integer(raw_id)
            ls, us = _permission_integer(raw_state), _upstream_integer(raw_state)
            if lid == identifier:
                local[identifier] = ls if identifier not in local or local[identifier] == ls else None
                local_values.append(ls)
            if uid == identifier:
                upstream_values.append(us)
                if us is not None:
                    upstream[identifier] = us
            if lid == identifier or uid == identifier:
                # Types only, never raw strings, arbitrary numeric values or counts.
                seen.append({"id_type": _kind(raw_id), "state_type": _kind(raw_state)})
        lp, up = identifier in local, identifier in upstream
        lv, uv = local.get(identifier), upstream.get(identifier)
        report[str(identifier)] = {
            "present": bool(seen) if valid else None,
            "types": [dict(pair) for pair in sorted({tuple(sorted(item.items())) for item in seen})],
            "duplicate": len(seen) > 1,
            "duplicate_conflict": len(set(local_values)) > 1 or len(set(upstream_values)) > 1,
            "local": {"present": lp if valid else None,
                      "state": lv if lv is not None and 0 <= lv <= 9 else None,
                      "status": _state(lv, lp) if valid else "unknown"},
            "upstream": {"present": up if valid else None,
                         "state": uv if uv is not None and 0 <= uv <= 9 else None,
                         "status": _state(uv, up) if valid else "unknown"},
            "parser_mismatch": (lp != up or lv != uv) if valid else None,
        }
    return {"authority": "available" if valid else "unknown", "permissions": report}


def _cached_session(client: CommandClient) -> ControlSession:
    api = client._api
    tokens = api._tokens
    if (
        not isinstance(tokens, TokenSet)
        or not isinstance(tokens.access_token, str) or not tokens.access_token.strip()
        or (tokens.expires_at is not None and tokens.expires_at <= time.time())
        or api._tsp_account is not tokens
        or api._tsp_login_confirmed is not True
        or not isinstance(api._user_token, str) or not api._user_token.strip()
        or not isinstance(api._tuser_id, str) or not api._tuser_id.strip()
    ):
        raise CommandError("A current confirmed cached session is required; no login or refresh was attempted.")
    return ControlSession(tokens, api._user_token, api._tuser_id)


async def diagnose_rear_defrost_permissions(client: CommandClient, vin: str) -> dict[str, Any]:
    """Exactly one authority request. No login, refresh, PIN or vehicle command.

    Do not use async_control_session: it can authenticate or refresh. The only
    network path below is the existing no-retry BFF authority route.
    """
    client._api.get_vehicle(vin)
    credentials = _cached_session(client)
    result = await client._bff("authority", credentials, {
        "vin": vin, "tUserId": credentials.tuser_id, "channelId": "1",
    })
    current = _cached_session(client)
    if current.account is not credentials.account or current != credentials:
        raise CommandError("Cached session changed during permission diagnosis; result discarded.")
    return permission_snapshot(result)
