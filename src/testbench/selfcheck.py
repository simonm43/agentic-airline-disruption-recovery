"""In-app checks for the deployed test bench, as its own managed identity.

az containerapp exec -n ca-airrec-testbench-dev ... \
    --command "python -m testbench.selfcheck send ar259-20"

send <preset>   publish one preset to the Service Bus topic (Service Bus intake spec §3.5)
event <id>      ARROW's view of the event: queued, received, outcome
"""

from __future__ import annotations

import json
import sys

from testbench.app import build_bench


def main(argv: list[str]) -> int:
    bench = build_bench()
    command = argv[0] if argv else ""
    if command == "send" and len(argv) == 2:
        sent = bench.send(argv[1], None, None)
        print(json.dumps({"event_id": sent["event_id"], "sent_at": sent["sent_at"]}))
    elif command == "event" and len(argv) == 2:
        print(json.dumps(bench.event(argv[1]), default=str)[:3000])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
