# ADR 0031: Azure SQL Standard S2 as ARROW's system of record

## Status

Accepted (D86). Supersedes
ADR 0003.

## Context

Cases, plans, decisions, approvals, actions, receipts and the sample systems' data are relational
and change together. ADR 0003 kept the first-phase database on the serverless free offer, which
pauses when idle. One morning the owner could not see data at 09:30 because the database had
paused (error 40613) and asked why it was not kept running.

## Decision

- One Azure SQL database, `airrec-ops` on server `sql-airrec-dev-bslgt7jwkw2zu`, Standard S2
  (50 DTUs, 250 GB, about USD 74 a month list price), always on. The free offer was left for good.
- A schema per sample system (`schedule`, `maintenance`, `crew`, `booking`, `slots`), `ops` for
  ARROW and `audit` for the systems' call records. Each app signs in as its managed identity, granted
  only the schemas or tables it uses.
- Entra-only authentication; public network access disabled (a tenant policy enforces it, D28);
  reached only through the private endpoint `pe-airrec-sql-dev-swc`.
- The generated day is loaded at 00:05 UTC by `caj-airrec-dayload-dev`, because the full load takes
  about a minute on S2.

## Alternatives rejected

- **Keep the serverless free offer (ADR 0003):** its idle pause made the first reads fail.
- **Standard S1:** the owner chose S2.
- **Cosmos DB:** the data is relational and transactional; the first-phase Cosmos account was
  deleted (D111).

## Consequences

- No cold start; ARROW still answers 503 "waking or temporarily unavailable" if SQL is briefly
  unreachable.
- About USD 74 a month whether used or not.
- The sample systems share ARROW's database only because they are samples; in production they are
  the airline's own systems behind their own interfaces (ADR 0028), and only `ops` stays ARROW's.

## Evidence

- Decisions D28, D86 and D111 in the decision log;
  S2 evidence.
- IaC: [`infra/ops-app/database.bicep`](../../../infra/ops-app/database.bicep),
  [`infra/modules/sql.bicep`](../../../infra/modules/sql.bicep), [SQL scripts](../../../infra/systems/sql/).
- Live check: `airrec-ops` Standard, capacity 50; server public network access
  Disabled.
- [Azure SQL Database DTU purchasing model](https://learn.microsoft.com/azure/azure-sql/database/service-tiers-dtu?view=azuresql)
