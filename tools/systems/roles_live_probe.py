"""ARROW's real tokens against the five systems, run inside ARROW's image (one exec).

For each system it calls GET /api/capabilities with the token ARROW's gateway requests
(CloudGateway.systems_scope) and with ARROW's api:// token (the audience form Foundry uses). With
the app-roles image both must be admitted where ARROW holds the read role and refused on
Maintenance (no Maintenance.Read, although ARROW is a listed caller). A token without roles, as
ARROW's old replica cached, is refused everywhere. Prints the roles and status
codes, never a token.
"""

import base64
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from azure.identity import ManagedIdentityCredential

from ops_app.engine import CloudGateway

E = os.environ
app_id = E["SYSTEMS_API_APP_ID"]
credential = ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"])
gateway_scope = CloudGateway(credential, app_id, "", "", "", booking_url="").systems_scope
for scope in (gateway_scope, f"api://{app_id}/.default"):
    token = credential.get_token(scope).token
    part = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    print("TOKEN", scope, "roles", sorted(claims.get("roles") or []))
    for system in ("SCHEDULE", "MAINTENANCE", "CREW", "BOOKING", "SLOTS"):
        headers = {"Authorization": f"Bearer {token}"}
        request = Request(f"{E[system + '_URL']}/api/capabilities", headers=headers)
        try:
            with urlopen(request, timeout=90) as response:
                outcome = str(response.status)
        except HTTPError as error:
            outcome = f"{error.code} {error.read(160).decode(errors='replace')}"
        print("PROBE", system.lower(), outcome)
