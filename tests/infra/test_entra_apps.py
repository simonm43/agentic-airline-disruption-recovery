"""The app registrations (tools/env/entra_apps.py): a re-run patches only what differs and adds
redirect URIs without dropping ones added since, so an existing environment keeps its sign-in."""

from __future__ import annotations

from tools.env import entra_apps
from tools.env.environment import load


def test_only_differences_are_patched_and_redirect_uris_are_only_added() -> None:
    wanted = entra_apps.desired(load("prd"), "example.azurecontainerapps.io")["arrow"]
    kept = "https://localhost:8443/.auth/login/aad/callback"
    found = {
        **wanted,
        "web": {
            "redirectUris": [kept, wanted["web"]["redirectUris"][0]],
            "implicitGrantSettings": wanted["web"]["implicitGrantSettings"],
        },
    }
    assert entra_apps.differences(found, wanted) == {
        "web": {"redirectUris": [kept, *wanted["web"]["redirectUris"]]}
    }
    found["web"]["redirectUris"] = [kept, *wanted["web"]["redirectUris"]]
    assert entra_apps.differences(found, wanted) == {}
    found["api"] = {"requestedAccessTokenVersion": None, "oauth2PermissionScopes": []}
    assert entra_apps.differences(found, wanted) == {"api": {"requestedAccessTokenVersion": 2}}
