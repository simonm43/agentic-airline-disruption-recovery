"""The environment definitions name every resource the way Azure does, so Dev keeps its names and
a new environment gets its own (tools/env/environment.py, infra/environments)."""

from __future__ import annotations

import re

from tools.env import environment

# Read from the live Dev subscription (az resource list, the Foundry portal link).
LIVE_DEV = {
    "resourceGroup": "rg-airrec-dev-swc",
    "vnet": "vnet-airrec-dev-swc",
    "privateEndpointNsg": "vnet-airrec-dev-swc-snet-private-endpoints-nsg-swedencentral",
    "acaNsg": "nsg-airrec-aca-dev-swc",
    "sqlServer": "sql-airrec-dev-bslgt7jwkw2zu",
    "sqlPrivateEndpoint": "pe-airrec-sql-dev-swc",
    "registry": "crairrecdevbslgt7jwkw2zu",
    "logAnalytics": "log-airrec-dev-swc",
    "appInsights": "appi-airrec-dev-swc",
    "containerEnvironment": "cae-airrec-dev-swc",
    "serviceBus": "sb-airrec-dev-bslgt7jwkw2zu",
    "foundryAccount": "aif-airrec-chat-dev-4w7wuej5i2a5m",
    "foundryProject": "proj-airrec-chat-dev",
    "foundryPortalUrl": (
        "https://ai.azure.com/nextgen/r/AAAAAAAAQACAAAAAAAAAEw,rg-airrec-dev-swc,,"
        "aif-airrec-chat-dev-4w7wuej5i2a5m,proj-airrec-chat-dev"
    ),
    "dayLoadJob": "caj-airrec-dayload-dev",
    "dayResetJob": "caj-airrec-dayreset-dev",
    "app.ops": "ca-airrec-ops-dev",
    "app.testbench": "ca-airrec-testbench-dev",
    "identity.systems": "id-airrec-systems-dev",
    "testbenchRole": "Airrec test bench operator (Dev)",
}


def test_dev_keeps_every_live_name() -> None:
    names = environment.load("dev").names()
    assert {key: names[key] for key in LIVE_DEV} == LIVE_DEV


def test_a_new_environment_shares_no_name_with_dev() -> None:
    dev, prd = environment.load("dev"), environment.load("prd")
    dev_names, prd_names = dev.names(), prd.names()
    shared = {"location", "tenantId", "database"}
    for key, value in prd_names.items():
        if key in shared:
            continue
        assert value != dev_names[key], key
        assert "dev" not in re.split(r"[-./,() ]", value.casefold()), key
    assert prd.developer_vnet_id == ""
    assert prd.sql_administrator == "identity"
    # Resource names stay generic: the product name is for the user interface only.
    assert "arrow" not in " ".join(prd_names.values()).casefold()
