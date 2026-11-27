"""Text checks on the environment scripts in tools/env."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "tools" / "env"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AZ_CALL = re.compile(r"(?<![\w$.'\"`-])az\s+[a-z]")
# The preflight also reads the developer network read-only (tested in test_preflight_gates.py).
ENVIRONMENT_SCRIPTS = {"deploy.ps1"}


def _text(name: str) -> str:
    return (ENV / name).read_text(encoding="utf-8")


def _statements(text: str) -> list[str]:
    """Code lines with backtick continuations joined and comment lines dropped."""
    joined = re.sub(r"`\r?\n\s*", " ", text)
    return [
        line for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _az_calls(text: str) -> list[str]:
    return [line for line in _statements(text) if AZ_CALL.search(line)]


def test_only_the_platform_scripts_remain() -> None:
    """The worker, the Fabric capacity and the first-phase SQL identities are
    retired, so dev-up, dev-down and grant-sql-identities went with them."""
    assert {path.name for path in ENV.glob("*.ps1")} == ENVIRONMENT_SCRIPTS | {"preflight.ps1"}
    assert not (ENV / "grant_sql_identities.py").exists()


@pytest.mark.parametrize("name", sorted(ENVIRONMENT_SCRIPTS))
def test_every_script_targets_only_its_environment(name: str) -> None:
    """The subscription comes from infra/environments/<name>.json, never from the script, and
    every Azure CLI call names it."""
    text = _text(name)
    assert "infra\\environments\\$Environment.json" in text
    assert "$subscription = [string]$definition.subscriptionId" in text
    assert not GUID.search(text)
    calls = _az_calls(text)
    assert calls
    for line in calls:
        assert "--subscription $subscription" in line, line
        if re.search(r"\baz\s+rest\b", line):
            assert "/subscriptions/$subscription/" in line, line


CMD_METACHARACTERS = re.compile(r"[()&|<>^]")
QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")


def test_az_arguments_survive_the_az_cmd_wrapper() -> None:
    """On Windows `az` is az.cmd, so cmd.exe re-parses its arguments. PowerShell quotes an
    argument only when it contains whitespace, so a space-free argument holding a cmd
    metacharacter reaches cmd.exe bare; cmd.exe rejects the line and az still exits 0."""
    for path in ENV.glob("*.ps1"):
        for line in _az_calls(path.read_text(encoding="utf-8")):
            for match in QUOTED.finditer(line):
                value = match.group(1) if match.group(1) is not None else match.group(2)
                if CMD_METACHARACTERS.search(value) and not re.search(r"\s", value):
                    pytest.fail(f"{path.name}: unquoted cmd metacharacter in {value!r}")


def test_deploy_validates_previews_and_gates_before_create() -> None:
    text = _text("deploy.ps1")
    for required in (
        "tools\\env\\preflight.ps1 -Environment $Environment",
        "--validation-level Provider",
        "what-if",
        "--no-pretty-print",
        "tools.env.whatif gate",
        "tools.env.whatif summary",
        "tools.env.costgate bodies",
        "tools.env.costgate check",
        '$deployment = "airrec-$Environment-$Stage"',
        '$parameters = "@.local\\$Environment.parameters.json"',
        "Microsoft.CostManagement/query?api-version=2025-03-01",
        "Microsoft.CostManagement/forecast?api-version=2025-03-01",
    ):
        assert required in text, required
    create = text.index("deployment sub create")
    assert text.index("tools\\env\\preflight.ps1") < text.index("deployment sub validate")
    assert text.index("tools.env.costgate check") < create
    assert text.index("tools.env.whatif gate") < create
    gate = next(line for line in _statements(text) if "tools.env.whatif gate" in line)
    assert "--subscription $subscription" in gate
    deploy_calls = [
        line
        for line in _statements(text)
        if re.search(r"\baz\s+deployment\s+sub\s+(validate|what-if|create)\b", line)
    ]
    assert len(deploy_calls) == 4
    for line in deploy_calls:
        assert "--location $location" in line, line
        assert "--name $deployment" in line, line
        assert "--template-file infra\\main.bicep" in line, line
        assert "--parameters $parameters" in line, line


def test_deploy_has_no_retired_stage_or_switch() -> None:
    text = _text("deploy.ps1")
    for retired in (
        "Fabric",
        "fabric",
        "dev-up",
        "ContainerApps",
        "DeployWorker",
        "ImageTag",
        "ScenarioId",
        "NoDeveloperAccess",
        "egress",
        "overrides",
        "tools\\dev\\",
        "a2-worker",
        "g1-",
        "d38",
    ):
        assert retired not in text, retired


def test_cost_queries_go_through_the_bounded_fetch() -> None:
    """D36: Cost Management throttled the A2 gate for about 15 minutes. Both cost queries go
    through `costgate fetch`, which honours the throttling hints within a bound and writes
    no result file on failure (tested in test_costgate.py), so `costgate check` refuses."""
    text = _text("deploy.ps1")
    statements = _statements(text)
    assert not [
        line for line in statements if "Microsoft.CostManagement/" in line and AZ_CALL.search(line)
    ]
    fetches = [line for line in statements if "tools.env.costgate fetch" in line]
    assert len(fetches) == 2
    for line, kind, body, out in zip(
        fetches,
        ("query", "forecast"),
        ("cost-query.json", "cost-forecast-body.json"),
        ("cost-actual.json", "cost-forecast.json"),
        strict=True,
    ):
        assert line.lstrip().startswith("& $python -m tools.env.costgate fetch "), line
        assert "--subscription $subscription" in line
        assert (
            '--url "https://management.azure.com/subscriptions/$subscription'
            f'/providers/Microsoft.CostManagement/{kind}?api-version=2025-03-01"'
        ) in line
        assert f"--body .local\\deploy\\{body}" in line
        assert f"--out .local\\deploy\\{out}" in line
    gate = text.index("tools.env.costgate check")
    assert text.index("tools.env.costgate bodies") < text.index("tools.env.costgate fetch") < gate
    assert text.rindex("tools.env.costgate fetch") < gate
    assert "'--increment-usd', [string]$IncrementUsd" in text


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(result: subprocess.CompletedProcess[str]) -> str:
    """Both streams without pwsh's colour codes, with its line wrapping undone."""
    return " ".join(ANSI.sub("", result.stdout + result.stderr).split())


