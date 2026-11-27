"""One ARROW environment: its inputs and every Azure and Entra name derived from them.

The inputs are ``infra/environments/<name>.json`` (committed, no secrets). Every resource name
follows one convention, so the Bicep templates and these tools compute the same names:
``<kind>-airrec-<env>-<region code>`` for regional resources, ``ca-airrec-<app>-<env>`` for apps
and a 13-character suffix, ARM's ``uniqueString(subscriptionId, resourceGroupId)``, for the names
that must be globally unique (registry, SQL server, Service Bus, the Foundry account
``aif-airrec-<env>-<suffix>`` with its project ``proj-airrec-<env>``). Dev's Foundry account and
project predate that rule and keep their names (``aif-airrec-chat-dev-<uniqueString(resource
group)>``, ``proj-airrec-chat-dev``). ``unique_string`` reproduces ARM's function offline.

    python -m tools.env.environment prd          # the derived names as JSON
"""

from __future__ import annotations

import base64
import json
import re
import struct
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = ROOT / "infra" / "environments"
WORKLOAD = "airrec"
DATABASE = "airrec-ops"
SYSTEMS = ("schedule", "maintenance", "crew", "booking", "slots")
# ARROW's three apps (three container apps spec): the operator app keeps the name "ops".
APPS = ("ops", "tools", "worker", *SYSTEMS, "testbench")
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NAME = re.compile(r"[a-z][a-z0-9]{1,5}")
_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _fmix(value: int) -> int:
    value ^= value >> 16
    value = value * 0x85EBCA6B & 0xFFFFFFFF
    value ^= value >> 13
    value = value * 0xC2B2AE35 & 0xFFFFFFFF
    return value ^ (value >> 16)


def _murmur64(data: bytes) -> int:
    """ARM's 64-bit MurmurHash variant behind ``uniqueString``."""
    c1, c2 = 0x239B961B, 0xAB0E9789
    length, h1, h2, index = len(data), 0, 0, 0
    while index + 7 < length:
        k1, k2 = struct.unpack_from("<II", data, index)
        h1 ^= _rotl(k1 * c1 & 0xFFFFFFFF, 15) * c2 & 0xFFFFFFFF
        h1 = (_rotl(h1, 19) + h2) & 0xFFFFFFFF
        h1 = (h1 * 5 + 0x561CCD1B) & 0xFFFFFFFF
        h2 ^= _rotl(k2 * c2 & 0xFFFFFFFF, 17) * c1 & 0xFFFFFFFF
        h2 = (_rotl(h2, 13) + h1) & 0xFFFFFFFF
        h2 = (h2 * 5 + 0x0BCAA747) & 0xFFFFFFFF
        index += 8
    remaining = length - index
    if remaining:
        k1 = int.from_bytes(data[index : index + min(remaining, 4)], "little")
        h1 ^= _rotl(k1 * c1 & 0xFFFFFFFF, 15) * c2 & 0xFFFFFFFF
        if remaining > 4:
            k2 = int.from_bytes(data[index + 4 : index + remaining], "little")
            h2 ^= _rotl(k2 * c2 & 0xFFFFFFFF, 17) * c1 & 0xFFFFFFFF
    h1 ^= length
    h2 ^= length
    h1 = (h1 + h2) & 0xFFFFFFFF
    h2 = (h2 + h1) & 0xFFFFFFFF
    h1, h2 = _fmix(h1), _fmix(h2)
    h1 = (h1 + h2) & 0xFFFFFFFF
    h2 = (h2 + h1) & 0xFFFFFFFF
    return (h2 << 32) | h1


def unique_string(*values: str) -> str:
    """ARM's ``uniqueString(values...)``: 13 base-32 characters of a 64-bit hash."""
    digest = _murmur64("-".join(values).encode("utf-8"))
    characters = []
    for _ in range(13):
        characters.append(_BASE32[digest >> 59])
        digest = (digest << 5) & 0xFFFFFFFFFFFFFFFF
    return "".join(characters)


@dataclass(frozen=True)
class ModelDeployment:
    name: str
    model: str
    version: str
    capacity: int
    quota: str


