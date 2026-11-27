# Architecture decision records

ARROW's architecture decision records (ADRs), each with Status, Context, Decision, Alternatives rejected,
Consequences and Evidence. How ARROW runs today is in the [README](../../../README.md#architecture)
and its [diagram](../arrow-architecture.png);
the decision log is findings and open questions.

| ADR | Decision | Status |
|---|---|---|
| [0008](0008-no-api-gateway-yet.md) | No application programming interface (API) gateway yet | Accepted |
| [0025](0025-service-bus-intake-queued-assessments.md) | Service Bus intake with queued, retried assessments | Accepted |
| [0026](0026-foundry-agents-through-arrow-typed-tools.md) | Foundry prompt agents act only through ARROW's typed Model Context Protocol (MCP) tools | Accepted |
| [0027](0027-entra-sign-in-identities-and-app-roles.md) | Entra sign-in, an identity per app and per agent, app roles on each sample system | Accepted |
| [0028](0028-dispatch-through-recipient-interfaces.md) | The governed executor sends each action through its recipient's own interface | Accepted |
| [0029](0029-storm-plan-held-until-confirmed.md) | A storm plan is held until the storm is confirmed | Accepted |
| [0030](0030-deterministic-routing-rules-and-limits.md) | Plans are routed by a rule table in code, with editable limits under four-eyes | Accepted |
| [0031](0031-azure-sql-standard-s2-system-of-record.md) | Azure SQL Standard S2 as ARROW's system of record | Accepted |
| [0032](0032-regional-models-per-agent.md) | Two pinned regional models, chosen per agent from Dev runs; no model router | Accepted |
| [0033](0033-dev-sql-access-over-devnet-peering.md) | Dev developer access to private SQL over the developer-network peering | Accepted |
| [0034](0034-three-container-apps.md) | ARROW in three container apps: operator app, agent tools and workers; only the workers write | Accepted |

Numbers missing from this index belong to records that were retired or replaced
during development.

`tests/docs/test_adrs.py` checks that every record has these sections and that this index links it.
