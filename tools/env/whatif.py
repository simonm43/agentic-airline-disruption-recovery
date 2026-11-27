"""Canonical what-if digests and the apply gate for an environment's deployments (A2).

A deployment may be applied only when a fresh what-if repeats the previewed change set
exactly, deletes nothing, stays inside the environment's subscription (Dev unless
``--subscription`` names another), and reports no unacknowledged ``Unsupported`` change.
Volatile ARM fields are removed before hashing so two unchanged previews compare equal.
``same`` tells whether two what-ifs hold the same change set, for example before and after a
template refactoring that must not change what an environment gets.

"Deletes nothing" includes deletions nested inside a ``Modify``: an incremental what-if
shows a removed subnet, NSG rule or IP rule as a ``propertyChangeType: Delete`` child of an
``Array`` delta. Every such array-element delete is refused. A property delete is refused
unless it is pinned in ``KNOWN_READ_ONLY_DELETES`` (read-only or provider-defaulted
properties that the templates do not set).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEV_SUBSCRIPTION_ID = "00000000-0000-4000-8000-000000000013"
VOLATILE_KEYS = frozenset({"etag", "systemData"})
ACK_FILE = Path(__file__).with_name("unsupported-ack.json")
_SUBSCRIPTION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SUMMARY_ORDER = ("Create", "Modify", "Delete", "Deploy", "NoChange", "Ignore", "Unsupported")

_PEERING_READ_ONLY = (
    "properties.doNotVerifyRemoteGateways",
    "properties.peerCompleteVnets",
    "properties.peeringSyncLevel",
    "properties.remoteAddressSpace",
    "properties.remoteVirtualNetworkAddressSpace",
)
# Property deletes seen in the Dev previews, keyed by lower-cased resource type.
# Each is a read-only or provider-defaulted property that the templates do not set.
# Patterns write array indices as ``[*]``. Array-element deletes are never noise.
KNOWN_READ_ONLY_DELETES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "microsoft.cognitiveservices/accounts": frozenset(
            {"properties.associatedProjects", "properties.defaultProject"}
        ),
        "microsoft.cognitiveservices/accounts/deployments": frozenset(
            {"properties.currentCapacity"}
        ),
        # agentIdentity: the project's Entra agent identity blueprint, created by Foundry.
        "microsoft.cognitiveservices/accounts/projects": frozenset(
            {
                "kind",
                "properties.agentIdentity",
                "properties.endpoints",
                "properties.internalId",
                "properties.isDefault",
            }
        ),
        # The App Insights tracing connection: group is read-only in the ARM schema,
        # isDefault is set by the provider (the project's one AppInsights connection).
        "microsoft.cognitiveservices/accounts/projects/connections": frozenset(
            {"properties.group", "properties.isDefault"}
        ),
        "microsoft.consumption/budgets": frozenset({"properties.timePeriod.endDate"}),
        # Premium-only setting; Standard reports its fixed 256 KB (ADR 0025).
        "microsoft.servicebus/namespaces/queues": frozenset(
            {"properties.maxMessageSizeInKilobytes"}
        ),
        "microsoft.servicebus/namespaces/topics": frozenset(
            {"properties.maxMessageSizeInKilobytes"}
        ),
        "microsoft.containerregistry/registries": frozenset(
            {"properties.dataEndpointEnabled", "properties.encryption"}
        ),
        "microsoft.managedidentity/userassignedidentities": frozenset({"properties"}),
        "microsoft.network/privatednszones/virtualnetworklinks": frozenset(
            {"properties.resolutionPolicy"}
        ),
        "microsoft.network/privateendpoints": frozenset(
            {"properties.billingSku", "properties.ipVersionType"}
        ),
        "microsoft.network/privateendpoints/privatednszonegroups": frozenset(
            {
                "properties.privateDnsZoneConfigs[*].id",
                "properties.privateDnsZoneConfigs[*].properties.provisioningState",
                "properties.privateDnsZoneConfigs[*].type",
            }
        ),
        "microsoft.network/virtualnetworks": frozenset(
            {
                "properties.privateEndpointVNetPolicies",
                *(f"properties.virtualNetworkPeerings[*].{path}" for path in _PEERING_READ_ONLY),
            }
        ),
        "microsoft.network/virtualnetworks/virtualnetworkpeerings": frozenset(_PEERING_READ_ONLY),
    }
)


@dataclass(frozen=True, slots=True)
class NestedDelete:
    """A ``propertyChangeType: Delete`` inside a resource's ``delta``.

    ``path`` shows array indices, ``pattern`` replaces them with ``[*]``; ``element`` marks the
    removal of an array element, and ``identity`` is its ``name``, ``id`` or ``value``.
    """

    path: str
    pattern: str
    element: bool
    identity: str | None


@dataclass(frozen=True, slots=True)
class Change:
    resource_id: str | None
    change_type: str
    record_sha256: str
    nested_deletes: tuple[NestedDelete, ...] = ()


@dataclass(frozen=True, slots=True)
class UnsupportedAck:
    resource_type: str
    owner: str
    acknowledged_on: date
    reason: str


_NO_ACKS: Mapping[str, UnsupportedAck] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class _Entry:
    label: str
    change: Change


def _first_segment(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0]


def _is_volatile_delta(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    path = item.get("path")
    return (
        isinstance(path, str)
        and "propertyChangeType" in item
        and _first_segment(path) in VOLATILE_KEYS
    )


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip(item) for item in value if not _is_volatile_delta(item)]
    return value


def _digest(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _strip(dict(record)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a what-if result object")
    return payload


def _body(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if "changes" in payload or "potentialChanges" in payload:
        return payload
    properties = payload.get("properties")
    return properties if isinstance(properties, dict) else payload


def _records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    body = _body(payload)
    records: list[dict[str, Any]] = []
    for key in ("changes", "potentialChanges"):
        items = body.get(key) or []
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError(f"what-if {key} is not a list of change records")
        records.extend(items)
    return records


def _element_identity(before: object) -> str | None:
    if isinstance(before, dict):
        for key in ("name", "id", "value"):
            value = before.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _walk_deletes(
    items: object, path: str, pattern: str, in_array: bool, found: list[NestedDelete]
) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        segment = item.get("path")
        if not isinstance(segment, str):
            continue
        if in_array:
            here, shape = f"{path}[{segment}]", f"{pattern}[*]"
        elif path:
            here, shape = f"{path}.{segment}", f"{pattern}.{segment}"
        else:
            here, shape = segment, segment
        kind = item.get("propertyChangeType")
        if kind == "Delete":
            identity = _element_identity(item.get("before")) if in_array else None
            found.append(NestedDelete(here, shape, in_array, identity))
        _walk_deletes(item.get("children"), here, shape, kind == "Array", found)


def _nested_deletes(record: Mapping[str, Any]) -> tuple[NestedDelete, ...]:
    found: list[NestedDelete] = []
    _walk_deletes(_strip(dict(record)).get("delta"), "", "", False, found)
    return tuple(found)


def _change(record: Mapping[str, Any]) -> Change:
    resource_id = record.get("resourceId")
    change_type = record.get("changeType")
    return Change(
        resource_id=resource_id if isinstance(resource_id, str) and resource_id else None,
        change_type=change_type if isinstance(change_type, str) else "",
        record_sha256=_digest(record),
        nested_deletes=_nested_deletes(record),
    )


def _label(record: Mapping[str, Any]) -> str:
    for key in ("resourceId", "symbolicName"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return "<unidentified>"


def _entries(payload: Mapping[str, Any]) -> list[_Entry]:
    return [_Entry(_label(record), _change(record)) for record in _records(payload)]


def load_changes(path: Path) -> list[Change]:
    """Every record in ``changes`` and ``potentialChanges``, top level or under ``properties``."""
    return [_change(record) for record in _records(_read(path))]


def load_acks(path: Path = ACK_FILE) -> dict[str, UnsupportedAck]:
    """Owner acknowledgements of ``Unsupported`` resource types, keyed by lower-cased type."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must hold a JSON list of acknowledgements")
    acks: dict[str, UnsupportedAck] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path} has an acknowledgement that is not an object")
        texts = [item.get(name) for name in ("resource_type", "owner", "reason")]
        on = item.get("acknowledged_on")
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise ValueError(f"{path} has an incomplete acknowledgement")
        if not isinstance(on, str):
            raise ValueError(f"{path} has an acknowledgement without a date")
        ack = UnsupportedAck(
            resource_type=str(texts[0]),
            owner=str(texts[1]),
            acknowledged_on=date.fromisoformat(on),
            reason=str(texts[2]),
        )
        acks[ack.resource_type.casefold()] = ack
    return acks


