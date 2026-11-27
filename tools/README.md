# Developer tooling

Developer-only scripts, kept apart from the application code. Run them from the repository root
with the locked environment (`.\.venv\Scripts\python.exe`) unless a line says otherwise. Scripts
that touch Azure take the environment (`dev`, `prd`: `infra\environments\<name>.json`) and target
its subscription explicitly; see the [deployment guide](../infra/README.md) for the order, the
gates and the approval rules.

| Folder | Tool | What it does and how to run it |
|---|---|---|
| `browser` | Playwright CLI | Pinned browser automation for checks and screen captures; see [browser](browser/README.md) |
| `checks` | `requirements.txt` | Hash-locked mypy and ruff that the image builds install |
| `env` | `environment.py` | Every resource name of an environment, from its definition: `python -m tools.env.environment <env>` |
| `env` | `preflight.ps1` | Live, read-only preflight for `infra\main.bicep`: `-Environment <env>`; writes `.local\<env>.parameters.json` |
| `env` | `deploy.ps1` | Preview, and with `-Apply -IncrementUsd <usd>` deploy, `infra\main.bicep` behind the gates: `.\tools\env\deploy.ps1 -Environment <env> -Stage <name>` |
| `env` | `deploy_group.py` | Preview, and with `--apply --increment-usd <usd>` deploy, one resource-group template with the environment's looked-up values: `python -m tools.env.deploy_group <env> <key>` |
| `env` | `entra_apps.py` | The environment's two app registrations: `python -m tools.env.entra_apps <env> [--apply]` |
| `env` | `publish_agents.py` | Publish the seven Foundry agents in order; ARROW's tools are on the tools app: `python -m tools.env.publish_agents <env> [--agent <name>] [--apply]` |
| `env` | `whatif.py`, `unsupported-ack.json` | The what-if gate: `python -m tools.env.whatif summary <file>`, `gate <preview> <current>` and `same <a> <b>` |
| `env` | `costgate.py` | The cost gate against the environment's budget (`bodies`, `fetch`, `check`); the deployers run it |
| `load` | `scenario.py`, `send.py`, `report.py`, `summarize.py` | The load test: a 900-flight day's disruption reports sent to the topic (`python -m tools.load.send <env> day\|burst [--apply]`), read back inside ARROW (`report.py` through `exec_py.ps1`) and summarised with the model metrics (`python -m tools.load.summarize <env> <run>`) |
| `ops_app` | `build.ps1` | Build the ARROW image in ACR (tests, mypy, ruff inside): `-Environment <env> -ImageTag <tag>` |
| `ops_app` | `smoke.ps1` | Operator browser smokes against mocked routes; start `python -m http.server 8768 --bind 127.0.0.1` first |
| `ops_app` | `evaluate_agents.py` | Foundry evaluation of the agents' recorded runs: `run --since <UTC time> --out <dir>`, then `results --out <dir>` |
| `ops_app` | `find_cases.py` | Find the demo cases on today's generated day: `$env:PYTHONPATH='src;tests;tests/ops_app'` first |
| `ops_app` | `make_land.py` | Regenerate the map outline `src\ops_app\static\land.js` from Natural Earth: `make_land.py <ne_110m_land.geojson>` |
| `systems` | `build.ps1`, `lock.ps1` | Build the sample systems image in ACR (`-Environment <env>`); regenerate `src\airline_systems\requirements.txt` in ACR |
| `systems` | `migrate.py` | The database migrations, run inside the environment by the job `caj-airrec-dbmigrate-<env>`; `--plan` lists them offline |
| `systems` | `apply_sql.py` | Dev only, from the developer VM: apply one migration as the SQL Entra admin: `apply_sql.py infra\systems\sql\<file>.sql [Name=<guid>]` |
| `systems` | `load_day.py` | Dev only: replace today's operating day from the developer VM: `$env:PYTHONPATH='src'` first |
| `systems` | `app_roles.py` | The systems' Entra app roles for ARROW and the agents: `python -m tools.systems.app_roles --environment <env> [--apply]` |
| `systems` | `exec_py.ps1` | Run a Python file inside an app: `-Environment <env> -App <app> -Script <file> [-Arg <word>]` |
| `systems` | `intake_cases_live.py`, `roles_live_probe.py`, `roles_live_case.py` | Live checks run through `exec_py.ps1` in ARROW: today's event cases (read only, the operator app `ca-airrec-ops-<env>`); the app's tokens against the five systems (any ARROW app); one QA case (`-Arg aog` or `auto`; only in the workers, `ca-airrec-worker-<env>`, the one writer) |
| `systems` | `write_refusal_probe.py` | Live check that the operator app's and the tools app's identities cannot write: through `exec_py.ps1` in `ca-airrec-ops-<env>` and `ca-airrec-tools-<env>`; every system answers 403, an empty body writes nothing |
| `testbench` | `build.ps1`, `lock.ps1` | Build the Test Bench image in ACR (`-Environment <env>`); regenerate `src\testbench\requirements.txt` |
| `testbench` | `configure_access.py` | Test Bench sign-in and access in ARROW: `python -m tools.testbench.configure_access --environment <env> [--apply]` |
| `testbench` | `smoke.ps1` | Test Bench browser smoke against mocked routes (same local server as above) |
| `testbench` | `intake_live.py` | Live Service Bus intake check in the Test Bench (`ca-airrec-testbench-<env>`) through `exec_py.ps1`: `-Arg cases`, `burst`, `<preset>`, `list` or `reset` |

`.github\mcp.json` configures the public, read-only Microsoft Learn MCP server. It needs no
credentials; never send private documents or case material to it.
