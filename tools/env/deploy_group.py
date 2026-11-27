"""Preview, and with --apply deploy, one resource-group template into one environment.

    python -m tools.env.deploy_group <environment> <template key> [--image sha256:...]
    python -m tools.env.deploy_group <environment> <template key> ... --apply --increment-usd <usd>

The template keys are in tools/env/wiring.py (for example foundry, prerequisites, system-crew,
ops). The parameters come from tools/env/wiring.py: the environment's names and the values looked
up live, so nothing is typed by hand. Without --apply it validates the template and writes the
what-if to .local/deploy/<environment>-<key>.whatif.json; nothing changes. With --apply it runs
the cost gate (actual + forecast + --increment-usd within the environment's budget), a fresh
what-if that must repeat the preview of the last 60 minutes, the what-if gate (no delete, nothing
outside the environment's subscription) and then the deployment, through Azure Resource
Manager's REST API.

ARROW's template (key ops) takes the Easy Auth sign-in secret: --signin-secret current reads it
from the running app, new adds a client secret to the ARROW app registration. It is held in
memory and sent only in the deployment request; a preview uses a placeholder, because what-if
does not evaluate secure parameters.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.env import costgate, whatif, wiring
from tools.env.environment import ROOT, Environment, load
from tools.env.rest import Rest, RestError

DEPLOYMENTS_API = "2024-03-01"
PLACEHOLDER = "what-if-does-not-read-secure-parameters"
SECRET = "entra-client-secret"
SECRET_DAYS = 180
TERMINAL = {"Succeeded", "Failed", "Canceled"}
# A template that grants AcrPull and creates the app or job pulling with it in one deployment
# fails while the new role assignment propagates; the same body is deployed again.
PROPAGATING = "unable to pull image using Managed identity"
PROPAGATION_RETRIES = 5
PROPAGATION_WAIT_SECONDS = 60.0


def bicep() -> str:
    """The Bicep CLI the offline contract checks use (Azure CLI's copy), else one on PATH."""
    installed = Path.home() / ".azure" / "bin" / "bicep.exe"
    found = str(installed) if installed.is_file() else shutil.which("bicep")
    if not found:
        raise SystemExit("Bicep CLI not found: run `az bicep install`.")
    return found


def compile_template(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [bicep(), "build", str(path), "--stdout", "--no-restore"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"bicep build {path} failed:\n{result.stderr}")
    template: dict[str, Any] = json.loads(result.stdout)
    return template


def required(template: dict[str, Any]) -> set[str]:
    return {
        name
        for name, declaration in (template.get("parameters") or {}).items()
        if "defaultValue" not in declaration
    }


def signin_secret(live: wiring.Live, mode: str) -> str:
    """ARROW's Easy Auth client secret, in memory only: the running app's, or a new one."""
    names = live.names
    if mode == "current":
        app = f"{live.env.group_id}/providers/Microsoft.App/containerApps/{names['app.ops']}"
        listed = live.rest.arm("POST", f"{app}/listSecrets", api_version=wiring.APPS_API).body
        values = [item["value"] for item in listed.get("value", []) if item.get("name") == SECRET]
        if len(values) != 1:
            raise SystemExit(f"{names['app.ops']} holds no {SECRET}; use --signin-secret new")
        return str(values[0])
    app_id = live.app_id("arrow")
    registration = live.rest.graph("GET", f"/applications(appId='{app_id}')?$select=id").body
    ends = (datetime.now(UTC) + timedelta(days=SECRET_DAYS)).replace(microsecond=0)
    created = live.rest.graph(
        "POST",
        f"/applications/{registration['id']}/addPassword",
        {
            "passwordCredential": {
                "displayName": f"easyauth-{live.env.name}-{datetime.now(UTC):%Y%m%d}",
                "endDateTime": ends.isoformat().replace("+00:00", "Z"),
            }
        },
    ).body
    registration_name = names["arrowAppRegistration"]
    print(f"Added an Easy Auth client secret to {registration_name}, ends {ends:%Y-%m-%d}.")
    return str(created["secretText"])


def _deployment_path(env: Environment, name: str) -> str:
    return f"{env.group_id}/providers/Microsoft.Resources/deployments/{name}"


def _body(template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    return {
        "properties": {
            "mode": "Incremental",
            "template": template,
            "parameters": {name: {"value": value} for name, value in values.items()},
        }
    }


def _what_if(rest: Rest, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = json.loads(json.dumps(body))
    request["properties"]["whatIfSettings"] = {"resultFormat": "FullResourcePayloads"}
    response = rest.wait(rest.arm("POST", f"{path}/whatIf", request, api_version=DEPLOYMENTS_API))
    result: dict[str, Any] = response.body
    return result


def _validate(rest: Rest, path: str, body: dict[str, Any]) -> None:
    response = rest.wait(rest.arm("POST", f"{path}/validate", body, api_version=DEPLOYMENTS_API))
    error = (response.body or {}).get("error")
    if error:
        raise SystemExit(f"Validation failed: {error.get('code')}: {error.get('message')}")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _cost_gate(env: Environment, increment: float, out: Path) -> None:
    costgate.main(["bodies", "--out-dir", str(out)])
    base = f"https://management.azure.com/subscriptions/{env.subscription_id}"
    base += "/providers/Microsoft.CostManagement"
    for kind, request, result in (
        ("query", "cost-query.json", "cost-actual.json"),
        ("forecast", "cost-forecast-body.json", "cost-forecast.json"),
    ):
        url = f"{base}/{kind}?api-version={costgate.API_VERSION}"
        costgate.main(
            [
                "fetch",
                "--subscription",
                env.subscription_id,
                "--url",
                url,
                "--body",
                str(out / request),
                "--out",
                str(out / result),
            ]
        )
    code = costgate.main(
        [
            "check",
            "--actual",
            str(out / "cost-actual.json"),
            "--forecast",
            str(out / "cost-forecast.json"),
            "--increment-usd",
            str(increment),
            "--approved-ceiling-usd",
            str(env.budget_usd),
        ]
    )
    if code != 0:
        raise SystemExit("The cost gate refused the deployment.")


def _create(
    rest: Rest,
    path: str,
    body: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Deploy and wait. Only a failure because a just-granted pull role has not propagated yet is
    retried, with the same (approved) body."""
    for attempt in range(PROPAGATION_RETRIES + 1):
        rest.arm("PUT", path, body, api_version=DEPLOYMENTS_API)
        while True:
            sleep(10)
            deployment: dict[str, Any] = rest.arm("GET", path, api_version=DEPLOYMENTS_API).body
            state = deployment["properties"]["provisioningState"]
            if state in TERMINAL:
                break
        if state == "Succeeded":
            return deployment
        error = json.dumps(deployment["properties"].get("error") or {})
        if PROPAGATING in error and attempt < PROPAGATION_RETRIES:
            print(
                "A role granted in this deployment has not reached the registry yet; deploying "
                f"again in {PROPAGATION_WAIT_SECONDS:.0f} s ({attempt + 1} of "
                f"{PROPAGATION_RETRIES})."
            )
            sleep(PROPAGATION_WAIT_SECONDS)
            continue
        raise SystemExit(f"Deployment {state}: {error[:1500]}")
    raise AssertionError("unreachable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.env.deploy_group", description=__doc__)
    parser.add_argument("environment")
    parser.add_argument("key", choices=sorted(wiring.TEMPLATES))
    parser.add_argument("--image", help="sha256 digest, for templates that run an image")
    parser.add_argument("--signin-secret", choices=("current", "new"), default="current")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--increment-usd", type=float)
    parser.add_argument("--max-age-minutes", type=int, default=60)
    parser.add_argument(
        "--cutover",
        action="store_true",
        help="ops only, in the deployment guide's 'From one ARROW app to three': keep the"
        " workers' identity on the operator app until the guide's next command removes it",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, rest: Rest | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.apply and arguments.increment_usd is None:
        print("--apply needs --increment-usd (0 when nothing new costs money)", file=sys.stderr)
        return 2
    if arguments.cutover and arguments.key != "ops":
        print("--cutover is only for ops", file=sys.stderr)
        return 2
    env = load(arguments.environment)
    rest = rest or Rest(env.tenant_id)
    live = wiring.Live(rest, env, cutover=arguments.cutover)
    key = arguments.key
    template = compile_template(ROOT / wiring.TEMPLATES[key].path)
    declared = template.get("parameters") or {}
    try:
        values = wiring.parameters(live, key, declared, image=arguments.image)
    except ValueError as error:
        print(f"{key}: {error}", file=sys.stderr)
        return 2
    if "clientSecret" in declared:
        values["clientSecret"] = PLACEHOLDER
    missing = sorted(required(template) - set(values))
    if missing:
        raise SystemExit(f"{key}: no value for {', '.join(missing)}")
    shown = {name: value for name, value in values.items() if name != "clientSecret"}
    print(f"{key} -> {env.resource_group} ({env.subscription_id})")
    print(json.dumps(shown, indent=1))

    deployment = f"airrec-{env.name}-{key}"
    path = _deployment_path(env, deployment)
    out = ROOT / ".local" / "deploy"
    preview = out / f"{env.name}-{key}.whatif.json"
    try:
        _validate(rest, path, _body(template, values))
        if not arguments.apply:
            _write(preview, _what_if(rest, path, _body(template, values)))
            print(whatif.summary(whatif.load_changes(preview)))
            print(f"Preview written to {preview}. Nothing was deployed.")
            return 0
        if not preview.is_file():
            print(f"No preview at {preview}: run without --apply first.", file=sys.stderr)
            return 2
        _cost_gate(env, arguments.increment_usd, out)
        current = out / f"{env.name}-{key}.apply.whatif.json"
        _write(current, _what_if(rest, path, _body(template, values)))
        print(whatif.summary(whatif.load_changes(current)))
        refusals = whatif.apply_gate(
            preview,
            current,
            now=datetime.now(UTC),
            max_age=timedelta(minutes=arguments.max_age_minutes),
            acks=whatif.load_acks(),
            subscription=env.subscription_id,
        )
        if refusals:
            print("The what-if gate refused: " + json.dumps(refusals), file=sys.stderr)
            return 3
        if "clientSecret" in declared:
            values["clientSecret"] = signin_secret(live, arguments.signin_secret)
        result = _create(rest, path, _body(template, values))
    except RestError as error:
        print(str(error), file=sys.stderr)
        return 1
    outputs = {
        name: item.get("value")
        for name, item in (result["properties"].get("outputs") or {}).items()
    }
    print(f"Deployment {deployment} succeeded. Outputs: {json.dumps(outputs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
