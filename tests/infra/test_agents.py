"""Publishing the agents (tools/env/publish_agents.py) renders Dev's committed definitions for an
environment: for Dev it reproduces them exactly, so Dev's agents stay unchanged; for any other
environment no Dev address remains."""

from __future__ import annotations

import json

import pytest

from tools.env import publish_agents
from tools.env.environment import load

# The committed definitions were written against Dev's apps' domain.
DEV_DOMAIN = "exampledev-00000000.swedencentral.azurecontainerapps.io"


@pytest.mark.parametrize("agent", sorted(publish_agents.FILES))
def test_rendering_for_dev_reproduces_the_committed_definition(agent: str) -> None:
    committed = publish_agents.definition(agent)
    assert publish_agents.render(committed, load("dev").names(), DEV_DOMAIN) == committed


@pytest.mark.parametrize("agent", sorted(publish_agents.FILES))
def test_rendering_for_another_environment_leaves_no_dev_address(agent: str) -> None:
    prd = load("prd").names()
    text = json.dumps(
        publish_agents.render(
            publish_agents.definition(agent), prd, "example.azurecontainerapps.io"
        )
    )
    assert DEV_DOMAIN not in text
    assert load("dev").names()["foundryAccount"] not in text
    assert "-dev." not in text and "-dev/" not in text
    assert "arrow-tools" not in text


def test_a_changed_definition_gets_a_new_version_and_a_domain_agent_its_card() -> None:
    body = publish_agents.definition("crew-recovery-agent")
    card = publish_agents.cards()["crew-recovery-agent"]
    live = {
        "versions": {"latest": {"version": "6", **body}},
        "agent_card": card,
        "agent_endpoint": {"protocol_configuration": publish_agents.A2A},
    }
    assert publish_agents.decide("crew-recovery-agent", body, live, card) == []
    changed = {**body, "description": "edited"}
    assert publish_agents.decide("crew-recovery-agent", changed, live, card) == ["new version"]
    assert publish_agents.decide("crew-recovery-agent", body, None, card) == [
        "create",
        "agent card and A2A",
    ]
