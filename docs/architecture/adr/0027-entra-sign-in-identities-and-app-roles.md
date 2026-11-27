# ADR 0027: Entra sign-in, an identity per app and per agent, and app roles on each sample system

## Status

Accepted; the systems' app roles added by D106.
Supersedes ADR 0009. Replaces the retired
ADR 0017
(MSAL bearer tokens validated in the first-phase API).

## Context

Every action must be attributable to the person or agent behind it, without keys or secrets in
the apps, and only ARROW may change an airline system. ADR 0009 ran all agents in one worker
process, so agents had no identities of their own. Foundry prompt agents now have their own Entra
agent identities (ADR 0026), and the first-phase API that validated tokens itself was deleted
(D111).

## Decision

- **People.** ARROW and the Test Bench sign people in with Container Apps built-in
  authentication (Entra, app `airrec-app-dev`), which admits only listed users. ARROW's code then
  checks its own lists: OCC duty managers may approve and change limits, Passenger Services
  experts only review rebookings. ARROW defines no app roles; Dev lists two duty managers (D98).
- **Apps.** One user-assigned managed identity per app (`id-airrec-ops-dev`, one per system, the
  Test Bench); Schedule and the two day jobs share `id-airrec-systems-dev`. Data-plane roles only,
  each on the narrowest scope (a queue, a topic, a job, the Application Insights component).
- **Agents.** Each Foundry prompt agent uses its own Entra agent identity. ARROW admits the
  orchestrator and the Case Assistant on `/mcp` by identity and decides in code which tools each
  may call.
- **Sample systems.** The app `airrec-systems-dev` defines `Schedule.Read`,
  `Maintenance.Read`, `Crew.Read`, `Booking.Read`, `Slots.Read` and `Actions.Write`, with role
  assignment required. A call needs a listed object id **and** the route's role. Each domain agent
  holds only its system's read role; ARROW holds four read roles and `Actions.Write`, which no other
  identity holds.

## Alternatives rejected

- **App roles only on the systems:** one Entra slip would open a system (D106).
- **One read role for every system:** the portal would not show least privilege (D106).
- **Validating tokens in application code (ADR 0017):** built-in authentication does the
  validation; code keeps only the allow-lists and role checks.
- **App roles for ARROW's operators:** not built; two Dev duty managers are managed as a list.
  Production would use Entra groups or roles.

## Consequences

- An identity without a role gets no token for the systems; a read without the role or a write
  from anyone but ARROW is refused and recorded with its caller (D114).
- ARROW requests its systems token by the plain app ID, because a managed-identity token is cached
  for up to 24 hours (D106).
- Operator access is a configuration list per environment, not an Entra role: adding a duty manager
  is a redeploy.

## Evidence

- Decisions D106 and D114 in the decision log;
  app-role evidence.
- Code: [`src/airline_systems/caller.py`](../../../src/airline_systems/caller.py),
  [`src/ops_app/app.py`](../../../src/ops_app/app.py); IaC:
  [`infra/ops-app/main.bicep`](../../../infra/ops-app/main.bicep),
  [`infra/systems/system.bicep`](../../../infra/systems/system.bicep).
- Live check: role assignments on `airrec-systems-dev` read back as above.
- [Authentication in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/authentication)
- [Managed identities in Azure Container Apps](https://learn.microsoft.com/azure/container-apps/managed-identity)
- [What's new in Microsoft Entra Agent ID](https://learn.microsoft.com/entra/agent-id/whats-new-agent-id)
- [Add app roles and receive them in the token](https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps)
