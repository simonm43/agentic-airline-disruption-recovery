# Application source

| Package | What it is | Runs as |
|---|---|---|
| `ops_app` | ARROW: intake from Service Bus and typed reports, the Foundry orchestrator and Case Assistant calls, deterministic planning, costing (airline cost, EU261, care), rules and routing, the governed executor, and the operator UI (`static\`). One image, three apps by `APP_ROLE`: `operator` (UI and operator API), `tools` (the agents' MCP tools), `workers` (intake, assessments, the executor; the only writer) | `ca-airrec-ops-dev`, `ca-airrec-tools-dev`, `ca-airrec-worker-dev`; image `airrec-ops-app` |
| `airline_systems` | The five sample airline systems (Schedule, Maintenance, Crew, Booking, Slots) over Azure SQL, each with REST and MCP; `SYSTEM` selects which one a replica serves. Also the day loader | `ca-airrec-<system>-dev` and the two day jobs, image `airrec-systems` |
| `testbench` | The Test Bench: sends disruption events to Service Bus and follows them into ARROW | `ca-airrec-testbench-dev`, image `airrec-testbench` |

Safety and legal limits (crew duty, EU261, approval routing) are enforced in deterministic code,
not in prompts. Everything uses generated data and sample airline systems. Images are built and
deployed as described in [infra](../infra/README.md).
