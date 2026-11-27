# ADR 0026: Foundry prompt agents act only through ARROW's typed MCP tools

## Status

Accepted (D87; extended by D94, D115, D118, D125 and D127). Supersedes
ADR 0010 and
ADR 0016. Replaces the retired in-code workflow
of ADR 0001.

## Context

The case asks for agents that work out a recovery together, while legality, cost and approval
must be decided by deterministic code outside the model. The first-phase design ran local Agent
Framework agents with in-process tools inside a workflow (ADRs 0001, 0010) and always ranked four
deterministic plans A–D (ADR 0016). The owner then reset the approach towards registered
Foundry agents and a simple happy path (D84, D85); the first-phase platform was later deleted
(D111).

## Decision

- Seven Foundry prompt agents, each with its own Entra agent identity: the recovery orchestrator,
  the Case Assistant and five domain agents (detection, aircraft, crew, passenger, slots).
  Definitions are in `infra/ops-app/*.json`.
- Each domain agent reads one sample system through that system's MCP tools and writes nothing.
- The orchestrator asks the domain agents over A2A v1.0 and acts only through ARROW's typed MCP
  tools: `record_disruption`, `record_case`, `get_playbook`, `build_plan(levers)` and
  `submit_plan(plan_id, reason)`. Its final answer is a strict JSON schema that code checks.
- The model names levers; code builds each plan from the options the systems offered, prices it,
  checks it and routes it (ADR 0030). A lever the systems did not offer builds no plan; at most four
  plans a case. Per-booking rebooking is computed by Booking, never proposed by a model.
- Code decides which agent may call which tool, refuses a call from another case's run (D115) and
  gives the Case Assistant only `read_case`, `what_if` and `get_playbook` (D118).

## Alternatives rejected

- **The in-code Agent Framework workflow with local agents (ADRs 0001, 0010):** retired with the
  first-phase platform; the owner asked for registered Foundry agents.
- **Agents calling write APIs:** execution before the whole plan is checked, and no single place
  that enforces authority.
- **Four deterministic plans A–D always ranked (ADR 0016):** replaced by plans built in the OCC
  escalation order (delay, swap, cancel) from the options Schedule offers. Code still builds every
  plan; the model only chooses among offered levers.
- **A hosted (container) orchestrator agent:** used briefly and deleted (D90, D111);
  a prompt agent needs no image or hosting.

## Consequences

- A model mistake reaches only checked tools; a refused value or lever comes back as a reason the
  orchestrator can act on.
- The orchestrator's recommendation can differ between runs of the same case; the plans' costs,
  checks and routes do not (D87). OCC decides whenever a plan is not automatic.
- Every agent and tool call is in the case's trace, and Foundry's judges score the recorded turns
  (D116). A pass is AI quality, not decision quality.
- ARROW depends on Foundry Agent Service prompt agents, A2A and MCP connections that authenticate
  with agent identities.

## Evidence

- Decisions D87, D94, D115, D118, D125 and D127 in the decision log.
- Orchestrated recovery spec;
  first evaluation;
  agent evaluation.
- Code: [`src/ops_app/app.py`](../../../src/ops_app/app.py) (the MCP tools and who may call them),
  [`src/ops_app/tools.py`](../../../src/ops_app/tools.py);
  [orchestrator definition](../../../infra/ops-app/orchestrator-agent.json).
- [Connect to an A2A agent endpoint from Foundry Agent Service](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent)
- [Connect agents to MCP server endpoints](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)