def resource_type(resource_id: str) -> str:
    """The full resource type after the last ``/providers/``, for example ``A.B/c/d``."""
    marker = resource_id.casefold().rfind("/providers/")
    if marker < 0:
        return ""
    segments = [part for part in resource_id[marker + len("/providers/") :].split("/") if part]
    if not segments:
        return ""
    return "/".join([segments[0], *segments[1::2]])


def is_read_only_noise(resource_id: str | None, delete: NestedDelete) -> bool:
    """Whether a nested property delete is pinned noise; array-element deletes never are."""
    if delete.element or not resource_id:
        return False
    pinned = KNOWN_READ_ONLY_DELETES.get(resource_type(resource_id).casefold(), frozenset())
    return delete.pattern in pinned


def _nested_refusal(label: str, delete: NestedDelete) -> str:
    if delete.element:
        suffix = f" {delete.identity}" if delete.identity else ""
        return f"delete:{label} at {delete.path} element{suffix}"
    return f"delete:{label} at {delete.path} property"


def summary(changes: Sequence[Change]) -> str:
    counts = Counter(change.change_type for change in changes)
    kinds = [kind for kind in _SUMMARY_ORDER if counts[kind]]
    kinds += sorted(kind for kind in counts if kind not in _SUMMARY_ORDER)
    lines = [f"{kind}: {counts[kind]}" for kind in kinds]
    nested = [(change, delete) for change in changes for delete in change.nested_deletes]
    if nested:
        refused = sum(
            not is_read_only_noise(change.resource_id, delete) for change, delete in nested
        )
        lines.append(f"Nested Delete: {len(nested)} (refused {refused})")
    for change in changes:
        identity = change.resource_id or "<no resourceId>"
        if change.change_type in {"Delete", "Unsupported"}:
            lines.append(f"REFUSED {change.change_type}: {identity}")
        elif change.change_type not in {"NoChange", "Ignore"}:
            lines.append(f"  {change.change_type} {identity}")
        for delete in change.nested_deletes:
            if delete.element:
                name = f" ({delete.identity})" if delete.identity else ""
                lines.append(f"REFUSED Delete element: {identity} at {delete.path}{name}")
            elif is_read_only_noise(change.resource_id, delete):
                lines.append(f"  read-only Delete: {identity} at {delete.path}")
            else:
                lines.append(f"REFUSED Delete property: {identity} at {delete.path}")
    return "\n".join(lines)


