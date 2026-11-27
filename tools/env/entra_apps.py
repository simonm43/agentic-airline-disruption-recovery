"""The environment's two Entra app registrations, created or brought up to date (Graph REST).

    python -m tools.env.entra_apps <environment>            # read-only: what would change
    python -m tools.env.entra_apps <environment> --apply    # make the changes, then read back

- airrec-app-<env>: ARROW's and the Test Bench's sign-in (Easy Auth, ID tokens only) and the
  audience of ARROW's API and tools (api://<appId>). Its redirect URIs name the apps' domain, so
  they are added once the Container Apps environment exists: run this again after it.
- airrec-systems-<env>: the sample airline systems' API (api://<appId>).
  tools/systems/app_roles.py defines its app roles, assigns them to ARROW and the agents, and then
  requires an assignment.

A new app is created by its unique name (Graph upsert); an existing one gets only the properties
that differ, so running this again changes nothing. You (the signed-in deployer) become an owner
of both apps and their service principals, as in Dev, so you can add ARROW's sign-in secret and
assign the systems' app roles without an Entra directory role. No client secret is created here:
tools/env/deploy_group.py adds one when it deploys ARROW and keeps it in memory. These
registrations are scripted because the Microsoft Graph Bicep extension cannot create client
secrets and what-if does not preview Graph resources, which the deployment gate relies on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

from tools.env.environment import Environment, load
from tools.env.rest import GRAPH, Rest, RestError

APPS_API = "2024-03-01"
SELECT = "$select=id,appId,displayName,uniqueName,signInAudience,identifierUris,web,api"
LATER = "redirect URIs later: the Container Apps environment does not exist yet"
# Entra replicates a new app for a few seconds before every read and reference sees it.
REPLICATION_SECONDS = 90


def _wait(read: Callable[[], Any], what: str, sleep: Callable[[float], None] = time.sleep) -> Any:
    for _ in range(REPLICATION_SECONDS // 5):
        try:
            found = read()
        except RestError as error:
            if error.status not in (400, 404):
                raise
            found = None
        if found:
            return found
        sleep(5)
    raise SystemExit(f"{what} did not appear within {REPLICATION_SECONDS} s")


def _find(rest: Rest, env: Environment, kind: str, name: str) -> dict[str, Any] | None:
    """The app by its pinned client ID (Dev's predate unique names), else by unique name, else by
    display name if exactly one has it."""
    pinned = env.arrow_app_id if kind == "arrow" else env.systems_app_id
    queries = (
        [f"appId eq '{pinned}'"]
        if pinned
        else [f"uniqueName eq '{name}'", f"displayName eq '{name}'"]
    )
    for expression in queries:
        query = quote(expression, safe="'")
        found = rest.graph_items(f"/applications?$filter={query}&{SELECT}")
        if len(found) > 1:
            raise SystemExit(f"{len(found)} app registrations match {expression}; resolve it")
        if found:
            return found[0]
    if pinned:
        raise SystemExit(f"the pinned {kind} app {pinned} does not exist")
    return None


def _apps_domain(rest: Rest, env: Environment) -> str | None:
    path = (
        f"{env.group_id}/providers/Microsoft.App/managedEnvironments/"
        f"{env.names()['containerEnvironment']}"
    )
    try:
        return str(rest.arm("GET", path, api_version=APPS_API).body["properties"]["defaultDomain"])
    except RestError as error:
        if error.status == 404:
            return None
        raise


def desired(env: Environment, domain: str | None) -> dict[str, dict[str, Any]]:
    names = env.names()
    redirects = [
        f"https://{names[app]}.{domain}/.auth/login/aad/callback"
        for app in ("app.ops", "app.testbench")
        if domain
    ]
    common = {"signInAudience": "AzureADMyOrg", "api": {"requestedAccessTokenVersion": 2}}
    return {
        "arrow": {
            "displayName": names["arrowAppRegistration"],
            **common,
            "web": {
                "redirectUris": redirects,
                "implicitGrantSettings": {
                    "enableIdTokenIssuance": True,
                    "enableAccessTokenIssuance": False,
                },
            },
        },
        "systems": {"displayName": names["systemsAppRegistration"], **common},
    }


def differences(found: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """The top-level properties to PATCH: only the parts that differ. Redirect URIs are added to,
    never replaced, so ones added by hand stay."""
    update: dict[str, Any] = {}
    for key, value in body.items():
        have = found.get(key)
        if key == "web":
            web = have or {}
            part: dict[str, Any] = {}
            uris = list(web.get("redirectUris") or [])
            missing = [uri for uri in value["redirectUris"] if uri not in uris]
            if missing:
                part["redirectUris"] = uris + missing
            grants = web.get("implicitGrantSettings") or {}
            if any(grants.get(k) != v for k, v in value["implicitGrantSettings"].items()):
                part["implicitGrantSettings"] = {**grants, **value["implicitGrantSettings"]}
            if part:
                update["web"] = part
        elif isinstance(value, dict):
            if any((have or {}).get(k) != v for k, v in value.items()):
                update[key] = value
        elif have != value:
            update[key] = value
    return update


def plan(rest: Rest, env: Environment) -> list[dict[str, Any]]:
    """Per app: its client ID if it exists, and the changes --apply would make."""
    domain = _apps_domain(rest, env)
    me = _me(rest)
    steps = []
    for kind, body in desired(env, domain).items():
        found = _find(rest, env, kind, body["displayName"])
        changes: list[str] = []
        if found is None:
            changes += [
                "create (upsert by unique name)",
                "identifier URI api://<appId>",
                "service principal",
                "you as owner of both",
            ]
        else:
            changes.extend(f"set {key}" for key in differences(found, body))
            if f"api://{found['appId']}" not in (found.get("identifierUris") or []):
                changes.append("identifier URI api://<appId>")
            if not _owned(rest, "applications", found["id"], me):
                changes.append("you as owner of the app")
            principal = _principal(rest, found["appId"])
            if principal is None:
                changes += ["service principal", "you as owner of the service principal"]
            elif not _owned(rest, "servicePrincipals", principal["id"], me):
                changes.append("you as owner of the service principal")
        if kind == "arrow" and domain is None:
            changes.append(LATER)
        app_id = (found or {}).get("appId")
        steps.append({"app": body["displayName"], "appId": app_id, "changes": changes})
    return steps


def _me(rest: Rest) -> str:
    return str(rest.graph("GET", "/me?$select=id").body["id"])


def _principal(rest: Rest, app_id: str) -> dict[str, Any] | None:
    try:
        return dict(rest.graph("GET", f"/servicePrincipals(appId='{app_id}')?$select=id").body)
    except RestError as error:
        if error.status != 404:
            raise
        return None


def _owned(rest: Rest, collection: str, object_id: str, me: str) -> bool:
    owners = rest.graph_items(f"/{collection}/{object_id}/owners?$select=id")
    return any(owner.get("id") == me for owner in owners)


def _own(rest: Rest, collection: str, object_id: str, me: str) -> None:
    """The deployer owns the app and its service principal (as in Dev): owners add the sign-in
    secret and assign the systems' app roles without a directory role."""
    if not _owned(rest, collection, object_id, me):
        rest.graph(
            "POST",
            f"/{collection}/{object_id}/owners/$ref",
            {"@odata.id": f"{GRAPH}/directoryObjects/{me}"},
        )


def apply(rest: Rest, env: Environment) -> list[dict[str, Any]]:
    domain = _apps_domain(rest, env)
    me = _me(rest)
    for kind, body in desired(env, domain).items():
        name = body["displayName"]
        found = _find(rest, env, kind, name)
        if found is None:
            rest.graph(
                "PATCH",
                f"/applications(uniqueName='{name}')",
                body,
                headers={"Prefer": "create-if-missing"},
            )
            app = _wait(lambda: _find(rest, env, kind, name), f"app registration {name}")
        else:
            update = differences(found, body)
            if update:
                rest.graph("PATCH", f"/applications/{found['id']}", update)
            app = found
        uri = f"api://{app['appId']}"
        if uri not in (app.get("identifierUris") or []):
            rest.graph(
                "PATCH",
                f"/applications/{app['id']}",
                {"identifierUris": [*(app.get("identifierUris") or []), uri]},
            )
        principal = _principal(rest, app["appId"]) or _wait(
            lambda: rest.graph("POST", "/servicePrincipals", {"appId": app["appId"]}).body,
            f"service principal of {name}",
        )
        _own(rest, "applications", app["id"], me)
        _own(rest, "servicePrincipals", principal["id"], me)
    return plan(rest, env)


def main(argv: Sequence[str] | None = None, *, rest: Rest | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.env.entra_apps", description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    env = load(arguments.environment)
    rest = rest or Rest(env.tenant_id)
    if not arguments.apply:
        print(json.dumps(plan(rest, env), indent=2))
        print("Read-only: nothing changed. Re-run with --apply.")
        return 0
    result = apply(rest, env)
    for _ in range(REPLICATION_SECONDS // 5):
        if all(change == LATER for step in result for change in step["changes"]):
            break
        time.sleep(5)
        result = plan(rest, env)
    print(json.dumps(result, indent=2))
    if any(change != LATER for step in result for change in step["changes"]):
        print("Read-back still differs from the intended state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
