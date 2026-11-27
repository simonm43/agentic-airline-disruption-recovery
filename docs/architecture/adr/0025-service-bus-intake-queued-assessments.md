# ADR 0025: Service Bus intake with queued, retried assessments; Fabric leaves the request path

## Status

Accepted by the owner (D110); Foundry's 400 `tool_user_error` was later added to the retried
failures (D127). Supersedes ADR 0005
for disruption intake. Execution is governed by [ADR 0028](0028-dispatch-through-recipient-interfaces.md).

## Context

Every new case started a thread that assessed it at once, with no limit and no retry. Six
events at once gave three Hypertext Transfer Protocol (HTTP) 429 responses and one 424 from Foundry, and those
cases stopped although nothing was wrong with the disruptions. Events from systems also needed
the Fabric F4 resumed first (paused when idle); the first event after a resume took 30 s to
become a case. The owner asked for Service Bus "with retry, exponential backoff etc. all the
good pattern" and chose it over a work queue in Azure SQL as the production path
(spec).

## Decision

- A Service Bus Standard namespace with Entra authentication only (local keys disabled).
- Systems publish disruption CloudEvents to the topic `disruption-events`. ARROW's intake reads
  the subscription `assessment`, opens each case at once (the operations control centre, OCC, sees it queued) and sends "wake
  case X" to the queue `assessments`. Reports typed in ARROW open their case and do the same.
- ARROW's two worker receivers take one message each (peek-lock, lock renewed while working).
- Temporary failures before any plan exists are retried by deterministic code: HTTP 408, 424,
  429, 500, 503, lost connections and Foundry's 400 `tool_user_error` (a tool or agent the run
  called failed; D127), but not a gateway 502/504 or a timeout while a Foundry run may still be
  working, and no other 400. Waits follow exponential backoff with random jitter: the first is
  15 s, each later one doubles, and each varies randomly by up to 20%; the service's Retry-After
  is honoured when longer, up to 5 minutes; at most four attempts inside the case deadline. The
  case in Azure SQL holds the attempt;
  queue messages only wake the worker, so a stale or repeated message does nothing.
- A retry resumes from the case: a recorded disruption is not recorded again, and a case with
  any plan is never retried (OCC decides, as before).
- Expired or repeatedly crashed messages go to the dead-letter queue; the case deadline stops
  the case visibly.

## Alternatives rejected

- **A work queue in the case store (Azure SQL):** the same behaviour with one store and no new service; the
  owner preferred the production path.
- **Fabric Eventstream or Eventhouse as the queue:** no per-message lock, delayed retry or
  dead-letter queue, and the capacity is paused when idle.
- **Feeding the Eventhouse from Service Bus as well:** Fabric's Service Bus source needs access
  keys; Dev is Entra-only.
- **Service Bus for execution:** still rejected; Azure SQL holds what ran (ADR 0028).
- **A separate worker app scaled on queue length:** not needed for one replica and two workers.

## Consequences

- A burst waits in the queue instead of failing; at most two assessments call Foundry at once.
- Sending events no longer depends on Fabric; the Test Bench lists events from ARROW, which
  holds every received event with its case.
- Two stores now hold intake state. Azure SQL stays the source of truth; a lost or dead-lettered
  message only delays a case until its 15-minute deadline stops it.
- About USD 9.81 a month (Standard base charge).
- Fabric was then retired and deleted (D111); weather updates for a held storm plan
  (ADR 0029) arrive on the same topic.

## Evidence

- Service Bus intake spec;
  decisions D110 and D127 in findings;
  live evidence.
- Code: [`src/ops_app/intake.py`](../../../src/ops_app/intake.py),
  [`src/ops_app/bus.py`](../../../src/ops_app/bus.py),
  [`src/ops_app/engine.py`](../../../src/ops_app/engine.py) (retry rules); infrastructure as code:
  [`infra/ops-app/servicebus.bicep`](../../../infra/ops-app/servicebus.bicep).
- [Queue-Based Load Leveling pattern](https://learn.microsoft.com/azure/architecture/patterns/queue-based-load-leveling)
- [Retry pattern](https://learn.microsoft.com/azure/architecture/patterns/retry)
- [Service Bus premium and standard tiers](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-premium-messaging)
- [Duplicate detection](https://learn.microsoft.com/azure/service-bus-messaging/duplicate-detection)
- [Prevent message loss and duplicates](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-message-loss-and-duplicates)
- [Add an Azure Service Bus source to an eventstream](https://learn.microsoft.com/fabric/real-time-intelligence/event-streams/add-source-azure-service-bus) (needs access keys)
