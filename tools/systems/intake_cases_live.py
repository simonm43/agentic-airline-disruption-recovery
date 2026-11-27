"""Read-only view of today's event cases for the Service Bus intake check, run in ARROW:
tools/systems/exec_py.ps1 -Environment <env> -App ca-airrec-ops-<env> -Script <this>. Prints, per
case, when it was opened, when its assessment started and ended, retries, and how it ended."""

import json
import os

from azure.identity import ManagedIdentityCredential

from airline_systems.schedule import lisbon_today
from airline_systems.sql import SqlScheduleStore

E = os.environ
sql = SqlScheduleStore(
    E["SQL_SERVER"],
    E["SQL_DATABASE"],
    None,
    "ops",
    ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"]),
)
with sql.connect() as conn:
    cases = conn.execute(
        "SELECT trace_id, document FROM ops.recovery_case WHERE operating_date = ?"
        " AND event_id IS NOT NULL ORDER BY created_at",
        lisbon_today().isoformat(),
    ).fetchall()
    out = []
    for trace, raw in cases:
        doc = json.loads(raw)
        logs = conn.execute(
            "SELECT at, JSON_VALUE(document, '$.event'), JSON_VALUE(document, '$.message')"
            " FROM ops.decision_log WHERE trace_id = ? ORDER BY sequence",
            trace,
        ).fetchall()
        starts = [
            str(at) for at, event, message in logs if message and "orchestrator is" in message
        ]
        out.append(
            {
                "id": doc["display_id"],
                "opened": doc["created_at"][11:19],
                "first_step": str(logs[1][0])[11:19] if len(logs) > 1 else None,
                "orchestrator": [s[11:19] for s in starts],
                "last": str(logs[-1][0])[11:19],
                "retries": sum(1 for _, event, _ in logs if event == "assessment_retry_scheduled"),
                "status": doc["status"],
                "stage": str(doc.get("stage"))[:90],
            }
        )
print("CASES", json.dumps(out))
