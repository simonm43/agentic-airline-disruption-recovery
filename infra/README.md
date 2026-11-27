# Deploy ARROW

This guide deploys ARROW into an Azure subscription, one step at a time. Hand it to an engineer or
an agent with "deploy according to this file". Every step gives the commands, what to expect and
how to check it. The [root README](../README.md#architecture) explains how the
pieces fit; this page only builds them.

Everything that differs between environments is in one file, `infra\environments\<name>.json`
(`dev.json`, `prd.json`). The tools compute every resource name from it and look up everything
else (identities, the apps' domain, app registrations, agent identities) in Azure, Entra and
Foundry, so nothing is typed twice.

A deployment holds, in one resource group `rg-airrec-<env>-<region>`:

- the platform: a VNet with a private-endpoint subnet and a Container Apps subnet, Entra-only
  private SQL, a container registry, Log Analytics with Application Insights, and a budget;
- Microsoft Foundry: an account and project with two model deployments, seven prompt agents, each
  with its own Entra agent identity, and their MCP and A2A connections;
- a Container Apps environment with ARROW's three apps (the operator app, the agents' tools and the
  workers; only the workers write to the airline systems), the five sample airline systems
  (Schedule, Maintenance, Crew, Booking, Slots), the Test Bench, and the day-load, day-reset and
  migration jobs;
- Service Bus: the topic the airline's systems report disruptions to, and ARROW's assessment and
  execution queues;
- and, in Entra, two app registrations: ARROW's sign-in and API, and the airline systems' API with
  its app roles.

Rules that hold throughout:

- Anything in Production, destructive operations (deleting resources, role assignments or data),
  spend above an environment's budget, and material architecture or security changes need the
  owner's explicit approval first.
- Every Azure CLI command that acts on a subscription passes `--subscription` (Entra's `az ad`
  commands act on the tenant and take none); every tool takes the environment name and targets
  that environment's subscription only.
- No secret is printed or written to disk. ARROW's sign-in secret goes from Entra straight into
  the deployment request.
- Only an OCC Duty Manager approves a recovery plan. Never approve one while deploying or testing.

**Steps:** [1 Prerequisites](#1-prerequisites) · [2 The environment file](#2-the-environment-file) ·
[3 Providers and the feature](#3-providers-and-the-container-apps-feature) ·
[4 Preflight](#4-preflight) · [5 Foundation](#5-foundation) · [6 Foundry](#6-foundry) ·
[7 Identities and database](#7-identities-and-the-database) ·
[8 Container Apps environment](#8-container-apps-environment) ·
[9 App registrations](#9-entra-app-registrations) · [10 Telemetry roles](#10-telemetry-roles) ·
[11 Images](#11-images) · [12 Migrations](#12-database-migrations) · [13 The day](#13-the-day-jobs-and-the-first-day) ·
[14 Test Bench](#14-test-bench) · [15 Service Bus](#15-service-bus) ·
[16 Foundry connections](#16-foundry-connections) · [17 Agents](#17-agents) ·
[18 Agent access](#18-agent-access) · [19 App roles](#19-app-roles) ·
[20 Airline systems](#20-airline-systems) · [21 ARROW](#21-arrow) ·
[22 Test Bench access](#22-test-bench-access) · [23 Smoke test](#23-smoke-test) ·
[24 Evidence](#24-evidence) · then [load test](#load-test), [cost](#cost-and-budget),
[updates and rollback](#updates-and-rollback), [teardown](#teardown), [Dev](#dev),
[the gates](#the-deployment-gates) and the [offline checks](#offline-infrastructure-contract-checks).

## 1. Prerequisites

### Tools

| Tool | Version (tested) | Install or check |
|---|---|---|
| Windows with PowerShell | 7.4 or later (7.6) | `$PSVersionTable.PSVersion` |
| Azure CLI | 2.89 | `az version` |
| Azure CLI `containerapp` extension | 1.3.0b5 tested (it has only preview releases) | `az extension add --name containerapp --upgrade` |
| Bicep CLI | **0.46.1**, Azure CLI's copy | `az bicep install --version v0.46.1`, then `& "$env:USERPROFILE\.azure\bin\bicep.exe" --version` |
| Python | 3.12 | `python --version` |
| Git | 2.56 tested | `git --version` |

No local Docker: images are built in Azure (ACR Tasks), and the tests, mypy and ruff run inside
each build.

```powershell
git clone https://github.com/simonm43/agentic-airline-disruption-recovery.git C:\src\arrow
cd C:\src\arrow
git checkout <commit to deploy>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
az login --tenant <tenant id>
```

Keep the checkout's path short, at most about 110 characters (`C:\src\arrow` is 12): the locked
environment installs files up to about 140 characters below it, and on Windows pip stops with
`WinError 206` past the 260-character path limit.

**Check:** run the [offline contract checks](#offline-infrastructure-contract-checks); every test
passes.

### Access

- **Azure:** Owner on the target subscription, with the RBAC condition (version 2.0) that stops
  that Owner assignment from granting Owner, User Access Administrator or Role Based Access
  Control Administrator. The preflight (step 4) checks the condition and stops without it: the
  templates never assign those roles (they assign only AcrPull, AcrPush, Foundry User, Foundry
  Agent Consumer, Azure Service Bus Data Sender and Data Receiver, Monitoring Metrics Publisher,
  Monitoring Reader, Reader and the custom role *Airrec test bench operator (&lt;label&gt;)*), and
  the condition keeps a mistake from granting them.
- **Entra:** permission to register applications (the tenant's default user setting, or the
  Application Developer role). You become an owner of both app registrations and their service
  principals; as owner you add ARROW's sign-in secret and assign the systems' app roles. The
  Azure CLI's Microsoft Graph token carries the delegated permissions the scripts use.
- **Foundry:** step 6 gives you Foundry User on the account, which publishing agents needs.

### Session variables

Every later step uses these. Run them in each new PowerShell session:

```powershell
$e    = 'prd'                                   # the environment: infra\environments\<e>.json
$py   = '.\.venv\Scripts\python.exe'
$n    = (& $py -m tools.env.environment $e) -join "`n" | ConvertFrom-Json   # every resource name
$sub  = $n.subscriptionId
$rg   = $n.resourceGroup
$last = "sort_by(@, &properties.startTime)[-1].{name:name, status:properties.status}"   # a job's latest run
az account show --subscription $sub --query "{name:name, state:state, user:user.name}" -o table
```

**Expect:** the subscription's name, `Enabled` and your user name. `$n` holds every name, for
example `$n.registry`, `$n.foundryAccount` or `$n.'app.ops'` (quote names with a dot). From step
11 on, a new session also needs the image digests: set `$commit` to the commit the images were
built from and run step 11's three `az acr repository show` lines again.

## 2. The environment file

`infra\environments\prd.json` is the template for a new environment; copy it to
`<name>.json` and change:

| Field | Meaning |
|---|---|
| `name`, `label` | Short name in resource names (`prd`), and its label in role and display names (`Prd`) |
| `subscriptionId`, `tenantId` | The target; the tools refuse any other subscription |
| `location`, `regionCode` | The region (it must offer the models below) and its code in names (`swc`) |
| `tags` | `owner` and `costCenter`; `workload` (`airrec`) and `environment` are added |
| `network` | The VNet range and its two subnets: a `/27` for private endpoints, a `/26` for Container Apps |
| `developerVnetId` | Empty: a new environment has no developer network (only Dev peers one) |
| `sqlAdministrator` | `identity`: the SQL server's Entra admin is the managed identity the migration job runs as |
| `budgetUsd` | The monthly budget: its alerts, and the cost gates' ceiling |
| `logDailyCapGb` | Log Analytics daily cap |
| `models` | The two model deployments: name, model, version, capacity (thousands of tokens a minute) and the quota they draw on |
| `operators` | Object IDs of the OCC Duty Managers who sign in to ARROW; the first is the main operator, the others make four-eyes rule changes possible |
| `testbenchOwners` | Object IDs who may sign in to the Test Bench |
| `apps` | Leave empty: step 9 creates the app registrations and the tools find them by name |
| `policyAssignments` | The subscription's policy assignments the preflight expects to find (list yours: `az policy assignment list --subscription $sub --query "[].name"`) |

**Check:** `& $py -m tools.env.environment $e` prints every name; `& $py -m pytest
tests\infra\test_environment.py -q -p no:cacheprovider` passes.

## 3. Providers and the Container Apps feature

Once per subscription. This changes the subscription (approval rule above).

```powershell
foreach ($p in 'Microsoft.Network', 'Microsoft.Sql', 'Microsoft.ContainerRegistry', 'Microsoft.OperationalInsights',
               'Microsoft.Insights', 'Microsoft.ManagedIdentity', 'Microsoft.Consumption', 'Microsoft.App',
               'Microsoft.CognitiveServices', 'Microsoft.ServiceBus') {
    az provider register --subscription $sub --namespace $p --wait
}
az feature register --subscription $sub --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress
az feature show --subscription $sub --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --query properties.state -o tsv
# repeat the show until it says Registered, then:
az provider register --subscription $sub --namespace Microsoft.Network --wait
```

The Container Apps environment is external (ARROW and the Test Bench are reached from a browser)
and lives in the workload VNet, so the platform gives it a managed public IP address; creating it
fails with `ManagedEnvironmentOperationFailedDueToSubscriptionNotRegisteredForFeature` until this
feature is registered. Where a tenant restricts public IP addresses, registering it is a
governance decision: ask the tenant's owner. If the state stays `Pending`, stop: Microsoft
approves it.

**Check:**

```powershell
foreach ($p in 'Microsoft.Network', 'Microsoft.Sql', 'Microsoft.ContainerRegistry', 'Microsoft.OperationalInsights',
               'Microsoft.Insights', 'Microsoft.ManagedIdentity', 'Microsoft.Consumption', 'Microsoft.App',
               'Microsoft.CognitiveServices', 'Microsoft.ServiceBus') {
    az provider show --subscription $sub -n $p --query "{ns:namespace, state:registrationState}" -o tsv
}
```

Every line says `Registered`, and the feature says `Registered`.

## 4. Preflight

Read-only: it checks you, the subscription and your conditioned Owner assignment, the policy
assignments, the providers and API versions, the feature, the model quota, the DNS zone, the
billing currency and the resource group's inventory, and writes `.local\<env>.parameters.json`
for step 5.

```powershell
pwsh -NoProfile -NonInteractive -File tools\env\preflight.ps1 -Environment $e
```

**Expect:** exit code 0, or 2 with `[NEEDS_CONTEXT]` lines, each saying what a person must check.
It reports the model quota as, for example, `OpenAI.Standard.gpt4.1 in swedencentral: 1000 units
free for gpt-4.1-eval (500 needed)`. A subscription with no cost yet this month gets a
`[NEEDS_CONTEXT]` line instead of the billing currency; the cost gates then count its cost as
USD 0 and project the rest of the month at that rate. **Stop** on exit 1, or when a model lacks
quota: request more (Azure portal, Quotas) or lower `capacity` in the environment file.

A new environment normally gets four `[NEEDS_CONTEXT]` lines. Check them, then go on:

- *The expected policy assignments are in place, but their initiatives' parameters … are not
  resolved here*: the preflight has matched `policyAssignments` by name and definition and found
  no other assignment; a further one gets a line of its own, so read what it enforces before
  deploying. The templates already set what this tenant's Modify policies enforce (SQL public
  network access disabled).
- *Provider metadata does not expose child type …* (three lines, for the VNet peering, the
  private endpoint's DNS zone group and SQL's Entra-only setting): step 5's validation and what-if
  check these API versions; go on if they succeed.

## 5. Foundation

`infra\main.bicep` (subscription scope): the resource group, the VNet `vnet-airrec-<env>-<region>`
with `snet-private-endpoints` and `snet-aca` and their NSGs, the managed identity
`id-airrec-sqladmin-<env>`, the SQL server (Entra-only, public network access disabled, TLS 1.2)
with that identity as its Entra admin, its private endpoint and the `privatelink.database.windows.net`
zone linked to the VNet, the Basic registry (admin user and anonymous pull off) with your AcrPush,
Log Analytics and Application Insights (local authentication off), and the budget.

```powershell
.\tools\env\deploy.ps1 -Environment $e -Stage foundation                            # preview
.\tools\env\deploy.ps1 -Environment $e -Stage foundation -Apply -IncrementUsd 90    # within 60 minutes
```

**Expect:** the preview runs the preflight and validation and summarises the what-if
(`Create: …`, no `REFUSED` line), and ends `Preview written to …`. The apply repeats them, passes
the cost gate and the what-if gate and ends `Deployment airrec-<env>-foundation succeeded`.

**Check:**

```powershell
az sql server show --subscription $sub -g $rg -n $n.sqlServer --query "{public:publicNetworkAccess, admin:administrators.login, entraOnly:administrators.azureAdOnlyAuthentication}" -o table
az acr show --subscription $sub -g $rg -n $n.registry --query "{admin:adminUserEnabled, anonymous:anonymousPullEnabled}" -o table
```

`Disabled`, `id-airrec-sqladmin-<env>`, `True`; then `False`, `False`.

## The resource-group steps

Most of steps 6 to 21 deploy one resource-group template each with `tools\env\deploy_group.py`.
It compiles the template, takes the environment's names and looks up every other value live (your
object ID, the identities, the apps' domain, the app registrations, the agents' identities), so no
parameter is typed. Each is two commands:

```powershell
& $py -m tools.env.deploy_group $e <key> [--image <digest>]                                  # preview
& $py -m tools.env.deploy_group $e <key> [--image <digest>] --apply --increment-usd <usd>    # deploy
```

The preview prints the parameters, validates, and writes and summarises the what-if
(`.local\deploy\<env>-<key>.whatif.json`). Read the summary: in a new environment expect `Create`
and `Ignore` only, never a `REFUSED` line. The apply, within 60 minutes, runs the cost gate, a
fresh what-if that must equal the preview, and the what-if gate, then deploys and ends
`Deployment airrec-<env>-<key> succeeded`. The increments below are each step's expected monthly
cost, from Dev's actual cost by service.

A template that grants an identity AcrPull and creates the app or job pulling with it (the
migration job, the day jobs, the Test Bench, the systems) can fail with *unable to pull image
using Managed identity* while the new role assignment propagates. The deployer then waits a minute
and deploys the same body again, up to five times, and says so. If an apply stops on another
error, read it, fix the cause, and run the preview again before the apply: the partial deployment
changed what the next what-if shows.

## 6. Foundry

```powershell
& $py -m tools.env.deploy_group $e foundry
& $py -m tools.env.deploy_group $e foundry --apply --increment-usd 0
& $py -m tools.env.deploy_group $e foundry-monitoring
& $py -m tools.env.deploy_group $e foundry-monitoring --apply --increment-usd 0
& $py -m tools.env.deploy_group $e model
& $py -m tools.env.deploy_group $e model --apply --increment-usd 0
```

`foundry` (`infra\foundry\isolated.bicep`): the account `aif-airrec-<env>-<suffix>` (public
endpoint, Entra only, local authentication disabled), the project `proj-airrec-<env>`, the
`gpt-4.1-mini` deployment, Foundry User for you and for the project's identity. Dev's account and
project predate these names (`aif-airrec-chat-dev-…`, `proj-airrec-chat-dev`).
`foundry-monitoring`: the project's Application Insights connection for Foundry tracing.
`model`: the `gpt-4.1-eval` deployment (`gpt-4.1`, no automatic version upgrade). Models are
paid per token, so the increment is 0.

Azure itself may then add a failed deployment `Failure-Anomalies-Alert-Rule-Deployment-<id>` to
the resource group: Application Insights tries to create its automatic smart-detection alert rule,
which needs the `Microsoft.AlertsManagement` provider (*MissingSubscriptionRegistration*). ARROW
does not use that rule, so step 3 does not register the provider; Dev has the same failed
deployment. Leave it.

**Check:**

```powershell
az cognitiveservices account deployment list --subscription $sub -g $rg -n $n.foundryAccount --query "[].{name:name, model:properties.model.name, version:properties.model.version, sku:sku.name, capacity:sku.capacity}" -o table
```

Both deployments, `Standard`, with the capacities in the environment file.

## 7. Identities and the database

```powershell
& $py -m tools.env.deploy_group $e prerequisites
& $py -m tools.env.deploy_group $e prerequisites --apply --increment-usd 0
& $py -m tools.env.deploy_group $e database
& $py -m tools.env.deploy_group $e database --apply --increment-usd 75
```

`prerequisites`: the identities `id-airrec-{ops,operator,tools,systems,maintenance,crew,booking,slots}-<env>`
(ARROW's three apps run as `ops`, the workers and the only writer, `operator` and `tools`), AcrPull
on the registry for the three, and Foundry User on the account for the workers (they run the
orchestrator) and the operator app (it runs the Case Assistant). `database`: `airrec-ops`,
Standard S2, always on (the free tier's idle pause failed the first reads).

**Check:**

```powershell
az identity list --subscription $sub -g $rg --query "[].name" -o tsv
az sql db show --subscription $sub -g $rg -s $n.sqlServer -n $n.database --query "{objective:currentServiceObjectiveName, status:status}" -o table
```

Nine identities (with `id-airrec-sqladmin-<env>`); `S2`, `Online`.

## 8. Container Apps environment

```powershell
& $py -m tools.env.deploy_group $e environment
& $py -m tools.env.deploy_group $e environment --apply --increment-usd 25
```

`cae-airrec-<env>-<region>` on `snet-aca`, external, Consumption workload profile.

**Check:**

```powershell
az containerapp env show --subscription $sub -g $rg -n $n.containerEnvironment --query "{state:properties.provisioningState, domain:properties.defaultDomain}" -o table
```

`Succeeded` and the apps' domain (`<words>-<hex>.<region>.azurecontainerapps.io`).

## 9. Entra app registrations

Scripted over Microsoft Graph, because the Graph Bicep extension cannot create client secrets
and what-if cannot preview Graph resources, which the deployment gate relies on.

```powershell
& $py -m tools.env.entra_apps $e            # read-only: what would change
& $py -m tools.env.entra_apps $e --apply    # create or update, then read back
```

It creates `airrec-app-<env>` (ARROW's and the Test Bench's sign-in: single tenant, ID tokens,
access tokens v2, identifier `api://<appId>`, the two apps' sign-in redirect URIs) and
`airrec-systems-<env>` (the systems' API: identifier `api://<appId>`), each with its service
principal, and makes you an owner of all four. A re-run changes only what differs.

**Expect:** the read-back lists both apps with their `appId` and `"changes": []`.

**Check:**

```powershell
az ad app list --filter "displayName eq '$($n.arrowAppRegistration)'" --query "[].{appId:appId, uris:identifierUris, redirects:web.redirectUris}" -o json
```

## 10. Telemetry roles

```powershell
& $py -m tools.env.deploy_group $e telemetry
& $py -m tools.env.deploy_group $e telemetry --apply --increment-usd 0
```

Monitoring Metrics Publisher on Application Insights for ARROW's three apps' and the systems'
identities, and Monitoring Reader for the operator app's, which reads a case's trace.

**Check:**

```powershell
$appi = az resource show --subscription $sub -g $rg -n $n.appInsights --resource-type Microsoft.Insights/components --query id -o tsv
az role assignment list --subscription $sub --scope $appi --query "[].{role:roleDefinitionName, principal:principalId}" -o table
```

Monitoring Metrics Publisher for the eight app identities and the Foundry project's, Monitoring
Reader for the operator app's (`id-airrec-operator-<env>`), Reader for the project's.

## 11. Images

Build from a clean detached worktree of the commit, so nothing uncommitted ships. Each build runs
the app's tests, strict mypy and ruff inside `az acr build`. The scripts in the worktree read the
names with `python` on PATH (3.12), as the worktree has no `.venv`.

```powershell
$commit = git rev-parse --short HEAD
git worktree add --detach ..\arrow-build $commit
& ..\arrow-build\tools\systems\build.ps1   -Environment $e -ImageTag "sys-$commit"   # airrec-systems, with the migrations
& ..\arrow-build\tools\ops_app\build.ps1   -Environment $e -ImageTag "ops-$commit"   # airrec-ops-app (ARROW)
& ..\arrow-build\tools\testbench\build.ps1 -Environment $e -ImageTag "tb-$commit"    # airrec-testbench
git worktree remove ..\arrow-build
$sys = az acr repository show --subscription $sub -n $n.registry --image "airrec-systems:sys-$commit" --query digest -o tsv
$ops = az acr repository show --subscription $sub -n $n.registry --image "airrec-ops-app:ops-$commit" --query digest -o tsv
$tb  = az acr repository show --subscription $sub -n $n.registry --image "airrec-testbench:tb-$commit" --query digest -o tsv
$sys, $ops, $tb
```

**Expect:** each build ends with `Run ID: … was successful`. **Check:** three digests, each
`sha256:` and 64 hexadecimal characters. Deploy by digest only, never by tag.

## 12. Database migrations

The database is private and the new environment has no developer network, so the migrations run
inside it: a manual job `caj-airrec-dbmigrate-<env>` runs every file in `infra\systems\sql` in
name order (`tools\systems\migrate.py`, built into the systems image) as
`id-airrec-sqladmin-<env>`. Each file is idempotent and runs in its own transaction; the files
create the schemas, the database users of the apps' identities (by client ID, so no Graph
permission is needed) and their grants.

```powershell
& $py -m tools.env.deploy_group $e migration --image $sys
& $py -m tools.env.deploy_group $e migration --image $sys --apply --increment-usd 0
az containerapp job start --subscription $sub -g $rg -n $n.migrationJob --query name -o tsv
az containerapp job execution list --subscription $sub -g $rg -n $n.migrationJob --query $last -o table   # repeat until Succeeded
az containerapp job logs show --subscription $sub -g $rg -n $n.migrationJob --container dbmigrate --tail 300 --format text
```

**Expect:** the execution says `Succeeded` after about a minute (a failed file is rolled back and
fails the execution). The log then shows `applied 001_… in … s` for each file and ends
`MIGRATED {"files": 21, "tables": 28, "entra_users": 8, "airport_rules": 1, "flights": 0, "stations_without_rule": 0}`
(the one airport rule is Lisbon's, which `008` always declares; no day is loaded yet). Read the
log once the execution has ended: `--follow` joins too late to print a run this short.
`az containerapp job logs` is a preview command, and the log stays readable only while the run's
replica exists; the execution status is the check.

The database users keep the names the migrations give them (`id-airrec-<component>-dev`) in every
environment: the migrations are immutable history, and each user is bound to this environment's
identity by its client ID, so the name is only a label.

## 13. The day jobs and the first day

```powershell
& $py -m tools.env.deploy_group $e jobs --image $sys
& $py -m tools.env.deploy_group $e jobs --image $sys --apply --increment-usd 1
az containerapp job start --subscription $sub -g $rg -n $n.dayResetJob --query name -o tsv
az containerapp job execution list --subscription $sub -g $rg -n $n.dayResetJob --query $last -o table   # repeat until Succeeded
az containerapp job start --subscription $sub -g $rg -n $n.migrationJob --query name -o tsv
az containerapp job execution list --subscription $sub -g $rg -n $n.migrationJob --query $last -o table   # repeat until Succeeded
az containerapp job logs show --subscription $sub -g $rg -n $n.migrationJob --container dbmigrate --tail 300 --format text
```

`caj-airrec-dayload-<env>` loads the next Lisbon day at 00:05 UTC; `caj-airrec-dayreset-<env>`
replaces today's day (the Test Bench's Reset starts it). Starting the reset job once loads the
first day: 900 flights. The second migration run then declares the airport rules of the stations
that day flies (`008`, `009`).

**Expect:** the reset execution `Succeeded` after about 90 seconds; the second migration
`Succeeded` and logs `"flights": 900` and `"stations_without_rule": 0` (every station the day
flies now has an airport rule, so `"airport_rules"` is well above 1).

## 14. Test Bench

```powershell
& $py -m tools.env.deploy_group $e testbench --image $tb
& $py -m tools.env.deploy_group $e testbench --image $tb --apply --increment-usd 2
```

The identity `id-airrec-testbench-<env>`, its AcrPull, the custom role *Airrec test bench
operator (&lt;label&gt;)* (read and start a job, read its executions; nothing else) on both day
jobs, and `ca-airrec-testbench-<env>`, which scales to zero. Its sign-in comes in step 22.

**Check:** `az containerapp show --subscription $sub -g $rg -n $n.'app.testbench' --query properties.provisioningState -o tsv`
says `Succeeded`.

## 15. Service Bus

```powershell
& $py -m tools.env.deploy_group $e servicebus
& $py -m tools.env.deploy_group $e servicebus --apply --increment-usd 10
```

`sb-airrec-<env>-<suffix>`, Standard, local authentication disabled: the topic
`disruption-events` with ARROW's subscription `assessment`, the queue `assessments`, and the queue
`executions`, which hands authorised plans to the workers (a wake only: the case in SQL decides
what runs). The workers (`id-airrec-ops`) receive from the subscription and both queues and send
to `assessments`; the operator and tools apps send to both queues; the Test Bench sends to the
topic.

**Check:**

```powershell
az servicebus topic subscription show --subscription $sub -g $rg --namespace-name $n.serviceBus --topic-name disruption-events -n assessment --query status -o tsv
az servicebus queue list --subscription $sub -g $rg --namespace-name $n.serviceBus --query "[].{name:name, status:status}" -o table
```

`Active`; `assessments` and `executions`, both `Active`.

## 16. Foundry connections

```powershell
& $py -m tools.env.deploy_group $e connections
& $py -m tools.env.deploy_group $e connections --apply --increment-usd 0
& $py -m tools.env.deploy_group $e orchestrator-connections
& $py -m tools.env.deploy_group $e orchestrator-connections --apply --increment-usd 0
```

`connections`: the MCP connections `schedule-mcp`, `maintenance-mcp`, `crew-mcp`, `booking-mcp`,
`slots-mcp` to the systems' `/mcp` (each agent calls with its own agent identity, audience the
systems' API). `orchestrator-connections`: the MCP connection to ARROW's tools app,
`ca-airrec-tools-<env>/mcp` (audience ARROW's API; named `ops-tools`, in Dev `arrow-tools`), and
`a2a-detection`, `a2a-aircraft`, `a2a-crew`, `a2a-passenger`, `a2a-slots` (A2A to the domain
agents).

**Check:**

```powershell
az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.CognitiveServices/accounts/$($n.foundryAccount)/projects/$($n.foundryProject)/connections?api-version=2025-12-01" --query "value[].name" -o tsv
```

The eleven connections above and the Application Insights connection.

## 17. Agents

Prompt agents and their versions are data-plane objects of the Foundry project that no ARM
resource type declares, so they are published over Foundry's Agents REST API.

```powershell
& $py -m tools.env.publish_agents $e            # read-only: create, new version or nothing, per agent
& $py -m tools.env.publish_agents $e --apply    # publish in order, then read back
```

It renders the committed definitions (`infra\ops-app\*.json`, written for Dev) with this
environment's MCP and A2A addresses (ARROW's tools are on the tools app) and publishes, in order: the five domain agents
(`disruption-detection-agent`, `aircraft-recovery-agent`, `crew-recovery-agent`,
`passenger-reaccommodation-agent`, `slot-negotiation-agent`), each with its agent card
(`infra\ops-app\agent-cards.json`) and incoming A2A enabled, then `recovery-orchestrator-agent`,
then `case-assistant-agent`. Foundry gives each agent its own Entra agent identity.

**Expect:** the read-back lists all seven with version `1` (a new environment), status `active`,
state `enabled`, their model and a `principal_id`; the domain agents' protocols are `a2a` and
`responses`. A re-run plans no step. An agent's endpoint serves its latest version at once, so
publish with no case in flight. If a new environment's read-only plan says `new version` rather
than `create`, its project has inherited the agents of a deleted project of the same name, bound
to deleted identities: delete them as in [teardown](#teardown), then publish.

## 18. Agent access

```powershell
& $py -m tools.env.deploy_group $e access-orchestrator
& $py -m tools.env.deploy_group $e access-orchestrator --apply --increment-usd 0
& $py -m tools.env.deploy_group $e access-assistant
& $py -m tools.env.deploy_group $e access-assistant --apply --increment-usd 0
```

Foundry Agent Consumer on the project for the orchestrator's and the Case Assistant's identities,
so they may call the domain agents over A2A.

**Check:**

```powershell
$project = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.CognitiveServices/accounts/$($n.foundryAccount)/projects/$($n.foundryProject)"
az role assignment list --subscription $sub --scope $project --query "[?ends_with(roleDefinitionId, 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6')].{role:roleDefinitionName, principal:principalId}" -o table
```

Two rows: the `principal_id`s step 17 read back for the orchestrator and the Case Assistant.

## 19. App roles

```powershell
& $py -m tools.systems.app_roles --environment $e            # read-only
& $py -m tools.systems.app_roles --environment $e --apply
```

Defines `Schedule.Read`, `Maintenance.Read`, `Crew.Read`, `Booking.Read`, `Slots.Read` and
`Actions.Write` on the systems' API, assigns each caller only what it needs (the Aircraft and
Detection agents `Schedule.Read`, Crew `Crew.Read`, Passenger `Booking.Read`, Slots `Slots.Read`;
ARROW's workers the four reads and `Actions.Write`; the operator app `Schedule.Read` and
`Booking.Read`; the tools app `Schedule.Read`, `Crew.Read`, `Booking.Read` and `Slots.Read`), and
then requires an assignment, so Entra issues no token for the systems to anyone else. It checks
each caller's display name first (`<agent>-AgentIdentity`, `id-airrec-ops-<env>`,
`id-airrec-operator-<env>`, `id-airrec-tools-<env>`).

**Expect:** the read-back's `assignments` match those roles and `appRoleAssignmentRequired` is
`true`.

## 20. Airline systems

```powershell
$systems = 'schedule', 'maintenance', 'crew', 'booking', 'slots'
foreach ($s in $systems) { & $py -m tools.env.deploy_group $e "system-$s" --image $sys }   # five previews
foreach ($s in $systems) { & $py -m tools.env.deploy_group $e "system-$s" --image $sys --apply --increment-usd 15 }
```

Read the five previews before the applies. In a new environment the schedule system's preview
also shows one `Modify`: the AcrPull of `id-airrec-systems-prd`, which the jobs (steps 12 and 13)
already hold; what-if cannot resolve the identity's principal in the template, so the unchanged
assignment shows as a change of `principalId`. One app `ca-airrec-<system>-<env>` per system, one warm
replica each, with Easy Auth for the systems' API. Each admits only its callers, looked up live:
its agents' identities, ARROW's workers (the only writer, in `WRITER_OIDS`) and ARROW's operator
and tools apps where they read that system (the operator app Schedule and Booking; the tools app
Schedule, Crew, Booking and Slots). Booking gives each journey of a rebooking plan, which ARROW's
cost estimate needs, to the workers and to the tools app (`JOURNEY_READER_OIDS`), never to an
agent.

**Check:** `az containerapp show --subscription $sub -g $rg -n $n.'app.schedule' --query properties.runningStatus -o tsv`
says `Running` (likewise for the other four).

## 21. ARROW

ARROW runs as three apps from one image (`$ops`), each started in its own mode (`APP_ROLE`):

| App | Ingress | Runs | Identity |
|---|---|---|---|
| Operator app `ca-airrec-ops-<env>` | External, Easy Auth for the operators and the Test Bench | The UI and the operator API, the Test Bench's routes, the Case Assistant chat | `id-airrec-operator-<env>` |
| Agent tools `ca-airrec-tools-<env>` | External, Easy Auth for the agents only | ARROW's MCP tools for the agents (`/mcp`) | `id-airrec-tools-<env>` |
| Workers `ca-airrec-worker-<env>` | None | The Service Bus intake, the assessments (the orchestrator runs), the executor and the supervisor | `id-airrec-ops-<env>`, the only writer |

```powershell
& $py -m tools.env.deploy_group $e tools --image $ops
& $py -m tools.env.deploy_group $e tools --image $ops --apply --increment-usd 15
& $py -m tools.env.deploy_group $e ops --image $ops
& $py -m tools.env.deploy_group $e ops --image $ops --apply --increment-usd 15 --signin-secret new
& $py -m tools.env.deploy_group $e worker --image $ops
& $py -m tools.env.deploy_group $e worker --image $ops --apply --increment-usd 15
```

`tools`: its Easy Auth admits exactly the agents whose committed definitions use ARROW's MCP
connection (the orchestrator, Detection, Passenger and the Case Assistant), with tokens for
ARROW's API; the code then decides which tools each may call (`ORCHESTRATOR_AGENT_OIDS`,
`ASSISTANT_AGENT_OIDS`, `DETECTION_AGENT_OIDS`, `PASSENGER_AGENT_OIDS`). `ops`: the operators,
the Test Bench's identity, both app registrations and the project's Foundry portal address; no
agent. `--signin-secret new` adds a client secret (180 days) to `airrec-app-<env>` and passes it,
in memory only, to the deployment; later redeploys keep the running app's secret with the default
`--signin-secret current`. `worker`: no ingress and one replica (the supervisor and the timed
standby calls must always run), with a liveness probe on `/healthz` that restarts it when a
receiver or worker thread has stopped. Each app has one warm replica.

**Check:** once the new revisions have started (a minute or so; until then an ingress answers
503), the operator app and the tools app answer without sign-in only where they should, and the
workers run without ingress:

```powershell
$url   = 'https://' + (az containerapp show --subscription $sub -g $rg -n $n.'app.ops' --query properties.configuration.ingress.fqdn -o tsv)
$tools = 'https://' + (az containerapp show --subscription $sub -g $rg -n $n.'app.tools' --query properties.configuration.ingress.fqdn -o tsv)
(Invoke-WebRequest "$url/healthz").StatusCode                                         # 200
(Invoke-WebRequest "$url/api/session" -SkipHttpErrorCheck).StatusCode                 # 401: sign-in required
(Invoke-WebRequest "$tools/healthz").StatusCode                                       # 200
(Invoke-WebRequest "$tools/mcp" -Method Post -SkipHttpErrorCheck).StatusCode          # 401: agents only
az containerapp show --subscription $sub -g $rg -n $n.'app.worker' --query "{status:properties.runningStatus, ingress:properties.configuration.ingress}" -o json
@(az containerapp replica list --subscription $sub -g $rg -n $n.'app.worker' --query "[].name" -o tsv).Count   # 1
```

The workers say `Running` with `"ingress": null`. Then an operator listed in the environment file
signs in at `$url`.

## 22. Test Bench access

```powershell
& $py -m tools.testbench.configure_access --environment $e            # read-only
& $py -m tools.testbench.configure_access --environment $e --apply
```

Checks the Test Bench's redirect URI on ARROW's registration, copies ARROW's sign-in secret into
the Test Bench app in memory, sets the Test Bench's Easy Auth to its owners only (sign-in
required except `/healthz`), and makes sure ARROW's operator app admits the Test Bench's identity. A secret
cannot be declared in `infra\testbench\main.bicep` without becoming a template parameter, so this
is scripted, and a redeploy of that template removes the copy: run this again after one.

**Expect:** `1 redirect URI present: True`, `2 test bench secrets: ['entra-client-secret']`,
`3 test bench admits: …` the owners, `4 ARROW admits: …` including the Test Bench.

## 23. Smoke test

The five Test Bench cases, sent by the Test Bench's own identity from inside its app (exec
sessions are rate-limited to about eight every ten minutes):

```powershell
$sent = .\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.testbench' -Script tools\testbench\intake_live.py -Arg cases | Select-String '^SENT '
$sent.Line                                                       # SENT [[<event id>, <sent at>], ...]
Start-Sleep -Seconds 300
.\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.testbench' -Script tools\testbench\intake_live.py -Arg list
```

The Test Bench scales to zero and an exec wakes it, but an exec that lands on a replica that is
stopping answers `ERROR: Could not find container`. Then wake it and run the command again:

```powershell
$tb = 'https://' + (az containerapp show --subscription $sub -g $rg -n $n.'app.testbench' --query properties.configuration.ingress.fqdn -o tsv)
(Invoke-WebRequest "$tb/healthz" -SkipHttpErrorCheck).StatusCode     # 200
```

**Expect:** five events, each with a case. The workers' two assessment receivers assess them, 40
to 120 seconds each:

| Case | Event | Outcome |
|---|---|---|
| 1 | AR259 20 min late, border-control queue | Executed automatically |
| 2 | AR444 70 min late, ground handling short-staffed | Executed automatically |
| 3 | CS-TQA hydraulic leak, about 4 h out of service | Waiting for OCC |
| 4 | AR689 60 min late, crew over their limit | Waiting for OCC |
| 5 | Crosswind forecast for Lisbon | Waiting for OCC |

To check each case step by step from its saved trail, before any reset: dump the five cases
inside the operator app (read-only, by their event ids), then check them here with ARROW's own
trail code:

```powershell
New-Item -ItemType Directory -Force .local\e2e | Out-Null
$ids = (($sent.Line.Substring(5) | ConvertFrom-Json) | ForEach-Object { $_[0] }) -join '_'
.\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.ops' -Script tools\testbench\case_trails.py -Arg $ids > .local\e2e\$e.dump.txt
& $py -m tools.testbench.check_cases $e .local\e2e\$e.dump.txt --out .local\e2e\$e.findings.json
```

**Expect:** five lines ending `PASS` and exit code 0. Each line checks:
- the outcome above, and every action of an executed plan confirmed by its recipient;
- no refusal, no tool error and no failed airline-system call;
- one `plan_built` per plan, and no two plans with the same title;
- no ARROW case id or trace in an airline-system call's endpoint or request body;
- options assessed one at a time, with no plan built after an automatic one;
- the Detection agent's record of the report, on ARROW's case only, with no airline system's
  event id yet;
- no airline-system write outside an executed plan, whose first action records the reported
  delay or defect, confirmed with the event id its system issues then (a weather forecast is
  recorded in no airline system);
- the Passenger agent's EU261 exposure for every option built.

The JSON adds who was asked about each option and the times from the report: queued, decided
and, for an executed plan, its actions confirmed.

Leave cases 3 to 5 waiting: only an OCC Duty Manager approves. Then reset the day so it starts
clean (`-Arg reset`, which closes the cases and reloads the day; expect `RESET` with
`"state": "done"`, the five cases under `closed` and `"message": "Done · day reloaded"`).

Then check that only the workers can write: from inside the operator app and the tools app, each
system's write route must refuse their identities. The probe sends an empty body, which every
probed interface refuses, so nothing can be written either way:

```powershell
.\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.ops'   -Script tools\systems\write_refusal_probe.py
.\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.tools' -Script tools\systems\write_refusal_probe.py
```

**Expect:** for each app, the token's `oid` (its own identity) and its read roles only (operator:
`Booking.Read`, `Schedule.Read`; tools: `Booking.Read`, `Crew.Read`, `Schedule.Read`,
`Slots.Read`), five `WRITE <system> … 403` lines and `REFUSED 5 of 5`. A system the app reads
answers `caller <oid> is not allowed` (only the workers are in its `WRITER_OIDS`); a system it does
not read refuses it at Easy Auth already.

Each ARROW app holds exactly its own identity:

```powershell
foreach ($a in 'ops', 'tools', 'worker') {
    $held = (az containerapp show --subscription $sub -g $rg -n $n."app.$a" --query identity.userAssignedIdentities -o json | ConvertFrom-Json).PSObject.Properties.Name
    "$a : " + (($held | ForEach-Object { $_.Split('/')[-1] }) -join ', ')
}
```

**Expect:** `ops : id-airrec-operator-<env>`, `tools : id-airrec-tools-<env>` and
`worker : id-airrec-ops-<env>`, nothing else.

Optional: `tools\systems\roles_live_probe.py` in the workers (`-App $n.'app.worker'`) shows the
writer's tokens and roles against the five systems. **Expect:** for both token audiences, the
roles `Actions.Write`, `Booking.Read`, `Crew.Read`, `Schedule.Read` and `Slots.Read`; `schedule`,
`crew`, `booking` and `slots` answer 200, and `maintenance` answers 403 "lacks the app role
Maintenance.Read". That refusal is by design: the workers record defects in Maintenance, when a
plan runs, but never read it.

## 24. Evidence

Record the deployment in `docs\evidence\<descriptive-name>\evidence.json`: the commit, the
image digests, each agent's version and identity, both app registrations' client IDs, the
deployments and the smoke outcomes. No secrets, no tokens.

## Load test

Measures a full day's disruptions through one workers replica (`tools\load`). The volume follows
EUROCONTROL's CODA figures for 900 departures, as stated in `tools\load\scenario.py`: about 150
primary delays, 4 aircraft out of service and one crosswind forecast, 20% of them in a person's
own words; each report names a different aircraft. The sender posts them to the topic over
Service Bus's REST API as you, so it needs Azure Service Bus Data Sender on the topic for the
test's duration. A new role assignment can take minutes to take effect: the sender retries its
first report while Service Bus refuses it (it prints `HTTP 401` or `HTTP 403: waiting for the
Data Sender role to take effect`), for up to ten minutes, and starts the schedule once it is
accepted.

```powershell
$me    = az ad signed-in-user show --query id -o tsv
$topic = az servicebus topic show --subscription $sub -g $rg --namespace-name $n.serviceBus -n disruption-events --query id -o tsv
az role assignment create --subscription $sub --assignee-object-id $me --assignee-principal-type User --role "Azure Service Bus Data Sender" --scope $topic
& $py -m tools.load.send $e day --speed 8              # read-only: the run id and the schedule
& $py -m tools.load.send $e day --speed 8 --apply      # about 100 minutes; log .local\load\<run>.jsonl
& $py -m tools.load.send $e burst --run <run> --apply  # once the day's cases are decided: the rest at once
.\tools\systems\exec_py.ps1 -Environment $e -App $n.'app.ops' -Script tools\load\report.py -Arg <run> > .local\load\<run>.rows.txt
& $py -m tools.load.summarize $e <run> --out .local\load\<run>.figures.json
az role assignment delete --subscription $sub --assignee $me --role "Azure Service Bus Data Sender" --scope $topic
```

The figures per phase: reports sent, cases opened, how they ended (plans ready, executed,
failed, window ended), queue wait, assessment time and end-to-end latency (p50, p90, p95, max),
the largest backlog, decisions an hour and retries; over the run, the model tokens, HTTP 429
answers and the token cost. Reset the day afterwards.

## Cost and budget

Expected monthly cost of one environment with no cases, from Dev's actual cost by service (list
prices, Sweden Central); the step increments above add up to it:

| Service | USD a month |
|---|---|
| Container Apps: ARROW's three apps (operator, agent tools, workers) and five systems with one warm replica each, the Test Bench and jobs scaling to zero, the environment's load balancer | about 150 |
| SQL Database S2 | about 75 |
| Microsoft Defender for Cloud plans the subscription's policies enable (SQL and others) | about 55 |
| Container registry, private endpoint and DNS, Log Analytics, Service Bus Standard | about 40 |
| **Total without cases** | **about 320** |
| Models, per case assessed | about EUR 0.04 to 0.11 (measured in Dev, five cases: cost per case) |

ARROW's tools app and workers app add about USD 30 a month to the single ARROW app before them:
two more replicas of 0.5 vCPU and 1 GiB, USD 11.83 a month each when idle all month and USD 39.42
when active all month (Consumption list prices, Sweden Central, 25 November 2026); Dev's warm apps
cost about USD 15 each.

`budgetUsd` sets the budget `budget-airrec-<env>` (alerts at 50, 80 and 100% of actual cost and
100% of forecast, to your address) and the ceiling of both cost gates, which refuse a deployment
when the month's actual cost plus the forecast plus the stated increment exceeds it. A higher
ceiling needs the owner's approval (`deploy.ps1 -ApprovedCeilingUsd`).

## Updates and rollback

- **Images:** build as in step 11 (only the images whose source changed) and deploy each new
  digest with the same keys, in this order and with no case in flight: `migration` (and start the
  job) when the SQL files changed, then `system-*` and `jobs`, then ARROW's three apps (`tools`,
  `ops`, `worker`, all with the one `$ops` digest), then `testbench` (and step 22 again). An
  update adds no monthly cost and keeps ARROW's running sign-in secret (the default
  `--signin-secret current`; `new` would also need step 22 again). For ARROW alone:

  ```powershell
  foreach ($k in 'tools', 'ops', 'worker') { & $py -m tools.env.deploy_group $e $k --image $ops }   # previews
  foreach ($k in 'tools', 'ops', 'worker') { & $py -m tools.env.deploy_group $e $k --image $ops --apply --increment-usd 0 }
  ```

  Each preview shows one `Modify`, the app: the new image, plus settings the what-if prints as
  `[reference(...)]` because it cannot evaluate them before deploying; they keep their values.
  Then run step 21's checks. Each app runs one revision at a time, so its old one stops within a
  minute or so.
- **Agents:** edit the definition in `infra\ops-app` and run step 17 (`--agent <name>`, repeatable,
  publishes only those agents); each change becomes a new immutable version, which the agent's
  endpoint serves at once (all its traffic goes to `@latest`), with no ARROW redeploy. To roll an
  agent back, publish its previous definition again: it becomes the next version.
- **Templates:** run the template's key again; the gates show exactly what changes.
- **Sign-in secret:** `ops --signin-secret new`, then step 22.
- **Rollback:** deploy the previous digest (recorded in the evidence) with the same keys; publish
  the previous definition (from git) as a new version; deploy the previous commit's template. SQL
  migrations go forward only; data is restored with SQL point-in-time restore into a new database,
  which needs the owner's approval. To undo the move to three apps, see
  [Back to one ARROW app](#back-to-one-arrow-app).

### From one ARROW app to three

An environment deployed before ARROW ran as three apps has one app, `ca-airrec-ops-<env>`, running
as `id-airrec-ops-<env>` with ARROW's MCP tools and the workers in it. It moves to three in two
parts. The preparation only adds, while the one app keeps working. The window then moves the
agents to the tools app, turns the one app into the operator app and starts the workers. Hold it
with no case in flight: no case assessing or executing, no Case Assistant answer pending, the
Test Bench idle. The one app stops processing when the operator app's revision replaces it, and
the workers start only after that, so no case is assessed or executed by two apps. Reports and
wakes sent in the window wait in Service Bus (one hour to live), and an approval recorded in the
window runs when the workers start. The workers keep `id-airrec-ops`, the writer, so a plan
approved before, during or after the move runs under the same identity.

Build the images from the new commit (step 11) and set the session variables (step 1). Then:

**Preparation.** Each preview shows only `Create`, or a `Modify` that adds; never `REFUSED`.

1. Identities: step 7's `prerequisites` (the operator and tools identities, their AcrPull, the
   operator's Foundry User).
2. Database: step 12 with `$sys` (`migration`, then start the job). `021_three_apps.sql` adds the
   two apps' database users, their grants and two tables; expect `"files": 21`, `"tables": 28` and
   `"entra_users": 8`. Dev has no migration job: apply the file from the developer VM as the SQL
   admin instead:

   ```powershell
   $operatorClient = az identity show --subscription $sub -g $rg -n $n.'identity.operator' --query clientId -o tsv
   $toolsClient    = az identity show --subscription $sub -g $rg -n $n.'identity.tools' --query clientId -o tsv
   & $py tools\systems\apply_sql.py infra\systems\sql\021_three_apps.sql "OperatorClientId=$operatorClient" "ToolsClientId=$toolsClient"
   ```

3. Service Bus and telemetry: steps 15 and 10 (the `executions` queue and its roles; Monitoring
   Metrics Publisher for the two new identities, Monitoring Reader for the operator's).
4. App roles: step 19 (adds the operator's and the tools' reads; the workers' are unchanged).
5. Airline systems: step 20 with `$sys` (each adds its new readers; `WRITER_OIDS` unchanged;
   Booking adds `JOURNEY_READER_OIDS`, the tools app, which needs the systems image of this
   commit: without it the tools app prices no plan), and `jobs` with `$sys`.

**Window.** Tell the people using the environment first.

6. The tools app: step 21's `tools` lines and its two checks (`/healthz` 200, `/mcp` 401).
7. The agents to the tools app: step 16's `orchestrator-connections`, then step 17. The agents
   that call ARROW's tools get new versions with the tools app's address; record them.
8. The operator app. The what-if gate refuses removing a setting, an Easy Auth entry or an
   identity, so first remove what the operator app no longer holds, the agents' settings and Easy
   Auth entries and `ASSESSMENT_WORKERS` (this restarts the one app, which needs none of them):

   ```powershell
   $app    = az containerapp show --subscription $sub -g $rg -n $n.'app.ops' -o json | ConvertFrom-Json
   $old    = @($app.properties.template.containers[0].env | Where-Object { $_.name -like '*_AGENT_OIDS' -or $_.name -eq 'ASSESSMENT_WORKERS' })
   $agents = @($old | Where-Object { $_.name -like '*_AGENT_OIDS' } | ForEach-Object { $_.value -split ',' } | Where-Object { $_ })
   $old.name; $agents                                  # the settings and the agent identities it stops holding
   $auth   = "https://management.azure.com$($app.id)/authConfigs/current?api-version=2024-03-01"
   $config = az rest --method get --url $auth -o json | ConvertFrom-Json
   $policy = $config.properties.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy
   $policy.allowedApplications = @($policy.allowedApplications | Where-Object { $_ -notin $agents })
   $policy.allowedPrincipals.identities = @($policy.allowedPrincipals.identities | Where-Object { $_ -notin $agents })
   New-Item -ItemType Directory -Force .local | Out-Null
   @{ properties = $config.properties } | ConvertTo-Json -Depth 30 | Set-Content .local\ops-auth.json -Encoding utf8
   az rest --method put --url $auth --body '@.local\ops-auth.json' -o none
   az rest --method get --url $auth --query properties.identityProviders.azureActiveDirectory.validation.defaultAuthorizationPolicy -o json
   if ($old) { az containerapp update --subscription $sub -g $rg -n $n.'app.ops' --remove-env-vars @($old.name) -o none }
   ```

   **Expect:** `allowedApplications` holds ARROW's app registration and the Test Bench,
   `identities` the operators and the Test Bench; no agent. Then deploy the operator app with
   `--cutover`, the one deployment that may keep the writer's identity on it until the next
   command removes it (without the flag, `deploy_group` refuses while the operator app holds
   `id-airrec-ops` and names the command that removes it):

   ```powershell
   & $py -m tools.env.deploy_group $e ops --image $ops --cutover
   & $py -m tools.env.deploy_group $e ops --image $ops --cutover --apply --increment-usd 0
   ```

   Its preview
   adds `id-airrec-operator` and keeps `id-airrec-ops` (the template keeps the identities the live
   app holds), switches `AZURE_CLIENT_ID` and the registry to the operator identity and adds
   `APP_ROLE`, `EXECUTION_QUEUE`, `OPERATOR_PRINCIPAL_ID` and `TOOLS_PRINCIPAL_ID`. Then take the
   writer's identity away from it:

   ```powershell
   $writer = az identity show --subscription $sub -g $rg -n $n.'identity.ops' --query id -o tsv
   if (-not $writer) { throw "$($n.'identity.ops') not found: stop (an empty --user-assigned removes every identity)" }
   az containerapp identity remove --subscription $sub -g $rg -n $n.'app.ops' --user-assigned $writer -o none
   (az containerapp show --subscription $sub -g $rg -n $n.'app.ops' --query identity.userAssignedIdentities -o json | ConvertFrom-Json).PSObject.Properties.Name
   & $py -m tools.env.deploy_group $e ops --image $ops
   ```

   **Expect:** only `id-airrec-operator-<env>`, and a preview with no identity change and no
   `REFUSED` line. Then step 21's two checks for the operator app.
9. The workers: once the operator app runs one revision
   (`az containerapp revision list --subscription $sub -g $rg -n $n.'app.ops' --query "[?properties.active].name" -o tsv`
   lists one), step 21's `worker` lines and its checks.

Then the smoke test (step 23) with the write-refusal probe, and the evidence (step 24) with the
agents' new versions, the three apps' revisions and the identities.

### Back to one ARROW app

With no case in flight, in this checkout with its session variables (`$n` must come from this
commit, which knows the three apps), the previous ARROW digest `$prev` from the evidence, and a
detached worktree of the previous commit, where only that commit's `deploy_group` and
`publish_agents` run (with this `.venv`):

```powershell
git worktree add --detach ..\arrow-previous <previous commit>
$prevPy = (Resolve-Path $py).Path
```

1. Stop the workers: an app with no ingress and no scale rule stays at zero replicas once there
   (Microsoft Learn, *Set scaling rules in Azure Container Apps*, "Default scale rule"):
   `az containerapp update --subscription $sub -g $rg -n $n.'app.worker' --min-replicas 0 -o none`.
2. The agents back to the one app, with the previous commit's definitions (new versions):

   ```powershell
   Push-Location ..\arrow-previous
   & $prevPy -m tools.env.deploy_group $e orchestrator-connections
   & $prevPy -m tools.env.deploy_group $e orchestrator-connections --apply --increment-usd 0
   & $prevPy -m tools.env.publish_agents $e
   & $prevPy -m tools.env.publish_agents $e --apply
   Pop-Location
   ```

3. The operator app back to the one app, running as the writer again:

   ```powershell
   $writer   = az identity show --subscription $sub -g $rg -n $n.'identity.ops' --query id -o tsv
   $client   = az identity show --subscription $sub -g $rg -n $n.'identity.ops' --query clientId -o tsv
   $operator = az identity show --subscription $sub -g $rg -n $n.'identity.operator' --query id -o tsv
   if (-not ($writer -and $client -and $operator -and $prev)) { throw 'An identity or $prev is missing: stop (an empty --user-assigned removes every identity)' }
   $appUrl   = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.App/containerApps/$($n.'app.ops')?api-version=2024-03-01"
   az containerapp identity assign --subscription $sub -g $rg -n $n.'app.ops' --user-assigned $writer -o none
   @{ properties = @{ configuration = @{ registries = @(@{ server = $n.registryServer; identity = $writer }) } } } |
       ConvertTo-Json -Depth 10 | Set-Content .local\ops-registry.json -Encoding utf8
   az rest --method patch --url $appUrl --body '@.local\ops-registry.json' -o none
   az containerapp update --subscription $sub -g $rg -n $n.'app.ops' --image "$($n.registryServer)/airrec-ops-app@$prev" `
       --set-env-vars "AZURE_CLIENT_ID=$client" --remove-env-vars APP_ROLE EXECUTION_QUEUE OPERATOR_PRINCIPAL_ID TOOLS_PRINCIPAL_ID -o none
   az containerapp identity remove --subscription $sub -g $rg -n $n.'app.ops' --user-assigned $operator -o none
   Push-Location ..\arrow-previous
   & $prevPy -m tools.env.deploy_group $e ops --image $prev
   & $prevPy -m tools.env.deploy_group $e ops --image $prev --apply --increment-usd 0
   Pop-Location
   ```

   The last preview adds back the agents' settings and Easy Auth entries and
   `ASSESSMENT_WORKERS`; no `REFUSED` line. Then step 21's two checks for the operator app, and
   step 23.
4. The tools app may stay (nothing calls it) or stop at zero like the workers.

The new identities, grants, queue and tables and the systems' new readers stay: they only admit
more callers, and the one app ignores them. Removing them is a deletion and needs the owner's
approval.

### Later

- **Clean-up after the move:** the workers' identity keeps three grants only the one app needed:
  `INSERT` and `UPDATE` on `ops.policy_change` and `SELECT` on `audit.system_call` in SQL, and
  Monitoring Reader on Application Insights. Revoking them needs a new migration and the deletion
  of a role assignment (owner approval).
- **Scaling the workers on queue length:** one worker replica today. The next step is a
  managed-identity `azure-servicebus` scale rule on `assessments` with an identity of its own that
  holds Azure Service Bus Data Owner on that queue only (KEDA needs Manage rights to read a
  queue's length) and that the code cannot use, after a business test and a Dev run with two
  replicas.

## Teardown

Destructive: the owner approves first.

```powershell
$scope = "/subscriptions/$sub/resourceGroups/$rg"
$role  = az role definition list --subscription $sub --custom-role-only --scope $scope --query "[?roleName=='$($n.testbenchRole)'].name" -o tsv
$apps  = foreach ($a in $n.arrowAppRegistration, $n.systemsAppRegistration) { az ad app list --display-name $a --query "[].id" -o tsv }
$agents = az rest --method get --url "$($n.projectEndpoint)/agents?api-version=v1" --resource https://ai.azure.com --query "data[].name" -o tsv
foreach ($a in $agents) { az rest --method delete --url "$($n.projectEndpoint)/agents/$($a)?api-version=v1" --resource https://ai.azure.com }
az monitor log-analytics workspace delete --subscription $sub -g $rg -n $n.logAnalytics --force true --yes
az group delete --subscription $sub -n $rg --yes
az cognitiveservices account purge --subscription $sub --location $n.location -g $rg -n $n.foundryAccount
az role definition delete --subscription $sub --name $role --scope $scope
foreach ($id in $apps) {
    az ad app delete --id $id
    az rest --method delete --url "https://graph.microsoft.com/v1.0/directory/deletedItems/$id"   # permanently
}
```

The agents go first: Foundry keeps a deleted project's agents and gives them back, bound to their
deleted identities, to the next project of the same name. Even then a purged Foundry account's
name is not safe to reuse soon: Prd's account, re-created under the same name on 26 November,
could not run agents for at least seven hours (`DeploymentNotFound`, then `Subdomain does not map
to a resource`) although its models answered, so Prd's Foundry moved to a new name
(`aif-airrec-prd-<suffix>`). Redeploying an environment soon after a teardown needs another
Foundry account name, or Microsoft's confirmation that the old one is free. The workspace goes
next, with `--force`: deleted with the group it stays soft-deleted for 14
days, and deploying the environment again restores it with its old logs. Deleting the group (about
30 minutes) removes everything in it and its role assignments; the group's budget outlives it, and
deploying the environment again declares it unchanged. The Foundry account stays soft-deleted
until purged. The custom role definition outlives the group. The app registrations are deleted
permanently: a deleted one stays in the recycle bin for 30 days and keeps its unique name, so the
same environment could not be deployed again sooner.

Deleting the project does not reliably delete its agents' Entra objects: in Prd's teardown on 26
November it removed one of seven agent identities and none of their blueprints. Delete what is
left over Microsoft Graph (Entra's admin center cannot delete agent identities; both are soft
deletes, restorable for 30 days). This covers the project's shared agent identity too:

```powershell
$graph  = @{ Authorization = 'Bearer ' + (az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv) }
$prefix = "$($n.foundryAccount)-$($n.foundryProject)-"
$find   = { param($kind) (Invoke-RestMethod -Headers $graph -Uri "https://graph.microsoft.com/beta/$kind`?`$filter=startswith(displayName,'$prefix')&`$select=id,displayName").value }
foreach ($identity in (& $find servicePrincipals | Where-Object displayName -like '*-AgentIdentity')) {
    Invoke-RestMethod -Method Delete -Headers $graph -Uri "https://graph.microsoft.com/beta/servicePrincipals/$($identity.id)/microsoft.graph.agentIdentity"
}
foreach ($blueprint in (& $find applications | Where-Object displayName -like '*-AgentIdentityBlueprint')) {
    Invoke-RestMethod -Method Delete -Headers $graph -Uri "https://graph.microsoft.com/v1.0/applications/$($blueprint.id)"   # and its principal
}
@(& $find servicePrincipals).Count, @(& $find applications).Count   # 0, 0
```

The provider and feature registrations stay.

## Dev

Dev (`dev.json`, `rg-airrec-dev-swc`) predates this guide and differs in three ways:

- Its SQL Entra admin is the developer (`sqlAdministrator: developer`), and it is peered with the
  developer VM's network (`developerVnetId`): `infra\main.bicep` adds the workload side of the
  peering and links the SQL DNS zone to that network. `infra\devnet-peering.bicep` records the
  peering's other half, in the developer's subscription; nothing deploys it, and changing or removing it
  needs the owner's approval. Migrations are applied from the developer VM as the SQL admin:
  `.\.venv\Scripts\python.exe tools\systems\apply_sql.py infra\systems\sql\<file>.sql [Name=<client id> …]`;
  `tools\systems\load_day.py` replaces the day from there too. Dev has no migration job.
- Its app registrations predate unique names, so `dev.json` pins their client IDs.
- Its committed agent definitions are Dev's own: rendering them for Dev reproduces them exactly,
  and the templates' defaults are Dev's values, so a Dev what-if shows what it showed before the
  templates took an environment.

## The deployment gates

Every change passes ARM validation and an inspected incremental what-if, through
`tools\env\deploy.ps1` (`infra\main.bicep`) or `tools\env\deploy_group.py` (resource-group
templates). With `-Apply`/`--apply` both run the cost gate first and refuse a preview older than
60 minutes, a what-if that did not succeed, any difference from the preview, any `Delete`, any
change outside the environment's subscription, an `Unsupported` change not acknowledged in
`tools\env\unsupported-ack.json`, and every nested delete: an array element (an environment
variable, an Easy Auth list entry) or a property not pinned in `KNOWN_READ_ONLY_DELETES`. Remove
such an entry with a targeted command first, then make the template match:

- environment variable: `az containerapp update --subscription $sub -g $rg -n <app> --remove-env-vars <NAME>`;
- user-assigned identity: `az containerapp identity remove --subscription $sub -g $rg -n <app> --user-assigned <identity resource id>`
  (once no setting or registry of the app uses it);
- Easy Auth entry: GET
  `https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.App/containerApps/<app>/authConfigs/current?api-version=2024-03-01`
  with `az rest`, remove the entry, PUT back only its `properties`, GET to confirm.

`python -m tools.env.whatif same <a> <b>` compares two what-ifs, ignoring the order of records.
Agents, Entra objects, job starts and SQL migrations are not ARM resources: their scripts show a
read-only plan first and read back after `--apply`.

The preview features this path uses: `az containerapp job logs` (the log view of steps 12 and 13),
which needs the Azure CLI `containerapp` extension. That extension has only preview releases, and
once installed it also takes over core Azure CLI's other `az containerapp` commands (Azure CLI
warns *The behavior of this command has been altered by the following extension*); core Azure
CLI 2.89 has every other `az containerapp` command and option this guide uses. Also the
prerelease telemetry packages recorded in `infra\foundry\dependency-exceptions.json`.

## Offline infrastructure contract checks

Use Bicep CLI **0.46.1** and the locked Python environment. The compiler runs without signing in
or querying Azure (`--no-restore` keeps modules local); the placeholder values stay in the child
shell and the outputs in the ignored `.local\infra-contract`. CI runs the same build. Never deploy
with these parameters.

```powershell
pwsh -NoProfile -NonInteractive -Command {
    $ErrorActionPreference = 'Stop'
    $compiler = Join-Path $env:USERPROFILE '.azure\bin\bicep.exe'
    if ((& $compiler --version) -notmatch '^Bicep CLI version 0\.46\.1\b') {
        throw 'Offline contract checks require Bicep CLI 0.46.1.'
    }
    $env:AIRREC_DEV_OBJECT_ID = '00000000-0000-0000-0000-000000000001'
    $env:AIRREC_DEV_UPN = 'offline-contract@example.invalid'
    $env:AIRREC_DEV_BUDGET_EMAIL = 'offline-contract@example.invalid'
    $env:AIRREC_DEV_BUDGET_START_DATE = '2026-10-01'
    New-Item -ItemType Directory -Force .local\infra-contract | Out-Null
    & $compiler build infra\main.bicep --no-restore --outfile .local\infra-contract\main.json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $compiler build infra\devnet-peering.bicep --no-restore --outfile .local\infra-contract\devnet-peering.json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    foreach ($name in 'main', 'tools', 'worker', 'servicebus', 'prerequisites') {
        & $compiler build "infra\ops-app\$name.bicep" --no-restore --outfile ".local\infra-contract\ops-app-$name.json"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $compiler build-params infra\dev.bicepparam --no-restore --outfile .local\infra-contract\dev.parameters.json
    exit $LASTEXITCODE
}
& .\.venv\Scripts\python.exe -m pytest tests\infra -q -p no:cacheprovider
```

The tests check the compiled contracts (the platform and ARROW's three apps: who runs where, who is
admitted and who may write), the environment definitions and names, the wiring, the
migrations runner, the agent rendering and the gates with test inputs, not provider
acceptance, permissions or connectivity. The compiler warns `BCP081` (no local types for Network
`2026-05-01`) for the NSGs, the VNet, the peering, the SQL private endpoint and its DNS zone
group; the live what-if checks them.

