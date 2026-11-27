"""One load-test run's cases as ARROW holds them, read inside ARROW (read-only):
tools/systems/exec_py.ps1 -Environment <env> -App ca-airrec-ops-<env> -Script tools/load/report.py
-Arg <run>. Prints one ROW line per case (id, event id, kind, event time, enqueued, opened, first
assessment step, decision: plan submitted or the case ended, attempts, retries, status and stage)
and then ROWS with the count. tools/load/summarize.py turns the output into the run's figures."""

import json
import os
import sys

from azure.identity import ManagedIdentityCredential

from airline_systems.sql import SqlScheduleStore

E = os.environ
ENDS = {
    "plan_submitted",
    "assessment_completed",
    "assessment_failed",
    "assessment_stopped",
    "orchestrator_no_submission",
    "manual_intervention_required",
}
sql = SqlScheduleStore(
    E["SQL_SERVER"],
    E["SQL_DATABASE"],
    None,
    "ops",
    ManagedIdentityCredential(client_id=E["AZURE_CLIENT_ID"]),
)
with sql.connect() as c:
    cases = c.execute(
        "SELECT trace_id, document FROM ops.recovery_case"
        " WHERE JSON_VALUE(document, '$.source.origin') = ?",
        "arrow-load/" + sys.argv[1],
    ).fetchall()
    for trace, raw in cases:
        d = json.loads(raw)
        log = [
            (str(a), e, q)
            for a, e, q in c.execute(
                "SELECT at, JSON_VALUE(document, '$.event'), JSON_VALUE(document, '$.enqueued_at')"
                " FROM ops.decision_log WHERE trace_id = ? ORDER BY sequence",
                trace,
            ).fetchall()
        ]
        step = [a for a, e, _ in log if e == "assessment_progress"]
        end = [a for a, e, _ in log if e in ENDS]
        print(
            "ROW "
            + json.dumps(
                [
                    d["display_id"],
                    d["source"]["event_id"],
                    d["disruption"]["type"],
                    d["source"]["event_time"],
                    next((q for _, e, q in log if e == "event_received"), None),
                    d["created_at"],
                    step[0] if step else None,
                    end[0] if end else None,
                    (d.get("assessment") or {}).get("attempt"),
                    sum(e == "assessment_retry_scheduled" for _, e, _ in log),
                    d["status"],
                    str(d.get("stage"))[:90],
                ]
            ),
            flush=True,
        )
print("ROWS", len(cases))
