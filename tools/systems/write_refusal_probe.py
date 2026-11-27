"""An ARROW app's own identity against each airline system's write route, run inside the operator
app and the tools app (three container apps spec; tools\\systems\\exec_py.ps1). Only the workers
may write, so each system must refuse the call before it reads the body: 403, or no token at all.
The body is empty, which every probed interface refuses (400), so even a wrongly admitted caller
writes nothing. Prints the token's roles and each status, never a token, and exits 1 unless every
route refused it.
"""

import base64
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from azure.identity import ManagedIdentityCredential

E = os.environ
# One write route per system whose interface refuses an empty body.
ROUTES = {
    "SCHEDULE": "/api/delays",
    "MAINTENANCE": "/api/defects",
    "CREW": "/api/standby-alerts/release",
    "BOOKING": "/api/passenger-notices",
    "SLOTS": "/nm/messages",
}
credential = ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"])
try:
    token = credential.get_token(f"{E['SYSTEMS_API_APP_ID']}/.default").token
except Exception as error:
    print("TOKEN refused", str(error)[:200])
    print("REFUSED", len(ROUTES), "of", len(ROUTES))
    raise SystemExit(0) from None
part = token.split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
print("TOKEN oid", claims.get("oid"), "roles", sorted(claims.get("roles") or []))
refused = 0
for system, path in ROUTES.items():
    request = Request(
        f"{E[system + '_URL']}{path}",
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=90) as response:
            outcome = str(response.status)
    except HTTPError as error:
        outcome = f"{error.code} {error.read(160).decode(errors='replace')}"
    refused += outcome.startswith("403")
    print("WRITE", system.lower(), path, outcome)
print("REFUSED", refused, "of", len(ROUTES))
raise SystemExit(0 if refused == len(ROUTES) else 1)
