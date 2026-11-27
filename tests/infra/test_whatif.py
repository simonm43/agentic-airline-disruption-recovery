from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools.env import whatif

ROOT = Path(__file__).resolve().parents[2]
DEV = "00000000-0000-4000-8000-000000000013"
GROUP = f"/subscriptions/{DEV}/resourceGroups/rg-airrec-dev-swc"
ENVIRONMENT_ID = f"{GROUP}/providers/Microsoft.App/managedEnvironments/cae-airrec-dev-swc"
WORKSPACE_ID = f"{GROUP}/providers/Microsoft.OperationalInsights/workspaces/log-airrec-dev-swc"
SQL_ID = f"{GROUP}/providers/Microsoft.Sql/servers/sql-airrec-dev"
VNET_ID = f"{GROUP}/providers/Microsoft.Network/virtualNetworks/vnet-airrec-dev-swc"
PEERING_ID = f"{VNET_ID}/virtualNetworkPeerings/peer-to-hub"
NSG_ID = f"{GROUP}/providers/Microsoft.Network/networkSecurityGroups/nsg-airrec-aca-dev-swc"
STORAGE_ID = f"{GROUP}/providers/Microsoft.Storage/storageAccounts/stairrecdl"
ZONE_GROUP_ID = (
    f"{GROUP}/providers/Microsoft.Network/privateEndpoints/pe-sql/privateDnsZoneGroups/sql"
)
IDENTITY_ID = f"{GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-worker"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _record(
    resource_id: str | None,
    change_type: str = "Create",
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {"changeType": change_type, "before": None, "after": None}
    if resource_id is not None:
        record["resourceId"] = resource_id
    record.update(extra)
    return record


def _modify(resource_id: str, public_access: str = "Disabled") -> dict[str, Any]:
    before = {"id": resource_id, "properties": {"publicNetworkAccess": "Disabled"}}
    after = {"id": resource_id, "properties": {"publicNetworkAccess": public_access}}
    delta = [
        {
            "path": "properties.publicNetworkAccess",
            "propertyChangeType": "Modify",
            "before": "Disabled",
            "after": public_access,
            "children": None,
        }
    ]
    return _record(resource_id, "Modify", before=before, after=after, delta=delta)


def _with_delta(resource_id: str, *delta: dict[str, Any]) -> dict[str, Any]:
    body = {"id": resource_id}
    return _record(resource_id, "Modify", before=body, after=body, delta=list(delta))


def _node(path: str, kind: str, *children: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": path,
        "propertyChangeType": kind,
        "before": None,
        "after": None,
        "children": list(children) or None,
    }


def _deleted(path: str, before: object = None) -> dict[str, Any]:
    return {
        "path": path,
        "propertyChangeType": "Delete",
        "before": before,
        "after": None,
        "children": None,
    }


NSG_RULE_ID = f"{NSG_ID}/securityRules/deny-all-inbound"
SUBNET_DELETE = _with_delta(
    VNET_ID,
    _node(
        "properties.subnets",
        "Array",
        _deleted("2", {"name": "snet-aca-int", "properties": {"addressPrefix": "10.60.0.128/26"}}),
    ),
)
NSG_RULE_DELETE = _with_delta(
    NSG_ID,
    _node(
        "properties",
        "Modify",
        _node(
            "securityRules",
            "Array",
            _deleted("1", {"id": NSG_RULE_ID, "properties": {"access": "Deny"}}),
        ),
    ),
)
IP_RULE_DELETE = _with_delta(
    STORAGE_ID,
    _node(
        "properties.networkAcls.ipRules",
        "Array",
        _deleted("0", {"action": "Allow", "value": "20.91.223.203"}),
    ),
)
NOISE_ONLY = [
    _with_delta(
        VNET_ID,
        _deleted("properties.privateEndpointVNetPolicies", "Disabled"),
        _node("properties.subnets", "Array", _node("1", "Create")),
        _node(
            "properties.virtualNetworkPeerings",
            "Array",
            _node(
                "0",
                "Modify",
                _deleted("properties.peeringSyncLevel", "RemoteNotInSync"),
                _deleted("properties.remoteAddressSpace", {"addressPrefixes": ["10.99.0.0/23"]}),
            ),
        ),
    ),
    _with_delta(
        ZONE_GROUP_ID,
        _node(
            "properties.privateDnsZoneConfigs",
            "Array",
            _node(
                "0",
                "Modify",
                _deleted("etag", 'W/"1"'),
                _deleted("id", f"{ZONE_GROUP_ID}/privateDnsZoneConfigs/sql"),
                _deleted("properties.provisioningState", "Succeeded"),
                _deleted("type", "Microsoft.Network/privateEndpoints/privateDnsZoneGroups/x"),
            ),
        ),
    ),
    _with_delta(IDENTITY_ID, _deleted("properties", {"isolationScope": "None"})),
]


def _write(
    path: Path,
    changes: list[dict[str, Any]],
    *,
    potential: list[dict[str, Any]] | None = None,
    status: str = "Succeeded",
    error: object = None,
    age: timedelta = timedelta(minutes=1),
) -> Path:
    payload = {
        "status": status,
        "error": error,
        "changes": changes,
        "potentialChanges": potential or [],
        "diagnostics": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _pair(
    tmp_path: Path,
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]] | None = None,
    **current_options: Any,
) -> tuple[Path, Path]:
    first = _write(tmp_path / "first.whatif.json", previous)
    second = _write(
        tmp_path / "second.whatif.json",
        previous if current is None else current,
        **current_options,
    )
    return first, second