def _status_refusals(path: Path, payload: Mapping[str, Any]) -> list[str]:
    status = payload.get("status")
    error = payload.get("error")
    properties = payload.get("properties")
    if status is None and isinstance(properties, dict):
        status = properties.get("status")
        error = error or properties.get("error")
    refusals = []
    if status != "Succeeded":
        refusals.append(f"status:{path}:{status}")
    if error:
        refusals.append(f"status:{path}:error")
    return refusals


def _dedupe(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def apply_gate(
    previous: Path,
    current: Path,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=60),
    acks: Mapping[str, UnsupportedAck] = _NO_ACKS,
    subscription: str = DEV_SUBSCRIPTION_ID,
) -> list[str]:
    """Refusals that block applying ``current``; an empty list means go."""
    if not _SUBSCRIPTION.fullmatch(subscription):
        raise ValueError(f"not a subscription id: {subscription}")
    inside = f"/subscriptions/{subscription}/".casefold()
    refusals: list[str] = []
    payloads: dict[Path, dict[str, Any]] = {}
    for path in (previous, current):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            payloads[path] = _read(path)
            _records(payloads[path])
        except (OSError, ValueError) as error:
            refusals.append(f"status:{path}:unreadable:{type(error).__name__}")
            continue
        if now - modified > max_age:
            refusals.append(f"stale:{path}")
        refusals.extend(_status_refusals(path, payloads[path]))
    if any(refusal.startswith("status:") for refusal in refusals):
        return _dedupe(refusals)

    before = _entries(payloads[previous])
    after = _entries(payloads[current])
    differing = _differing(before, after)
    for entry in [*after, *before]:
        if differing[(entry.label, entry.change.record_sha256)]:
            refusals.append(f"changed:{entry.label}")

    for entry in after:
        kind = entry.change.change_type.casefold()
        if kind == "delete":
            refusals.append(f"delete:{entry.label}")
        elif kind == "unsupported":
            identity = entry.change.resource_id or ""
            if resource_type(identity).casefold() not in acks:
                refusals.append(f"unsupported:{entry.label}")
        for delete in entry.change.nested_deletes:
            if not is_read_only_noise(entry.change.resource_id, delete):
                refusals.append(_nested_refusal(entry.label, delete))
        resource_id = entry.change.resource_id
        if resource_id is None or not resource_id.casefold().startswith(inside):
            refusals.append(f"subscription:{entry.label}")
    return _dedupe(refusals)


