# ADR 0032: Two pinned regional models, chosen per agent from Dev runs; no model router

## Status

Accepted (orchestrator on gpt-4.1, D87; capacity D100; Case Assistant D118).
Supersedes
ADR 0019.

## Context

ADR 0019 planned to pick a model per role from six candidates against an evaluation bar fixed in
advance, deployed as Data Zone Standard (EU). That evaluation harness belonged to the first-phase
platform, which was retired (D111). The reset chose models from runs of the real Foundry agents in
Dev instead.

## Decision

- Two model deployments in the Foundry account, Sweden Central, both Standard (regional), pinned
  to version 2025-04-14 with no automatic upgrade:
  - `gpt-4.1-eval` (gpt-4.1, 500 units, `infra/ops-app/model-deployment.bicep`): the
    orchestrator, the Detection agent, the Case Assistant and Foundry's evaluation judge;
  - `gpt-4.1-mini` (600 units, `infra/foundry/isolated.bicep`): the Aircraft, Crew, Passenger and
    Slot agents.
- The orchestrator runs at temperature 0. No model router.

## Alternatives rejected

- **gpt-4.1-mini for Detection:** 10 of 15 known regression cases right, against 15 of 15 for
  gpt-4.1 with the same instructions; a prompt-optimised mini also scored 10 of 15.
- **gpt-4.1-mini for the Case Assistant:** it sent malformed A2A messages in the live run (D118).
- **A model router:** per-run cost and quality would not be reproducible.
- **The six-candidate evaluation of ADR 0019:** not carried into the reset; Foundry's judges now
  score the agents' recorded turns instead (D116).

## Consequences

- gpt-4.1 costs about five times gpt-4.1-mini per Detection call (USD 0.081 against 0.016 for the
  same fifteen cases).
- One deployment serves the orchestrator, Detection, the Case Assistant and the judge. Its capacity
  was raised from 60 to 500 units after six simultaneous events hit HTTP 429 (D100); the queue now
  limits ARROW to two assessments at once (ADR 0025).
- Regional Standard deployments process prompts and responses within the Azure geography of
  Sweden Central, possibly across its regions (Learn, deployment types).
- Pinned versions mean a model retirement needs a planned change and a re-run of the evaluation.
- Safety does not depend on the model: code checks legality, cost and routing (ADR 0030).

## Evidence

- Decisions D87, D100, D116 and D118 in the decision log;
  Detection comparison;
  agent evaluation.
- IaC: [`infra/ops-app/model-deployment.bicep`](../../../infra/ops-app/model-deployment.bicep),
  [`infra/foundry/isolated.bicep`](../../../infra/foundry/isolated.bicep).
- Live check: both deployments Standard, capacities 500 and 600.
- [Deployment types for Foundry models](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types)