def test_load_changes_reads_full_records(tmp_path: Path) -> None:
    potential = _record(ENVIRONMENT_ID, "Create", after={"name": "cae-airrec-dev-swc"})
    first = _write(tmp_path / "a.json", [_modify(SQL_ID, "Enabled")], potential=[potential])
    changes = whatif.load_changes(first)
    assert [(change.resource_id, change.change_type) for change in changes] == [
        (SQL_ID, "Modify"),
        (ENVIRONMENT_ID, "Create"),
    ]
    assert all(len(change.record_sha256) == 64 for change in changes)

    other = _modify(SQL_ID, "Enabled")
    other["after"]["properties"]["x"] = 1
    second = _write(tmp_path / "b.json", [other], potential=[potential])
    assert whatif.load_changes(second)[0].record_sha256 != changes[0].record_sha256
    assert whatif.load_changes(second)[1].record_sha256 == changes[1].record_sha256

    nested = tmp_path / "nested.json"
    nested.write_text(
        json.dumps(
            {
                "status": "Succeeded",
                "properties": {"changes": [_modify(SQL_ID, "Enabled")], "potentialChanges": []},
            }
        ),
        encoding="utf-8",
    )
    assert whatif.load_changes(nested) == changes[:1]


def test_digest_ignores_volatile_fields(tmp_path: Path) -> None:
    plain = _modify(SQL_ID)
    noisy = _modify(SQL_ID)
    noisy["after"]["etag"] = 'W/"2"'
    noisy["before"]["properties"]["systemData"] = {"lastModifiedAt": "2026-11-05T11:00:00Z"}
    noisy["delta"].append(
        {"path": "etag", "propertyChangeType": "Modify", "before": "1", "after": "2"}
    )
    noisy["delta"].append(
        {"path": "systemData.lastModifiedAt", "propertyChangeType": "Modify", "after": "x"}
    )
    first = whatif.load_changes(_write(tmp_path / "a.json", [plain]))
    second = whatif.load_changes(_write(tmp_path / "b.json", [noisy]))
    assert first == second
    assert whatif.VOLATILE_KEYS == frozenset({"etag", "systemData"})


def test_summary_counts_and_flags_unsupported(tmp_path: Path) -> None:
    unsupported = f"{GROUP}/providers/Microsoft.Authorization/roleAssignments/abc"
    path = _write(
        tmp_path / "a.json",
        [
            _record(ENVIRONMENT_ID),
            _record(WORKSPACE_ID),
            _modify(SQL_ID),
            _record(unsupported, "Unsupported", unsupportedReason="cannot be calculated"),
        ],
    )
    text = whatif.summary(whatif.load_changes(path))
    assert "Create: 2" in text
    assert "Modify: 1" in text
    assert f"REFUSED Unsupported: {unsupported}" in text


def test_gate_accepts_an_identical_fresh_whatif(tmp_path: Path) -> None:
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID), _modify(SQL_ID)])
    assert whatif.apply_gate(first, second, now=NOW) == []


