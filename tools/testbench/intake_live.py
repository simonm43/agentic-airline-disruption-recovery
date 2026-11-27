"""Live check of the Service Bus intake (Service Bus intake spec §4), run inside an environment's
Test Bench as its own identity: tools/systems/exec_py.ps1 -Environment <env>
-App ca-airrec-testbench-<env> -Script <this> -Arg <mode>.

burst   publish seven disruption events at once: the five demo cases plus two more free-text
        delays (an owner test sent six at once and four cases stopped)
cases   publish the five demo cases at once (Case 1-5, as the Test Bench offers them)
<key>   publish one demo case by its preset key (ar259-20, ar444-70, cstqa-aog, ar689-60,
        lppt-taf)
list    today's events as the Test Bench lists them: sent, queued, case, outcome
reset   the Test Bench Reset, waited for: close ARROW's cases, reload the day
"""

import json
import sys

from testbench.app import build_bench

KEYS = ("ar259-20", "ar444-70", "cstqa-aog", "ar689-60", "lppt-taf")
UPDATES = ("storm-confirmed", "storm-dropped")
MORE = (
    "Ground staff shortage at the gate: AR805 will leave about 30 minutes late.",
    "Late inbound crew bus: AR882 will leave about 25 minutes late.",
)
bench = build_bench()
if sys.argv[1] in KEYS + UPDATES:
    sent = [bench.send(sys.argv[1], None, None)]
    print("SENT", json.dumps([[item["event_id"], item["sent_at"]] for item in sent]))
elif sys.argv[1] in ("burst", "cases"):
    sent = [bench.send(key, None, None) for key in KEYS]
    for text in MORE if sys.argv[1] == "burst" else ():
        sent.append(bench.send(None, "message", {"text": text, "system": "Ground handling"}))
    print("SENT", json.dumps([[item["event_id"], item["sent_at"]] for item in sent]))
elif sys.argv[1] == "reset":
    bench._spawn = lambda work: work()  # run the reset here and wait for it
    bench.reset()
    print("RESET", json.dumps(bench.reset_state()))
else:
    rows = bench.events(bench.today())
    keys = ("sent_at", "label", "queued_after_s", "case", "case_after_s", "outcome", "reason")
    print("EVENTS", json.dumps([{key: row.get(key) for key in keys} for row in rows]))
