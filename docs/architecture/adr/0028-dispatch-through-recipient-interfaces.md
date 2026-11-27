# ADR 0028: The governed executor sends each action through its recipient's own interface

## Status

Accepted (D117). Supersedes ADR 0006.

## Context

ADR 0006 put an authority gate inside the first-phase workflow and wrote one batch outbox row per
plan; that workflow was later deleted (D111). Until this decision ARROW then wrote every action
to one generic `POST /api/actions` on the sample systems. The owner pointed out that real
vendors "wouldn't add endpoints": ARROW must decide, then reach each recipient through the
interface it already has.

## Decision

- ARROW's code decides whether a plan may run: automatic within every limit, or approved by OCC
  (ADR 0030). The approval is bound to the plan's hash and the case version OCC reviewed.
- The executor in the ARROW app re-reads what the plan depends on, then sends each action, in
  order, to its recipient's own interface (`src/ops_app/dispatch.py`): operations control
  (Schedule, `PATCH /api/flights/{flight}`), crew system (Crew), passenger system and ground
  handling (Booking), EUROCONTROL Network Manager, Lisbon airport A-CDM and the slot coordinator
  (Slots).
- Writes to the airline's own systems carry an `Idempotency-Key` and, where the system supports it,
  `If-Match` on the version ARROW validated; they are re-sent under the same key after a failure.
  Messages to outside parties are sent once; an unclear answer goes to OCC.
- ARROW keeps the ledger in SQL: each call, its reply and their order. A refusal stops what depends
  on it; nothing is rolled back across systems automatically. A lease in SQL keeps one executor per
  case, and a supervisor resumes an interrupted execution without repeating a confirmed call.
- Only ARROW's identity with `Actions.Write` may call these write routes (ADR 0027).

## Alternatives rejected

- **One generic actions endpoint on every system:** a real vendor would not add it (the
  owner).
- **Service Bus for execution:** a second store that can disagree with SQL about what ran; Service
  Bus carries only intake and wake-ups (ADR 0025).
- **A separate action service:** no extra isolation while every sample system runs in one Dev
  environment; it remains a production option behind the same `dispatch.py` seam.
- **Agents calling the write interfaces:** no single enforcement point, and execution before the
  whole plan is checked (ADR 0026).

## Consequences

- Each recipient's record and ARROW's match by `X-Request-Id`; OCC sees one row per call, named by
  its recipient.
- A retime stopped part-way says which flights moved, which was refused and which were not sent.
- In production each recipient becomes an adapter behind `dispatch.py`; the vendor interfaces named
  in the knowledge base illustrate the market, not the case airline's systems.
- The executor and the planner share one process and one trust zone, accepted for Dev and
  disclosed.

## Evidence

- Decision D117 in the decision log;
  native dispatch spec;
  live evidence.
- Code: [`src/ops_app/executor.py`](../../../src/ops_app/executor.py),
  [`src/ops_app/dispatch.py`](../../../src/ops_app/dispatch.py),
  [`src/airline_systems/app.py`](../../../src/airline_systems/app.py) (the recipients' routes).
- Airline operations knowledge base
  for the real interfaces each sample system represents.