def test_gate_refuses_a_stale_preview(tmp_path: Path) -> None:
    changes = [_record(ENVIRONMENT_ID)]
    first = _write(tmp_path / "first.json", changes, age=timedelta(minutes=61))
    second = _write(tmp_path / "second.json", changes)
    refusals = whatif.apply_gate(first, second, now=NOW)
    assert len(refusals) == 1
    assert refusals[0].startswith("stale:")


def test_gate_refuses_a_changed_set(tmp_path: Path) -> None:
    extra = _modify(WORKSPACE_ID)
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID)], [_record(ENVIRONMENT_ID), extra])
    assert whatif.apply_gate(first, second, now=NOW) == [f"changed:{WORKSPACE_ID}"]


def test_gate_refuses_changed_property_with_same_id_and_type(tmp_path: Path) -> None:
    first, second = _pair(tmp_path, [_modify(SQL_ID, "Disabled")], [_modify(SQL_ID, "Enabled")])
    assert whatif.apply_gate(first, second, now=NOW) == [f"changed:{SQL_ID}"]


def test_gate_refuses_any_delete(tmp_path: Path) -> None:
    first, second = _pair(tmp_path, [_record(WORKSPACE_ID, "Delete")])
    assert whatif.apply_gate(first, second, now=NOW) == [f"delete:{WORKSPACE_ID}"]


ROLE_ASSIGNMENT_ID = (
    f"{STORAGE_ID}/providers/Microsoft.Authorization/roleAssignments/"
    "98f2b155-0dc2-5702-b33d-66d4f3fc588d"
)


def test_gate_refuses_a_role_assignment_delete_and_passes_an_ignored_one(
    tmp_path: Path,
) -> None:
    """D38's swap runbook relies on this. Under Incremental mode an assignment that leaves the
    template shows as Ignore (Learn: deploy-what-if, change types) or not at all, and ARM
    leaves it in place; a top-level Delete of one is refused like any other Delete. So the
    swap removes the account-scope Reader only by its own explicit command."""
    first, second = _pair(tmp_path, [_record(ROLE_ASSIGNMENT_ID, "Delete")])
    assert whatif.apply_gate(first, second, now=NOW) == [f"delete:{ROLE_ASSIGNMENT_ID}"]
    assert f"REFUSED Delete: {ROLE_ASSIGNMENT_ID}" in whatif.summary(whatif.load_changes(second))
    first, second = _pair(tmp_path, [_record(ROLE_ASSIGNMENT_ID, "Ignore")])
    assert whatif.apply_gate(first, second, now=NOW) == []
    text = whatif.summary(whatif.load_changes(second))
    assert "Ignore: 1" in text
    assert "REFUSED" not in text


def test_load_changes_exposes_nested_deletes(tmp_path: Path) -> None:
    changes = whatif.load_changes(_write(tmp_path / "a.json", [SUBNET_DELETE, *NOISE_ONLY]))
    assert changes[0].nested_deletes == (
        whatif.NestedDelete(
            path="properties.subnets[2]",
            pattern="properties.subnets[*]",
            element=True,
            identity="snet-aca-int",
        ),
    )
    zone_group = changes[2].nested_deletes
    assert [delete.pattern for delete in zone_group] == [
        "properties.privateDnsZoneConfigs[*].id",
        "properties.privateDnsZoneConfigs[*].properties.provisioningState",
        "properties.privateDnsZoneConfigs[*].type",
    ]
    assert not any(delete.element for delete in zone_group)


@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (SUBNET_DELETE, f"delete:{VNET_ID} at properties.subnets[2] element snet-aca-int"),
        (
            NSG_RULE_DELETE,
            f"delete:{NSG_ID} at properties.securityRules[1] element {NSG_RULE_ID}",
        ),
        (
            IP_RULE_DELETE,
            f"delete:{STORAGE_ID} at properties.networkAcls.ipRules[0] element 20.91.223.203",
        ),
    ],
    ids=["subnet", "nsg-rule", "ip-rule"],
)
def test_gate_refuses_an_array_element_delete_inside_a_modify(
    tmp_path: Path, record: dict[str, Any], refusal: str
) -> None:
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID), record])
    assert whatif.apply_gate(first, second, now=NOW) == [refusal]