def _differing(before: Sequence[_Entry], after: Sequence[_Entry]) -> Counter[tuple[str, str]]:
    """Change records in one what-if and not the other. ``Ignore`` records are left out: they
    are resources in the group that the template does not declare, which an Incremental
    deployment leaves untouched, and their live payload changes with every scale event."""
    first = Counter(
        (e.label, e.change.record_sha256) for e in before if e.change.change_type != "Ignore"
    )
    second = Counter(
        (e.label, e.change.record_sha256) for e in after if e.change.change_type != "Ignore"
    )
    return (first - second) + (second - first)


def differences(before: Path, after: Path) -> list[str]:
    """Labels whose change records differ between two what-ifs; empty when they hold the same
    change set (volatile fields and resources the template ignores left out)."""
    differing = _differing(_entries(_read(before)), _entries(_read(after)))
    return sorted({label for label, _ in differing})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.env.whatif",
        description="Summarise a what-if result or gate an apply on two of them.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    summarise = commands.add_parser("summary", help="Count and list the changes in one file.")
    summarise.add_argument("file", type=Path)
    gate = commands.add_parser("gate", help="Refuse unless a fresh what-if repeats the preview.")
    gate.add_argument("previous", type=Path)
    gate.add_argument("current", type=Path)
    gate.add_argument("--max-age-minutes", type=int, default=60)
    gate.add_argument(
        "--subscription",
        default=DEV_SUBSCRIPTION_ID,
        help="The environment's subscription; any change outside it is refused (default Dev).",
    )
    same = commands.add_parser("same", help="Exit 0 when two what-ifs hold the same change set.")
    same.add_argument("before", type=Path)
    same.add_argument("after", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    if arguments.command == "summary":
        try:
            print(summary(load_changes(arguments.file)))
        except (OSError, ValueError) as error:
            print(f"whatif: cannot read {arguments.file}: {error}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "same":
        try:
            changed = differences(arguments.before, arguments.after)
        except (OSError, ValueError) as error:
            print(f"whatif: cannot compare: {error}", file=sys.stderr)
            return 1
        print(json.dumps(changed))
        return 3 if changed else 0
    try:
        refusals = apply_gate(
            arguments.previous,
            arguments.current,
            now=datetime.now(UTC),
            max_age=timedelta(minutes=arguments.max_age_minutes),
            acks=load_acks(),
            subscription=arguments.subscription,
        )
    except ValueError as error:
        print(f"whatif: {error}", file=sys.stderr)
        return 2
    print(json.dumps(refusals))
    for refusal in refusals:
        print(refusal, file=sys.stderr)
    return 3 if refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
