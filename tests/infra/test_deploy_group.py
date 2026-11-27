"""The resource-group deployer (tools/env/deploy_group.py) deploys the approved body again only
when a pull role granted in the same deployment has not propagated yet; any other failure stops."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from tools.env import deploy_group
from tools.env.rest import Rest

PATH = "/subscriptions/s/resourceGroups/g/providers/Microsoft.Resources/deployments/d"
PULL = "unable to pull image using Managed identity /subscriptions/s/.../id-x for registry r"


def _deployer(*outcomes: str) -> tuple[Rest, list[str], list[float]]:
    """A deployment API whose runs end with ``outcomes`` in turn (a message, or Succeeded)."""
    calls: list[str] = []
    runs = iter(outcomes)
    current: dict[str, str] = {}

    def send(
        method: str, url: str, headers: Mapping[str, str], data: bytes | None
    ) -> tuple[int, Mapping[str, str], bytes]:
        calls.append(method)
        if method == "PUT":
            current["outcome"] = next(runs)
            return 201, {}, b"{}"
        outcome = current["outcome"]
        state = "Succeeded" if outcome == "Succeeded" else "Failed"
        error = {} if state == "Succeeded" else {"code": "DeploymentFailed", "message": outcome}
        body = {"properties": {"provisioningState": state, "error": error, "outputs": {}}}
        return 200, {}, json.dumps(body).encode()

    sleeps: list[float] = []
    return Rest("t", send=send, token=lambda scope: "token"), calls, sleeps


def test_a_pull_role_still_propagating_is_deployed_again_and_nothing_else_is() -> None:
    rest, calls, sleeps = _deployer(PULL, "Succeeded")
    result = deploy_group._create(rest, PATH, {"properties": {}}, sleep=sleeps.append)
    assert result["properties"]["provisioningState"] == "Succeeded"
    assert calls == ["PUT", "GET", "PUT", "GET"]
    assert deploy_group.PROPAGATION_WAIT_SECONDS in sleeps

    rest, calls, sleeps = _deployer("InvalidTemplate: something else")
    with pytest.raises(SystemExit, match="something else"):
        deploy_group._create(rest, PATH, {"properties": {}}, sleep=sleeps.append)
    assert calls == ["PUT", "GET"]