def test_an_array_element_delete_is_never_read_only_noise(tmp_path: Path) -> None:
    record = _with_delta(
        ZONE_GROUP_ID,
        _node(
            "properties.privateDnsZoneConfigs",
            "Array",
            _deleted("0", {"name": "privatelink-database"}),
        ),
    )
    first, second = _pair(tmp_path, [record])
    assert whatif.apply_gate(first, second, now=NOW) == [
        f"delete:{ZONE_GROUP_ID} at properties.privateDnsZoneConfigs[0]"
        " element privatelink-database"
    ]


@pytest.mark.parametrize(
    ("record", "refusal"),
    [
        (
            _with_delta(
                STORAGE_ID,
                _deleted(
                    "properties.networkAcls.ipRules",
                    [{"action": "Allow", "value": "20.91.223.203"}],
                ),
            ),
            f"delete:{STORAGE_ID} at properties.networkAcls.ipRules property",
        ),
        (
            _with_delta(STORAGE_ID, _deleted("properties.privateEndpointVNetPolicies", "x")),
            f"delete:{STORAGE_ID} at properties.privateEndpointVNetPolicies property",
        ),
        (
            _with_delta(
                VNET_ID,
                _node(
                    "properties.virtualNetworkPeerings",
                    "Array",
                    _node("0", "Modify", _deleted("properties.allowForwardedTraffic", True)),
                ),
            ),
            f"delete:{VNET_ID} at properties.virtualNetworkPeerings[0]"
            ".properties.allowForwardedTraffic property",
        ),
    ],
    ids=["whole-ip-rules", "noise-of-another-type", "unpinned-peering-property"],
)
def test_gate_refuses_an_unpinned_property_delete(
    tmp_path: Path, record: dict[str, Any], refusal: str
) -> None:
    first, second = _pair(tmp_path, [record])
    assert whatif.apply_gate(first, second, now=NOW) == [refusal]


def test_gate_accepts_an_all_noise_preview(tmp_path: Path) -> None:
    first, second = _pair(tmp_path, NOISE_ONLY)
    assert whatif.apply_gate(first, second, now=NOW) == []
    text = whatif.summary(whatif.load_changes(second))
    assert "REFUSED" not in text
    assert f"  read-only Delete: {VNET_ID} at properties.privateEndpointVNetPolicies" in text
    assert (
        f"  read-only Delete: {VNET_ID} at properties.virtualNetworkPeerings[0]"
        ".properties.remoteAddressSpace"
    ) in text
    assert f"  read-only Delete: {IDENTITY_ID} at properties" in text
    assert "Nested Delete: 7 (refused 0)" in text


def test_summary_lists_every_nested_delete(tmp_path: Path) -> None:
    peering = _with_delta(PEERING_ID, _deleted("properties.peeringSyncLevel", "RemoteNotInSync"))
    unpinned = _with_delta(
        f"{STORAGE_ID}2",
        _deleted("properties.networkAcls.ipRules", [{"value": "20.91.223.203"}]),
    )
    path = _write(tmp_path / "a.json", [SUBNET_DELETE, NSG_RULE_DELETE, peering, unpinned])
    text = whatif.summary(whatif.load_changes(path))
    assert "Modify: 4" in text
    assert "Nested Delete: 4 (refused 3)" in text
    assert f"REFUSED Delete element: {VNET_ID} at properties.subnets[2] (snet-aca-int)" in text
    assert (
        f"REFUSED Delete element: {NSG_ID} at properties.securityRules[1] ({NSG_RULE_ID})" in text
    )
    assert f"REFUSED Delete property: {STORAGE_ID}2 at properties.networkAcls.ipRules" in text
    assert f"  read-only Delete: {PEERING_ID} at properties.peeringSyncLevel" in text