@dataclass(frozen=True)
class Environment:
    name: str
    label: str
    subscription_id: str
    tenant_id: str
    location: str
    region_code: str
    owner: str
    cost_center: str
    address_prefix: str
    private_endpoint_subnet_prefix: str
    aca_subnet_prefix: str
    developer_vnet_id: str
    sql_administrator: str
    budget_usd: float
    log_daily_cap_gb: float
    models: tuple[ModelDeployment, ...]
    operators: tuple[str, ...]
    testbench_owners: tuple[str, ...]
    arrow_app_id: str
    systems_app_id: str
    policy_assignments: tuple[str, ...]

    @property
    def resource_group(self) -> str:
        return f"rg-{WORKLOAD}-{self.name}-{self.region_code}"

    @property
    def group_id(self) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"

    @property
    def suffix(self) -> str:
        return unique_string(self.subscription_id, self.group_id)

    @property
    def tags(self) -> dict[str, str]:
        return {
            "workload": WORKLOAD,
            "environment": self.name,
            "owner": self.owner,
            "costCenter": self.cost_center,
        }

    def app(self, name: str) -> str:
        return f"ca-{WORKLOAD}-{name}-{self.name}"

    def identity(self, name: str) -> str:
        """The managed identity a component runs as; Schedule and the day jobs use 'systems'."""
        return f"id-{WORKLOAD}-{'systems' if name == 'schedule' else name}-{self.name}"

    def names(self) -> dict[str, str]:
        """Every name the templates and tools use for this environment."""
        regional = f"{WORKLOAD}-{self.name}-{self.region_code}"
        vnet = f"vnet-{regional}"
        # As in infra/foundry/names.bicep: Dev's Foundry account and project predate the rule.
        if self.name == "dev":
            account = f"aif-{WORKLOAD}-chat-dev-{unique_string(self.group_id)}"
            project = f"proj-{WORKLOAD}-chat-dev"
        else:
            account = f"aif-{WORKLOAD}-{self.name}-{self.suffix}"
            project = f"proj-{WORKLOAD}-{self.name}"
        names = {
            "subscriptionId": self.subscription_id,
            "tenantId": self.tenant_id,
            "location": self.location,
            "resourceGroup": self.resource_group,
            "vnet": vnet,
            "privateEndpointNsg": f"{vnet}-snet-private-endpoints-nsg-{self.location}",
            "acaNsg": f"nsg-{WORKLOAD}-aca-{self.name}-{self.region_code}",
            "sqlServer": f"sql-{WORKLOAD}-{self.name}-{self.suffix}",
            "sqlServerFqdn": f"sql-{WORKLOAD}-{self.name}-{self.suffix}.database.windows.net",
            "sqlPrivateEndpoint": f"pe-{WORKLOAD}-sql-{self.name}-{self.region_code}",
            "database": DATABASE,
            "registry": f"cr{WORKLOAD}{self.name}{self.suffix}",
            "registryServer": f"cr{WORKLOAD}{self.name}{self.suffix}.azurecr.io",
            "logAnalytics": f"log-{regional}",
            "appInsights": f"appi-{regional}",
            "budget": f"budget-{WORKLOAD}-{self.name}",
            "containerEnvironment": f"cae-{regional}",
            "serviceBus": f"sb-{WORKLOAD}-{self.name}-{self.suffix}",
            "serviceBusFqdn": f"sb-{WORKLOAD}-{self.name}-{self.suffix}.servicebus.windows.net",
            "foundryAccount": account,
            "foundryProject": project,
            "projectEndpoint": (f"https://{account}.services.ai.azure.com/api/projects/{project}"),
            "foundryPortalUrl": self.foundry_portal_url(account, project),
            "dayLoadJob": f"caj-{WORKLOAD}-dayload-{self.name}",
            "dayResetJob": f"caj-{WORKLOAD}-dayreset-{self.name}",
            "migrationJob": f"caj-{WORKLOAD}-dbmigrate-{self.name}",
            "sqlAdminIdentity": f"id-{WORKLOAD}-sqladmin-{self.name}",
            "arrowAppRegistration": f"{WORKLOAD}-app-{self.name}",
            "systemsAppRegistration": f"{WORKLOAD}-systems-{self.name}",
            "testbenchRole": f"Airrec test bench operator ({self.label})",
            # Foundry's MCP connection to ARROW's tools. Dev's name predates the rule that
            # resource names stay generic; its agents' definitions still refer to it.
            "toolsConnection": "arrow-tools" if self.name == "dev" else "ops-tools",
        }
        for app in APPS:
            names[f"app.{app}"] = self.app(app)
        # "ops" is the workers' identity, the one writer; the operator app and the tools app
        # have their own (three container apps spec).
        for component in (
            "ops",
            "operator",
            "tools",
            "systems",
            "maintenance",
            "crew",
            "booking",
            "slots",
        ):
            names[f"identity.{component}"] = f"id-{WORKLOAD}-{component}-{self.name}"
        names["identity.testbench"] = f"id-{WORKLOAD}-testbench-{self.name}"
        return names

    def foundry_portal_url(self, account: str, project: str) -> str:
        """The Foundry portal's address of the project: the subscription id as URL-safe base64
        of its bytes, then the resource group, account and project."""
        encoded = base64.urlsafe_b64encode(uuid.UUID(self.subscription_id).bytes)
        subscription = encoded.decode("ascii").rstrip("=")
        return (
            f"https://ai.azure.com/nextgen/r/{subscription},{self.resource_group},,"
            f"{account},{project}"
        )

    def common_parameters(self) -> dict[str, str]:
        """The parameters every resource-group template takes when it declares them."""
        return {
            "env": self.name,
            "regionCode": self.region_code,
            "owner": self.owner,
            "costCenter": self.cost_center,
        }


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key}: a string is required")
    return value


