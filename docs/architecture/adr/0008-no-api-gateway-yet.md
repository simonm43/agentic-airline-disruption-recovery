# ADR 0008: No API gateway yet

## Status

Accepted. Reviewed against the implementation: still holds.

## Context

Azure API Management could front the sample airline systems, ARROW's MCP tools and the model
deployments (review §2 C9). The review assessed whether the project needs it now (review §7.5).

## Decision

Do not deploy API Management. The systems' interfaces and ARROW's MCP tools are called only by
ARROW and its own Foundry agents, each admitted by its Entra identity (ADR 0027). Authority stays in
ARROW's code (ADR 0030) and in the systems' role checks. A production architecture may show API
Management in front of the recipients' adapters without building it.

## Alternatives rejected

- **API Management now (C9):** authority needs case state (plan hash, case version, limits,
  standby crews left), so a gateway policy would call back into ARROW, adding a hop without adding
  a decision (review §7.5). Sweden Central list prices when decided: Developer about
  USD 51 a month without an SLA; Basic v2 about USD 197 without VNet integration; Standard v2 about
  USD 913, the realistic production entry point.

## Consequences

- No gateway token quotas, load balancing or semantic caching. The model deployments' own rate
  limits apply; code retries an HTTP 429 and the queue limits ARROW to two assessments at once
  (ADR 0025).
- Adopt API Management when any of these appears: real vendor APIs, partner channels or outside
  MCP clients; several applications sharing Foundry model capacity; ARROW's MCP tools offered to
  consumers outside ARROW's own agents.

## Evidence

- Review §2 (C9), §7.5, §15 (review).
- Code: [`src/ops_app/app.py`](../../../src/ops_app/app.py) (who may call ARROW's tools).
- [AI gateway capabilities in API Management](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
- [Expose a REST API in API Management as an MCP server](https://learn.microsoft.com/azure/api-management/export-rest-mcp-server)