def test_read_only_delete_noise_is_pinned() -> None:
    """Each entry hides a property Delete from the gate, so additions must be deliberate.

    Observed in the Dev what-if previews: read-only or provider-defaulted
    properties that the templates do not set. Array-element deletes are never noise.
    """
    peering = {
        "properties.doNotVerifyRemoteGateways",
        "properties.peerCompleteVnets",
        "properties.peeringSyncLevel",
        "properties.remoteAddressSpace",
        "properties.remoteVirtualNetworkAddressSpace",
    }
    expected = {
        "microsoft.cognitiveservices/accounts": {
            "properties.associatedProjects",
            "properties.defaultProject",
        },
        "microsoft.cognitiveservices/accounts/deployments": {"properties.currentCapacity"},
        "microsoft.cognitiveservices/accounts/projects": {
            "kind",
            "properties.agentIdentity",
            "properties.endpoints",
            "properties.internalId",
            "properties.isDefault",
        },
        "microsoft.cognitiveservices/accounts/projects/connections": {
            "properties.group",
            "properties.isDefault",
        },
        "microsoft.consumption/budgets": {"properties.timePeriod.endDate"},
        "microsoft.servicebus/namespaces/queues": {"properties.maxMessageSizeInKilobytes"},
        "microsoft.servicebus/namespaces/topics": {"properties.maxMessageSizeInKilobytes"},
        "microsoft.containerregistry/registries": {
            "properties.dataEndpointEnabled",
            "properties.encryption",
        },
        "microsoft.managedidentity/userassignedidentities": {"properties"},
        "microsoft.network/privatednszones/virtualnetworklinks": {"properties.resolutionPolicy"},
        "microsoft.network/privateendpoints": {
            "properties.billingSku",
            "properties.ipVersionType",
        },
        "microsoft.network/privateendpoints/privatednszonegroups": {
            "properties.privateDnsZoneConfigs[*].id",
            "properties.privateDnsZoneConfigs[*].properties.provisioningState",
            "properties.privateDnsZoneConfigs[*].type",
        },
        "microsoft.network/virtualnetworks": {
            "properties.privateEndpointVNetPolicies",
            *(f"properties.virtualNetworkPeerings[*].{path}" for path in peering),
        },
        "microsoft.network/virtualnetworks/virtualnetworkpeerings": peering,
    }
    assert {key: set(value) for key, value in whatif.KNOWN_READ_ONLY_DELETES.items()} == expected
    patterns = [pattern for value in expected.values() for pattern in value]
    assert not any(pattern.endswith("[*]") for pattern in patterns)
    assert not any("publicNetworkAccess" in pattern for pattern in patterns)
    assert not any(
        word in pattern.casefold()
        for pattern in patterns
        for word in ("iprules", "subnet", "securityrule", "firewall", "acl")
    )


def test_gate_refuses_unsupported(tmp_path: Path) -> None:
    unsupported = f"{GROUP}/providers/Microsoft.Authorization/roleAssignments/abc"
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID), _record(unsupported, "Unsupported")])
    assert whatif.apply_gate(first, second, now=NOW) == [f"unsupported:{unsupported}"]


def test_gate_allows_acknowledged_unsupported_type(tmp_path: Path) -> None:
    unsupported = (
        f"{GROUP}/providers/Microsoft.Insights/components/appi-airrec-dev-swc"
        "/providers/Microsoft.Authorization/roleAssignments/abc"
    )
    first, second = _pair(tmp_path, [_record(unsupported, "Unsupported")])
    ack_file = tmp_path / "ack.json"
    ack_file.write_text(
        json.dumps(
            [
                {
                    "resource_type": "Microsoft.Authorization/roleAssignments",
                    "owner": "simonm43",
                    "acknowledged_on": "2026-11-05",
                    "reason": "copy loop over a runtime output",
                }
            ]
        ),
        encoding="utf-8",
    )
    acks = whatif.load_acks(ack_file)
    assert acks == {
        "microsoft.authorization/roleassignments": whatif.UnsupportedAck(
            resource_type="Microsoft.Authorization/roleAssignments",
            owner="simonm43",
            acknowledged_on=date(2026, 11, 5),
            reason="copy loop over a runtime output",
        )
    }
    assert whatif.apply_gate(first, second, now=NOW, acks=acks) == []


def test_committed_ack_file_is_empty() -> None:
    assert whatif.load_acks(ROOT / "tools" / "env" / "unsupported-ack.json") == {}


@pytest.mark.parametrize(
    "record",
    [
        _record(
            "/subscriptions/00000000-0000-4000-8000-000000000005/resourceGroups/rg/providers/"
            "Microsoft.Storage/storageAccounts/foreign"
        ),
        _record("/providers/Microsoft.Management/managementGroups/tenant-scoped"),
        _record(None, symbolicName="noId"),
    ],
    ids=["prd", "tenant", "missing"],
)
def test_gate_refuses_non_dev_subscription(tmp_path: Path, record: dict[str, Any]) -> None:
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID), record])
    refusals = whatif.apply_gate(first, second, now=NOW)
    assert len(refusals) == 1
    assert refusals[0].startswith("subscription:")


