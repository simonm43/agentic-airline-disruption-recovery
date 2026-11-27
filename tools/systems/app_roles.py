"""Entra app roles for the sample airline systems (owner-approved).

Defines one read role per system and Actions.Write on the environment's systems API app
(airrec-systems-<env>), assigns each caller only the roles it needs, then makes the API's service
principal require an assignment, so Entra issues no token for the systems to any other identity.
Each system also checks the token's `roles` claim alongside its caller-oid allowlist
(src/airline_systems/caller.py). The callers are the environment's own: each domain agent's Entra
agent identity, read from Foundry, and ARROW's three apps' managed identities, read from Azure:
the workers' (the only writer), the operator app's and the tools app's (three container apps
spec), which only read.

    python -m tools.systems.app_roles --environment prd            # read-only: what would change
    python -m tools.systems.app_roles --environment prd --apply    # make the changes, read back

Adds what is missing and removes a declared caller's roles it no longer needs. It never removes a
role definition or touches an identity that is not declared here. Prints the read-back as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from tools.env import wiring
from tools.env.environment import load
from tools.env.rest import Rest

ROLES = {
    "Schedule.Read": "Read flights, aircraft rotations, disruptions and recovery options.",
    "Maintenance.Read": "Read defects reported to Maintenance.",
    "Crew.Read": "Read crew duty impact, reserve options and duty plans.",
    "Booking.Read": "Read passenger impact counts, rebooking plans and flight summaries.",
    "Slots.Read": "Read slot capacity checks and airport rules.",
    "Actions.Write": (
        "Apply governed recovery actions and record defects, delays and seat holds in the "
        "sample airline systems."
    ),
}

# Agents read only the systems they use; ARROW's workers read four systems over REST and are the
# only writer. They write defects to Maintenance but never read it, so they have no
# Maintenance.Read. The Detection agent reads only Schedule (its one tool). ARROW's operator and
# tools apps read only what they use (wiring.ARROW_READS).
AGENT_ROLES = {
    "aircraft-recovery-agent": ["Schedule.Read"],
    "disruption-detection-agent": ["Schedule.Read"],
    "crew-recovery-agent": ["Crew.Read"],
    "passenger-reaccommodation-agent": ["Booking.Read"],
    "slot-negotiation-agent": ["Slots.Read"],
}
ARROW_ROLES = ["Schedule.Read", "Crew.Read", "Booking.Read", "Slots.Read", "Actions.Write"]

Callers = Mapping[str, tuple[str, str, list[str]]]


def callers(
    agents: Mapping[str, Mapping[str, Any]],
    arrow: wiring.ArrowIdentities,
    names: Mapping[str, str],
) -> dict[str, tuple[str, str, list[str]]]:
    """Name -> (object id, expected display-name fragment, roles)."""
    found = {
        name: (str(agents[name]["principal_id"]), f"{name}-AgentIdentity", roles)
        for name, roles in AGENT_ROLES.items()
    }
    workers = names["identity.ops"]
    found[f"arrow workers ({workers})"] = (arrow.workers.principal_id, workers, ARROW_ROLES)
    for app, systems in wiring.ARROW_READS.items():
        name = names[f"identity.{app}"]
        roles = [f"{system.capitalize()}.Read" for system in systems]
        found[f"arrow {app} ({name})"] = (getattr(arrow, app).principal_id, name, roles)
    return found


def role_id(api_app_id: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"api://{api_app_id}/roles/{value}"))


def read_state(
    rest: Rest, api_app_id: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    by_app_id = quote(f"appId eq '{api_app_id}'", safe="'")
    [app] = rest.graph_items(f"/applications?$filter={by_app_id}")
    [sp] = rest.graph_items(f"/servicePrincipals?$filter={by_app_id}")
    assigned = rest.graph_items(f"/servicePrincipals/{sp['id']}/appRoleAssignedTo")
    return app, sp, assigned


def plan(
    app: dict[str, Any],
    sp: dict[str, Any],
    assigned: list[dict[str, Any]],
    declared: Callers,
    api_app_id: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]], list[tuple[str, str, str]], bool]:
    """Roles to define, assignments to add, a declared caller's surplus assignments to remove
    (name, assignment id, role), and whether assignment must still be made required."""
    existing = {role["value"]: role for role in app.get("appRoles", [])}
    new_roles = [
        {
            "id": role_id(api_app_id, value),
            "value": value,
            "displayName": value,
            "description": description,
            "allowedMemberTypes": ["Application"],
            "isEnabled": True,
        }
        for value, description in ROLES.items()
        if value not in existing
    ]
    ids = {value: role["id"] for value, role in existing.items()}
    ids.update({role["value"]: role["id"] for role in new_roles})
    have = {(a["principalId"], a["appRoleId"]) for a in assigned}
    missing = [
        (name, oid, value)
        for name, (oid, _, roles) in declared.items()
        for value in roles
        if (oid, ids[value]) not in have
    ]
    names = {role_id_: value for value, role_id_ in ids.items()}
    wanted = {
        oid: (name, {ids[value] for value in roles}) for name, (oid, _, roles) in declared.items()
    }
    surplus = [
        (wanted[a["principalId"]][0], a["id"], names.get(a["appRoleId"], a["appRoleId"]))
        for a in assigned
        if a["principalId"] in wanted and a["appRoleId"] not in wanted[a["principalId"]][1]
    ]
    return new_roles, missing, surplus, not sp.get("appRoleAssignmentRequired", False)


def check_callers(rest: Rest, declared: Callers) -> None:
    for name, (oid, fragment, _) in declared.items():
        principal = rest.graph("GET", f"/servicePrincipals/{oid}?$select=id,displayName").body
        if fragment not in principal["displayName"]:
            raise SystemExit(f"{name}: {oid} is {principal['displayName']!r}, not {fragment!r}")


def summary(rest: Rest, api_app_id: str, declared: Callers) -> dict[str, Any]:
    app, sp, assigned = read_state(rest, api_app_id)
    names = {role["id"]: role["value"] for role in app.get("appRoles", [])}
    by_oid = {oid: name for name, (oid, _, _) in declared.items()}
    holders: dict[str, list[str]] = {}
    for item in assigned:
        caller = by_oid.get(item["principalId"], f"UNEXPECTED {item['principalDisplayName']}")
        holders.setdefault(caller, []).append(names.get(item["appRoleId"], item["appRoleId"]))
    return {
        "api_app": {"appId": app["appId"], "displayName": app["displayName"]},
        "app_roles": [
            {
                "value": r["value"],
                "id": r["id"],
                "allowedMemberTypes": r["allowedMemberTypes"],
                "isEnabled": r["isEnabled"],
            }
            for r in app.get("appRoles", [])
        ],
        "service_principal": {
            "id": sp["id"],
            "appRoleAssignmentRequired": sp.get("appRoleAssignmentRequired"),
        },
        "assignments": {caller: sorted(roles) for caller, roles in sorted(holders.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args()

    env = load(args.environment)
    rest = Rest(env.tenant_id)
    live = wiring.Live(rest, env)
    api_app_id = live.app_id("systems")
    declared = callers(live.agents, live.arrow, live.names)
    check_callers(rest, declared)
    app, sp, assigned = read_state(rest, api_app_id)
    new_roles, missing, surplus, require = plan(app, sp, assigned, declared, api_app_id)
    print(
        json.dumps(
            {
                "api_app": app["displayName"],
                "add_roles": [r["value"] for r in new_roles],
                "add_assignments": [f"{name}: {value}" for name, _, value in missing],
                "remove_assignments": [f"{name}: {value}" for name, _, value in surplus],
                "set_assignment_required": require,
            },
            indent=2,
        )
    )
    if not args.apply:
        print("Dry run: nothing changed. Re-run with --apply.")
        return 0

    if new_roles:
        rest.graph(
            "PATCH",
            f"/applications/{app['id']}",
            {"appRoles": [*app.get("appRoles", []), *new_roles]},
        )
    roles = rest.graph("GET", f"/applications/{app['id']}").body["appRoles"]
    ids = {r["value"]: r["id"] for r in roles}
    for _, oid, value in missing:
        rest.graph(
            "POST",
            f"/servicePrincipals/{sp['id']}/appRoleAssignedTo",
            {"principalId": oid, "resourceId": sp["id"], "appRoleId": ids[value]},
        )
    for _, assignment, _ in surplus:
        rest.graph("DELETE", f"/servicePrincipals/{sp['id']}/appRoleAssignedTo/{assignment}")
    # Only after every caller holds its roles: from now on Entra refuses unassigned callers.
    app, sp, assigned = read_state(rest, api_app_id)
    if plan(app, sp, assigned, declared, api_app_id)[1]:
        raise SystemExit("Some assignments are still missing; assignment not made required.")
    if require:
        rest.graph("PATCH", f"/servicePrincipals/{sp['id']}", {"appRoleAssignmentRequired": True})

    result = summary(rest, api_app_id, declared)
    print(json.dumps(result, indent=2))
    expected = {name: sorted(roles) for name, (_, _, roles) in declared.items()}
    if (
        result["assignments"] != expected
        or not result["service_principal"]["appRoleAssignmentRequired"]
    ):
        print("Read-back differs from the intended state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
