# ADR 0034: ARROW in three container apps

## Status

Accepted (owner, 25 November 2026: "can we maybe also start the implementation with multiple
container apps"); deployed in Dev on 25 November. Replaces the "Three container apps" item that
D133 had left for future development.

## Context

ARROW ran as one container app, `ca-airrec-ops-<env>`, with one managed identity,
`id-airrec-ops-<env>`. One process served the OCC's screens and API, the MCP tools the Foundry
agents call, the Service Bus intake, two assessment workers and the executor. The app was
reachable from the internet (Entra sign-in for people, Easy Auth for the agents), and its
identity was the only one the airline systems let write. A fault anywhere took the operator
screens down with it, and the assessments could not scale apart from the screens.

## Decision

- One image (`airrec-ops-app`, deployed by digest) and three container apps, chosen by `APP_ROLE`,
  in the existing Container Apps environment, sharing Azure SQL and Service Bus:
  - the operator app, `ca-airrec-ops-<env>` (its name and address kept): the OCC's screens and
    API, the Test Bench's routes and the Case Assistant, as `id-airrec-operator-<env>`;
  - the agent tools, `ca-airrec-tools-<env>`: ARROW's MCP tools, with Easy Auth admitting only
    the agents whose definitions use ARROW's connection, as `id-airrec-tools-<env>`;
  - the workers, `ca-airrec-worker-<env>`, with no ingress and one replica: the intake, the
    assessments (the orchestrator runs), the executor and its supervisor, as
    `id-airrec-ops-<env>`, kept so the writer never changes.
- Only the workers' identity holds `Actions.Write` and is in the systems' `WRITER_OIDS`. The
  operator app and the agent tools only read, each the systems it uses; Booking gives each
  journey of a rebooking plan, which pricing needs, to the agent tools by a separate list
  (`JOURNEY_READER_OIDS`), never by the writer list.
- The operator app and the agent tools record an approval or an automatic plan in Azure SQL and
  wake the workers on a Service Bus queue, `executions`; the message carries no authority (ADR
  0028), and the workers' supervisor finds a plan whose wake was lost within a minute.
- State the one process held in memory moves to Azure SQL: the per-flight recording lock
  (`sp_getapplock`), the events that opened no case and the time of the last reset.

## Alternatives rejected

- **The operator app keeps `id-airrec-ops`; the workers get a new writer identity:** the writer
  would change during the cutover, so an approval executing then would be refused.
- **Execution wake-ups on the assessment queue:** an approved plan would wait behind queued
  assessments.
- **Polling Azure SQL for approved plans:** slower and constant reads; kept only as the backstop.
- **The Case Assistant in the workers:** its answer would queue behind assessments; it needs no
  write right.
- **Three images:** three builds and digests to keep in step for the same code.
- **Making the agent tools a writer so Booking gives it the journeys:** an internet-facing app
  could then write.

## Consequences

- No app that can be reached from the internet can write to the airline's systems; the systems
  refuse the operator app's and the agent tools' identities on every write route.
- A failure in the agent tools or the workers leaves the OCC's screens up.
- About USD 30 a month more per environment (two more warm replicas of 0.5 vCPU and 1 GiB).
- The workers run one replica. Scaling on the assessment queue's length is the next step: a scale
  rule with its own identity (Azure Service Bus Data Owner on that queue only, which KEDA needs),
  after a test with two replicas.
- The workers' identity keeps three grants only the one app needed (`INSERT` and `UPDATE` on
  `ops.policy_change`, `SELECT` on `audit.system_call`, Monitoring Reader); removing them is a
  later clean-up that needs a migration and a role-assignment deletion.

## Evidence

- Design: the three container apps spec;
  deployment: [infra/README.md](../../../infra/README.md#from-one-arrow-app-to-three).
- Dev cutover and checks: evidence.
- IaC: [`infra/ops-app/main.bicep`](../../../infra/ops-app/main.bicep),
  [`infra/ops-app/tools.bicep`](../../../infra/ops-app/tools.bicep),
  [`infra/ops-app/worker.bicep`](../../../infra/ops-app/worker.bicep),
  [`infra/ops-app/servicebus.bicep`](../../../infra/ops-app/servicebus.bicep),
  [`infra/systems/sql/021_three_apps.sql`](../../../infra/systems/sql/021_three_apps.sql).
- [Managed identities in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/managed-identity);
  [Set scaling rules in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/scale-app).
