"""Sign-in and access for the ARROW Test Bench (event trigger and test bench spec §5.2).

    python -m tools.testbench.configure_access --environment prd            # read-only plan
    python -m tools.testbench.configure_access --environment prd --apply    # make the changes

Run it after ARROW is deployed. With --apply, in order, each read back:
1. checks the Test Bench's sign-in redirect URI on ARROW's app registration
   (tools/env/entra_apps.py adds it; no new secret);
2. copies ARROW's Easy Auth client secret into the Test Bench app, in memory only;
3. PUTs the Test Bench's Easy Auth: its owners only, sign-in required except /healthz;
4. lets ARROW admit it: TESTBENCH_OIDS, plus Easy Auth allowedApplications/allowedPrincipals
   (infra/ops-app/main.bicep already admits it when the Test Bench exists before ARROW).
No secret value is printed or written to disk. The copy is scripted rather than declared in
infra/testbench/main.bicep because a template would need the secret as a parameter; a redeploy of
that template removes the copy, so run this again after one.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from tools.env import wiring
from tools.env.environment import Environment, load
from tools.env.rest import Rest

API = "2024-03-01"
SECRET = "entra-client-secret"


def bench_auth(issuer: str, app_id: str, owners: list[str]) -> dict[str, Any]:
    return {
        "properties": {
            "platform": {"enabled": True},
            "globalValidation": {
                "unauthenticatedClientAction": "RedirectToLoginPage",
                "redirectToProvider": "azureactivedirectory",
                "excludedPaths": ["/healthz"],
            },
            "identityProviders": {
                "azureActiveDirectory": {
                    "enabled": True,
                    "registration": {
                        "clientId": app_id,
                        "clientSecretSettingName": SECRET,
                        "openIdIssuer": issuer,
                    },
                    "validation": {
                        "allowedAudiences": [f"api://{app_id}", app_id],
                        "defaultAuthorizationPolicy": {"allowedPrincipals": {"identities": owners}},
                    },
                }
            },
            "login": {"tokenStore": {"enabled": False}, "preserveUrlFragmentsForLogins": False},
            "httpSettings": {"requireHttps": True},
        }
    }


class Apps:
    def __init__(self, rest: Rest, env: Environment) -> None:
        names = env.names()
        group = f"{env.group_id}/providers/Microsoft.App/containerApps"
        self.rest = rest
        self.ops = f"{group}/{names['app.ops']}"
        self.bench = f"{group}/{names['app.testbench']}"

    def call(self, method: str, path: str, body: Any = None) -> Any:
        return self.rest.arm(method, path, body, api_version=API).body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--apply", "--execute", action="store_true", dest="apply")
    args = parser.parse_args()

    env = load(args.environment)
    rest = Rest(env.tenant_id)
    live = wiring.Live(rest, env)
    apps = Apps(rest, env)
    app_id = live.app_id("arrow")
    identity = live.identity("testbench")
    fqdn = apps.call("GET", apps.bench)["properties"]["configuration"]["ingress"]["fqdn"]
    callback = f"https://{fqdn}/.auth/login/aad/callback"
    print(f"test bench: https://{fqdn}")
    if not args.apply:
        print("plan: redirect URI, copy sign-in secret, owner-only Easy Auth, ARROW admits it")
        return 0

    # 1. Redirect URI on ARROW's registration.
    registration = rest.graph("GET", f"/applications(appId='{app_id}')?$select=id,web").body
    uris = list(registration["web"]["redirectUris"])
    if callback not in uris:
        rest.graph(
            "PATCH",
            f"/applications/{registration['id']}",
            {"web": {"redirectUris": [*uris, callback]}},
        )
    current = rest.graph("GET", f"/applications/{registration['id']}?$select=web").body
    print(f"1 redirect URI present: {callback in current['web']['redirectUris']}")

    # 2. ARROW's sign-in secret, copied in memory only.
    secrets = apps.call("POST", f"{apps.ops}/listSecrets")["value"]
    value = next(item["value"] for item in secrets if item["name"] == SECRET)
    apps.call(
        "PATCH",
        apps.bench,
        {"properties": {"configuration": {"secrets": [{"name": SECRET, "value": value}]}}},
    )
    del value, secrets
    # The PATCH completes asynchronously; read back until the secret's name appears (60 s).
    names: list[str] = []
    for _ in range(12):
        names = [item["name"] for item in apps.call("POST", f"{apps.bench}/listSecrets")["value"]]
        if SECRET in names:
            break
        time.sleep(5)
    print(f"2 test bench secrets: {names}")

    # 3. Owner-only sign-in.
    ops_auth = apps.call("GET", f"{apps.ops}/authConfigs/current")
    issuer = ops_auth["properties"]["identityProviders"]["azureActiveDirectory"]["registration"][
        "openIdIssuer"
    ]
    apps.call(
        "PUT",
        f"{apps.bench}/authConfigs/current",
        bench_auth(issuer, app_id, list(env.testbench_owners)),
    )
    policy = apps.call("GET", f"{apps.bench}/authConfigs/current")["properties"][
        "identityProviders"
    ]["azureActiveDirectory"]["validation"]["defaultAuthorizationPolicy"]
    print(f"3 test bench admits: {policy}")

    # 4. ARROW admits the Test Bench's identity.
    rules = ops_auth["properties"]["identityProviders"]["azureActiveDirectory"]["validation"][
        "defaultAuthorizationPolicy"
    ]
    applications = rules.setdefault("allowedApplications", [])
    principals = rules.setdefault("allowedPrincipals", {}).setdefault("identities", [])
    if identity.client_id not in applications or identity.principal_id not in principals:
        if identity.client_id not in applications:
            applications.append(identity.client_id)
        if identity.principal_id not in principals:
            principals.append(identity.principal_id)
        apps.call("PUT", f"{apps.ops}/authConfigs/current", {"properties": ops_auth["properties"]})
    after = apps.call("GET", f"{apps.ops}/authConfigs/current")["properties"]["identityProviders"]
    admitted = after["azureActiveDirectory"]["validation"]["defaultAuthorizationPolicy"]
    print(f"4 ARROW admits: {admitted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
