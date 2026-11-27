"""Read-only dump of the Test Bench cases' saved trails, run inside ARROW:
tools/systems/exec_py.ps1 -Environment <env> -App ca-airrec-ops-<env> -Script <this>
-Arg <event id>_<event id>_... (the ids intake_live.py printed as SENT).

For each case: its stored document with its decision log as ``events`` (as ARROW's store loads it)
and every airline-system call the systems audited under its trace (audit.system_call). Printed
zlib-compressed and base64-encoded in ``D`` lines, then ``N <cases> <characters>``;
tools/testbench/check_cases.py rebuilds each trail with ARROW's own code and checks it."""

import base64
import json
import os
import sys
import zlib

from azure.identity import ManagedIdentityCredential

from airline_systems.sql import SqlScheduleStore

E = os.environ
ids = sys.argv[1].split("_")
sql = SqlScheduleStore(
    E["SQL_SERVER"],
    E["SQL_DATABASE"],
    None,
    "ops",
    ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"]),
)
keys = ("at", "system", "operation", "caller_oid", "outcome", "detail", "caller_name")
keys += ("endpoint", "request", "response")
out = []
with sql.connect() as c:
    marks = ",".join("?" * len(ids))
    cases = c.execute(
        f"SELECT trace_id, document FROM ops.recovery_case WHERE event_id IN ({marks})", *ids
    ).fetchall()
    for trace, raw in cases:
        doc = json.loads(raw)
        doc["events"] = [
            {**json.loads(d), "sequence": s, "at": a.isoformat() + "Z", "trace_id": trace}
            for s, a, d in c.execute(
                "SELECT sequence, at, document FROM ops.decision_log WHERE trace_id = ?"
                " ORDER BY sequence",
                trace,
            ).fetchall()
        ]
        calls = c.execute(
            "SELECT called_at, system_name, operation, caller_oid, outcome, detail, caller_name,"
            " endpoint, request_json, response_json FROM audit.system_call WHERE trace_id = ?"
            " ORDER BY call_id",
            trace,
        ).fetchall()
        rows = [dict(zip(keys, [r[0].isoformat() + "Z", *r[1:]], strict=True)) for r in calls]
        out.append({"trace": trace, "document": doc, "calls": rows})
text = base64.b64encode(zlib.compress(json.dumps(out, default=str).encode(), 9)).decode()
for start in range(0, len(text), 4000):
    print("D", text[start : start + 4000], flush=True)
print("N", len(out), len(text))
