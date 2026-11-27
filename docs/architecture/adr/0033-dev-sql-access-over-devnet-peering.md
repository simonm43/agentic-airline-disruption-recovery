# ADR 0033: Dev developer access to private SQL over the developer-network peering

## Status

Accepted (D28; kept by D111). Supersedes
ADR 0020. Removing the peering is destructive and needs
the owner's explicit approval.

## Context

The developer works from a VM in a separate developer subscription. ADR 0020 planned to
drop the existing developer-network peering and open SQL, storage and Foundry to the developer's IP address
instead. During the first deployment a management-group policy (`GovDeployPolicies`, Modify
effect) rewrote
SQL's public network access to Disabled during the deployment, so no firewall rule could be
created (D28). Developer access already worked privately through the peering and its private DNS
link.

## Decision

- SQL stays private: public network access Disabled, Entra-only, reached through the private
  endpoint `pe-airrec-sql-dev-swc`.
- The developer reaches it over the VNet peering `airrec-to-devnet` and the DNS zone link
  `link-devnet-developer`, both declared in `infra/modules/network.bicep`.
- The developer-side half (`devnet-to-airrec`) is recorded in `infra/devnet-peering.bicep`, which CI
  compiles but no deployment step applies, so deploying `infra/main.bicep` never writes to the developer
  subscription.
- Foundry, the apps, Service Bus and the registry keep public endpoints protected by Entra (D80);
  the storage accounts ADR 0020 covered were deleted (D111).

## Alternatives rejected

- **Public SQL limited to the developer's IP (ADR 0020):** the tenant policy forces it to
  Disabled.
- **A policy exemption:** a tenant governance control, and the private path already worked (D28).
- **Bastion or a point-to-site VPN gateway:** both need a public IP, which the tenant gates (D22,
  D26).

## Consequences

- An exception to "the project owns everything it uses": the developer's VNet is in another
  subscription. It carries developer access only; no app depends on it.
- Removing the peering first needs another private developer path.
- This is a Dev-only arrangement.

## Evidence

- Decisions D22, D26, D28, D80 and D111 in the decision log;
  cleanup evidence.
- IaC: [`infra/modules/network.bicep`](../../../infra/modules/network.bicep),
  [`infra/devnet-peering.bicep`](../../../infra/devnet-peering.bicep),
  [`infra/modules/sql.bicep`](../../../infra/modules/sql.bicep).
- Live check: peering `airrec-to-devnet` Connected and in sync; link
  `link-devnet-developer` present; SQL public network access Disabled.
- [Azure Private Link for Azure SQL Database](https://learn.microsoft.com/azure/azure-sql/database/private-endpoint-overview?view=azuresql)
