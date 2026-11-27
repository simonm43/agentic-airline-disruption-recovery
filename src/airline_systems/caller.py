"""Caller identity from Container Apps authentication (Easy Auth).

Easy Auth validates the Entra token, strips any client-sent X-MS-CLIENT-* headers and injects
its own. The service additionally admits only listed object ids. The airline systems also require
the Entra app role the route needs (caller_with_role; defence in depth: both must pass), so a
slip in Easy Auth or in Entra alone does not open a system. Roles: tools/systems/app_roles.py.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_OID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")
_ROLE_CLAIMS = ("roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

WRITE_ROLE = "Actions.Write"


def read_role(system: str) -> str:
    return f"{system.capitalize()}.Read"


class CallerRefused(Exception):
    """The request has no validated caller, or the caller is not allowed."""


@dataclass(frozen=True)
class Caller:
    oid: str
    name: str | None
    roles: frozenset[str] = frozenset()


def caller_from_headers(headers: Mapping[str, str], allowed_oids: frozenset[str]) -> Caller:
    lowered = {key.lower(): value for key, value in headers.items()}
    encoded = lowered.get("x-ms-client-principal")
    if not encoded:
        raise CallerRefused("no validated caller")
    try:
        principal = json.loads(base64.b64decode(encoded))
        entries = principal.get("claims", [])
        claims = {c["typ"]: c["val"] for c in entries}
        # Easy Auth passes each app role as its own claim entry.
        roles = frozenset(str(c["val"]) for c in entries if c["typ"] in _ROLE_CLAIMS)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise CallerRefused("unreadable caller principal") from error
    oid = str(next((claims[name] for name in _OID_CLAIMS if name in claims), "")).lower()
    if not _GUID.match(oid):
        raise CallerRefused("caller has no object id")
    if oid not in allowed_oids:
        raise CallerRefused(f"caller {oid} is not allowed")
    name = lowered.get("x-ms-client-principal-name") or claims.get("appid") or claims.get("azp")
    return Caller(oid=oid, name=str(name) if name else None, roles=roles)


def caller_with_role(headers: Mapping[str, str], allowed_oids: frozenset[str], role: str) -> Caller:
    """A listed caller whose token also carries the app role (the airline systems' check)."""
    caller = caller_from_headers(headers, allowed_oids)
    if role not in caller.roles:
        raise CallerRefused(f"caller {caller.oid} lacks the app role {role}")
    return caller


def parse_allowed(value: str) -> frozenset[str]:
    oids = frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    if not oids or not all(_GUID.match(oid) for oid in oids):
        raise ValueError("ALLOWED_CALLER_OIDS must be a comma-separated list of object ids")
    return oids


def trace_id(headers: Mapping[str, str]) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    parts = lowered.get("traceparent", "").split("-")
    if len(parts) == 4 and re.fullmatch(r"[0-9a-f]{32}", parts[1]):
        return parts[1]
    return None