def test_gate_accepts_upper_case_dev_subscription(tmp_path: Path) -> None:
    upper = ENVIRONMENT_ID.replace(DEV, DEV.upper())
    first, second = _pair(tmp_path, [_record(upper)])
    assert whatif.apply_gate(first, second, now=NOW) == []


def test_gate_keeps_another_environment_inside_its_own_subscription(tmp_path: Path) -> None:
    """Deploying Prd: its changes pass, and anything that would touch Dev is refused."""
    prd = "00000000-0000-4000-8000-000000000005"
    inside = ENVIRONMENT_ID.replace(DEV, prd).replace("-dev", "-prd")
    first, second = _pair(tmp_path, [_record(inside), _record(ENVIRONMENT_ID)])
    assert whatif.apply_gate(first, second, now=NOW, subscription=prd) == [
        f"subscription:{ENVIRONMENT_ID}"
    ]


def test_same_tells_whether_a_refactoring_changed_what_dev_gets(tmp_path: Path) -> None:
    """A template refactoring must leave the change set an environment gets unchanged."""
    before = _write(
        tmp_path / "before.json", [_modify(SQL_ID), _record(ENVIRONMENT_ID, "NoChange")]
    )
    after = _write(tmp_path / "after.json", [_record(ENVIRONMENT_ID, "NoChange"), _modify(SQL_ID)])
    changed = _write(tmp_path / "changed.json", [_modify(SQL_ID, "Enabled")])
    assert whatif.main(["same", str(before), str(after)]) == 0
    assert whatif.differences(before, changed) == [ENVIRONMENT_ID, SQL_ID]
    assert whatif.main(["same", str(before), str(changed)]) == 3


def test_a_resource_the_template_ignores_may_change_between_preview_and_apply(
    tmp_path: Path,
) -> None:
    """An app the template does not declare scales between the preview and the apply; an
    Incremental deployment leaves it untouched, so only the declared changes must repeat."""
    other = f"{GROUP}/providers/Microsoft.App/containerApps/ca-airrec-crew-dev"
    scaled = _record(other, "Ignore", before={"properties": {"replicas": 1}})
    rescaled = _record(other, "Ignore", before={"properties": {"replicas": 2}})
    first, second = _pair(tmp_path, [_modify(SQL_ID), scaled], [_modify(SQL_ID), rescaled])
    assert whatif.apply_gate(first, second, now=NOW) == []
    moved = _record(other, "Modify", before={"properties": {"replicas": 1}})
    first, second = _pair(tmp_path, [_modify(SQL_ID), moved], [_modify(SQL_ID), rescaled])
    assert whatif.apply_gate(first, second, now=NOW) == [f"changed:{other}"]


@pytest.mark.parametrize(
    ("status", "error"),
    [("Failed", None), ("Succeeded", {"code": "InvalidTemplate", "message": "bad"})],
)
def test_gate_refuses_failed_whatif(tmp_path: Path, status: str, error: object) -> None:
    first, second = _pair(tmp_path, [_record(ENVIRONMENT_ID)], status=status, error=error)
    refusals = whatif.apply_gate(first, second, now=NOW)
    assert refusals
    assert all(refusal.startswith("status:") for refusal in refusals)


def test_cli_gate_exits_three_on_refusal(tmp_path: Path) -> None:
    changes = [_record(WORKSPACE_ID, "Delete")]
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for path in (first, second):
        path.write_text(
            json.dumps({"status": "Succeeded", "error": None, "changes": changes}),
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, "-m", "tools.env.whatif", "gate", str(first), str(second)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 3
    assert f"delete:{WORKSPACE_ID}" in result.stderr

    passing = [_record(ENVIRONMENT_ID)]
    for path in (first, second):
        path.write_text(
            json.dumps({"status": "Succeeded", "error": None, "changes": passing}),
            encoding="utf-8",
        )
    result = subprocess.run(
        [sys.executable, "-m", "tools.env.whatif", "gate", str(first), str(second)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    summary = subprocess.run(
        [sys.executable, "-m", "tools.env.whatif", "summary", str(first)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert summary.returncode == 0
    assert "Create: 1" in summary.stdout
