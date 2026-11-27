# Tests

The suite tests business workflows, not internal functions. It has four layers:

| Layer | Where |
|---|---|
| Offline end-to-end scenario workflows through the real planner, policy and executor, with scripted agents and a scripted approver (approval, rejection, stale approval, stopped and restarted cases) | `ops_app\` (for example `test_four_cases.py`, `test_orchestrated_flow.py`, `test_execution_workflow.py`), on the in-memory systems in `arrow_scenario.py` and `day_store.py` |
| Evaluations of the Foundry agents in Dev | `tools\ops_app\evaluate_agents.py`; results go to the folder `--out` names |
| API tests at the service boundary | `airline_systems\`, `ops_app\`, `testbench\` |
| One Dev end-to-end smoke | the Test Bench cases: `.\tools\systems\exec_py.ps1 -App ca-airrec-testbench-dev -Script tools\testbench\intake_live.py -Arg cases`, then `-Arg list` and `-Arg reset` |

Beside them sit the deterministic rule tables: approval routing (`ops_app\test_policy_rules.py`),
EU261 and passenger costs (`ops_app\test_passenger_cost_rules.py`) and crew duty limits
(`airline_systems\test_reserve_options.py`). Also offline: document links and ADRs (`docs\`) and
the infrastructure contract (`infra\`).

## Running

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`tests\infra` reads compiled templates: follow the offline contract build in
[infra](../infra/README.md#offline-infrastructure-contract-checks) first, or those tests fail. Each
image build also runs its app's tests, strict mypy and ruff in ACR.

The browser smokes run the real UI against mocked routes (set up [tools\browser](../tools/browser/README.md) first):

```powershell
$server = Start-Process .\.venv\Scripts\python.exe '-m http.server 8768 --bind 127.0.0.1' -PassThru -WindowStyle Hidden
.\tools\ops_app\smoke.ps1
.\tools\testbench\smoke.ps1
Stop-Process -Id $server.Id
```

Model evaluations supplement, and never replace, the deterministic checks.