def _guids(values: Any, key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, list) or (not values and not allow_empty):
        raise ValueError(f"{key}: a list of object ids is required")
    for value in values:
        if not isinstance(value, str) or not GUID.fullmatch(value):
            raise ValueError(f"{key}: {value!r} is not a lower-case GUID")
    return tuple(values)


def parse(raw: dict[str, Any]) -> Environment:
    """An environment from its JSON definition; ValueError names the first bad field."""
    name = _text(raw, "name")
    if not _NAME.fullmatch(name):
        raise ValueError("name: 2-6 lower-case letters or digits, starting with a letter")
    subscription = _text(raw, "subscriptionId")
    tenant = _text(raw, "tenantId")
    for key, value in (("subscriptionId", subscription), ("tenantId", tenant)):
        if not GUID.fullmatch(value):
            raise ValueError(f"{key}: a lower-case GUID is required")
    region_code = _text(raw, "regionCode")
    if not re.fullmatch(r"[a-z]{2,4}", region_code):
        raise ValueError("regionCode: 2-4 lower-case letters")
    administrator = _text(raw, "sqlAdministrator")
    if administrator not in {"developer", "identity"}:
        raise ValueError("sqlAdministrator: developer or identity")
    developer_vnet = raw.get("developerVnetId", "")
    if not isinstance(developer_vnet, str):
        raise ValueError("developerVnetId: a string is required")
    tags, network, apps = raw.get("tags"), raw.get("network"), raw.get("apps")
    if not isinstance(tags, dict) or not isinstance(network, dict) or not isinstance(apps, dict):
        raise ValueError("tags, network and apps: objects are required")
    models = []
    for item in raw.get("models") or []:
        models.append(
            ModelDeployment(
                name=_text(item, "name"),
                model=_text(item, "model"),
                version=_text(item, "version"),
                capacity=int(item["capacity"]),
                quota=_text(item, "quota"),
            )
        )
    if not models:
        raise ValueError("models: at least one model deployment is required")
    app_ids = {}
    for key in ("arrow", "systems"):
        value = apps.get(key, "")
        if not isinstance(value, str) or (value and not GUID.fullmatch(value)):
            raise ValueError(f"apps.{key}: empty or a lower-case GUID")
        app_ids[key] = value
    budget, cap = raw.get("budgetUsd"), raw.get("logDailyCapGb")
    if not isinstance(budget, (int, float)) or budget <= 0:
        raise ValueError("budgetUsd: a positive amount is required")
    if not isinstance(cap, (int, float)) or cap <= 0:
        raise ValueError("logDailyCapGb: a positive amount is required")
    policies = raw.get("policyAssignments") or []
    if not isinstance(policies, list) or not all(isinstance(item, str) for item in policies):
        raise ValueError("policyAssignments: a list of assignment names is required")
    return Environment(
        name=name,
        label=_text(raw, "label"),
        subscription_id=subscription,
        tenant_id=tenant,
        location=_text(raw, "location"),
        region_code=region_code,
        owner=_text(tags, "owner"),
        cost_center=_text(tags, "costCenter"),
        address_prefix=_text(network, "addressPrefix"),
        private_endpoint_subnet_prefix=_text(network, "privateEndpointSubnetPrefix"),
        aca_subnet_prefix=_text(network, "acaSubnetPrefix"),
        developer_vnet_id=developer_vnet,
        sql_administrator=administrator,
        budget_usd=float(budget),
        log_daily_cap_gb=float(cap),
        models=tuple(models),
        operators=_guids(raw.get("operators"), "operators"),
        testbench_owners=_guids(raw.get("testbenchOwners"), "testbenchOwners"),
        arrow_app_id=app_ids["arrow"],
        systems_app_id=app_ids["systems"],
        policy_assignments=tuple(policies),
    )


def load(name: str, directory: Path = ENVIRONMENTS) -> Environment:
    """The environment ``name`` from ``infra/environments/<name>.json``."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"unknown environment {name!r}")
    path = directory / f"{name}.json"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in directory.glob("*.json")))
        raise ValueError(f"unknown environment {name!r}; known: {known}")
    environment = parse(json.loads(path.read_text(encoding="utf-8")))
    if environment.name != name:
        raise ValueError(f"{path.name} names environment {environment.name!r}")
    return environment


def known() -> dict[str, Environment]:
    """Every committed environment, by subscription id."""
    return {env.subscription_id: env for env in map(load, sorted(_stems()))}


def _stems() -> list[str]:
    return [path.stem for path in ENVIRONMENTS.glob("*.json")]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m tools.env.environment <environment>", file=sys.stderr)
        return 2
    try:
        environment = load(arguments[0])
    except ValueError as error:
        print(f"environment: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"name": environment.name, **environment.names()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