def _run_deploy_with_az_shim(
    tmp_path: Path, arguments: list[str]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run deploy.ps1 with an `az` first on PATH that only records the call and fails, so a
    guard that let the script through would show up as a recorded call, not reach Azure."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is required to run deploy.ps1.")
    shim = tmp_path / "shim"
    shim.mkdir()
    marker = tmp_path / "az-called.txt"
    if os.name == "nt":
        (shim / "az.cmd").write_text(f'@echo az>> "{marker}"\r\n@exit /b 1\r\n', encoding="utf-8")
    else:
        script = shim / "az"
        script.write_text(f'#!/bin/sh\necho az >> "{marker}"\nexit 1\n', encoding="utf-8")
        script.chmod(0o755)
    environment = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ENV / "deploy.ps1"),
            "-Stage",
            "platform",
            *arguments,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return result, marker


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        (["-Apply"], "-Apply needs -IncrementUsd"),
        (["-ContainerApps"], "A parameter cannot be found that matches parameter name"),
        (["-FabricStorageAccess", "account-reader"], "A parameter cannot be found"),
    ],
)
def test_deploy_refuses_before_any_az_call(
    tmp_path: Path, arguments: list[str], refusal: str
) -> None:
    """An apply without its cost increment, or a retired switch, stops deploy.ps1 before it
    calls az."""
    result, marker = _run_deploy_with_az_shim(tmp_path, arguments)
    output = _plain(result)
    assert result.returncode != 0, output
    assert refusal in output
    assert not marker.exists()
