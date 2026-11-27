# ADR 0030: Plans are routed by a rule table in code, with editable limits under four-eyes

## Status

Accepted (D87; editable limits D96 and D97). Supersedes
ADR 0007.

## Context

Whether a plan runs automatically, waits for OCC or is blocked depends on its parameters and the
state of the operation (crew limits, Lisbon capacity, cost, rebookings, standby crews left), not on
which agent proposed it. ADR 0007 kept that policy as a YAML matrix in the first-phase service with
Agent Framework middleware as a second line; that service was later deleted (D111). The
owner wanted the airline's autonomy limits editable, but legal and safety rules fixed (D96), and
any change approved by a second person (D97).

## Decision

- A deterministic rule table in code (`src/ops_app/policy.py`) routes every plan: **automatic**
  within every limit (ARROW carries it out and OCC is informed), **OCC decides**, or **blocked**.
  Aircraft changes, cancellations, storm plans, EU261 care thresholds, high cost, many rebookings,
  airport requests and more than two standby crews go to OCC. A crew limit no standby crew covers,
  a failed Lisbon capacity check, or an unknown or contradictory check blocks the plan.
- Unknown is never clearance: a missing check blocks; a missing business figure (cost, distance,
  standby crews left) sends the plan to OCC.
- Only two limits are editable: the cost limit for automatic plans (€20,000) and the rebooking
  limit (10). They are versioned in SQL; one duty manager proposes, a different one approves, and
  both the API and a database constraint refuse self-approval. Each plan keeps the rule version it
  was built with.
- The model never routes: `submit_plan` looks up the stored plan and the route code gave it.
  The sample systems are the second line: they refuse a write without ARROW's `Actions.Write`
  role (ADR 0027).

## Alternatives rejected

- **Prompt instructions as policy:** not enforcement.
- **Editable legal and safety rules:** owner chose limits only (D96).
- **A YAML matrix in a separate service (ADR 0007):** retired with the first-phase platform; the
  rule table now lives beside the planner it governs.
- **Agent Framework middleware as a second line:** no Agent Framework runtime remains; the systems'
  role checks and the executor's source re-check take that place.

## Consequences

- The rule table is covered by table-driven tests and the offline business workflows.
- A limit change is audited; a plan keeps the limits it was built with.
- Passengers beyond the rebooking limit go to Passenger Services after OCC approves.

## Evidence

- Decisions D87, D96, D97 and D88 in the decision log;
  control tower and rules spec.
- Code: [`src/ops_app/policy.py`](../../../src/ops_app/policy.py),
  [`src/ops_app/rules.py`](../../../src/ops_app/rules.py);
  [SQL 012](../../../infra/systems/sql/012_policy_change.sql).
