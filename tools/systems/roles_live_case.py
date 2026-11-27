"""One QA case through the real orchestrator and agents, run in ARROW's image (one exec) in the
workers app (ca-airrec-worker-<env>), the only ARROW app whose identity may write.

aog: an aircraft defect; OCC decides, so nothing executes. auto: a staff-shortage delay that ARROW
executes; reload the generated day afterwards. Prints who called which system with what outcome
(audit.system_call, by caller object id), then closes the QA case like the Test Bench reset.

    python roles_live_case.py aog|auto
"""

import json
import os
import sys
import time

from azure.identity import ManagedIdentityCredential

from airline_systems.sql import SqlScheduleStore
from ops_app.engine import CloudGateway, Operations
from ops_app.rules import SqlRules
from ops_app.store import SqlCaseStore

E = os.environ
ARROW = E["OPS_PRINCIPAL_ID"]
REPORTS = {
    "aog": {
        "type": "aircraft_defect",
        "aircraft_reg": "CS-TQA",
        "description": "QA-ROLES check: hydraulic leak, maintenance estimate four hours",
        "unavailable_minutes": 240,
    },
    "auto": {
        "type": "flight_delay",
        "flight_no": "AR259",
        "delay_minutes": 20,
        "cause": "staff_shortage",
        "description": "QA-ROLES check: ground staff shortage",
    },
}
credential = ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"])
sql = SqlScheduleStore(E["SQL_SERVER"], E["SQL_DATABASE"], None, "ops", credential)
store = SqlCaseStore(sql.connect)
gateway = CloudGateway(
    credential,
    E["SYSTEMS_API_APP_ID"],
    E["SCHEDULE_URL"],
    E["MAINTENANCE_URL"],
    E["FOUNDRY_PROJECT_ENDPOINT"],
    booking_url=E["BOOKING_URL"],
    crew_url=E["CREW_URL"],
    slots_url=E["SLOTS_URL"],
)
operations = Operations(store, gateway, SqlRules(sql.connect))
trace = operations.open(REPORTS[sys.argv[1]], ARROW)["trace_id"]
operations.run(trace)
deadline = time.monotonic() + 120
document = store.get(trace)
while (document.get("execution") or {}).get("status") in {
    "approved",
    "executing",
    "pending_external",
} and time.monotonic() < deadline:
    time.sleep(3)
    document = store.get(trace)
with sql.connect() as connection:
    rows = connection.execute(
        "SELECT system_name, operation, LOWER(CONVERT(char(36), caller_oid)), outcome, COUNT(*)"
        " FROM audit.system_call WHERE trace_id = ?"
        " GROUP BY system_name, operation, caller_oid, outcome",
        trace,
    ).fetchall()
event = {"actor": "qa", "autonomy": "system", "event": "qa_closed", "message": "QA-ROLES check"}
record = {
    "display_id": document.get("display_id"),
    "trace_id": trace,
    "status": document.get("status"),
    "stage": document.get("stage"),
    "plans": len(document.get("options") or []),
    "execution": {
        a["kind"]: a["status"] for a in (document.get("execution") or {}).get("actions") or []
    },
    "calls": sorted(f"{r[0]} {r[1]} {r[2]} {r[3]} x{r[4]}" for r in rows),
    "closed": store.close(trace, event),
}
print("CASE", json.dumps(record))
