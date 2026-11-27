<#
.SYNOPSIS
Preview, and with -Apply deploy, one stage of infra\main.bicep to one environment.

.DESCRIPTION
infra\main.bicep is the shared platform ARROW runs on: the workload VNet (peered with the
developer network in Dev only), private SQL with its endpoint and DNS zone, the container
registry, Log Analytics with Application Insights, and the workload budget. The environment's
inputs are infra\environments\<environment>.json.

Without -Apply: run the read-only deployment preflight (tools\env\preflight.ps1), validate at
Provider level, write the what-if to .local\deploy\<environment>-<stage>.whatif.json and
summarise it. Nothing changes in Azure.

With -Apply: run the preflight and validation again, then the subscription-wide cost gate
with -IncrementUsd, a fresh what-if (.local\deploy\<environment>-<stage>.apply.whatif.json) and
the what-if gate against the preview from the run without -Apply (which must be under 60
minutes old and identical). Any failed check stops the script before the deployment starts.

The cost gate's Cost Management queries go through the fetch in tools\env\costgate.py, which retries
only throttling (HTTP 429), waits as long as Cost Management's retry-after hints ask, and
gives up after 10 minutes per query; any failure refuses the deployment (D36). The ceiling is
the environment's budgetUsd unless -ApprovedCeilingUsd records a higher owner-approved one.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9]+(-[a-z0-9]+)*$')]
    [string]$Stage,
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')]
    [string]$Environment = 'dev',
    [ValidateRange(0, 10000)]
    [double]$IncrementUsd,
    [ValidateRange(1, 100000)]
    [double]$ApprovedCeilingUsd,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$python = '.\.venv\Scripts\python.exe'

function Assert-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE." }
}

function Write-Utf8([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) $Path), $Text, [System.Text.UTF8Encoding]::new($false))
}

if ($Apply -and -not $PSBoundParameters.ContainsKey('IncrementUsd')) {
    throw "-Apply needs -IncrementUsd, the stage's expected monthly cost increase (0 when it only re-applies the platform)."
}

Push-Location (Resolve-Path (Join-Path $PSScriptRoot '..\..'))
try {
    $definition = Get-Content -Raw -Encoding utf8 "infra\environments\$Environment.json" | ConvertFrom-Json
    $subscription = [string]$definition.subscriptionId
    $location = [string]$definition.location
    $deployment = "airrec-$Environment-$Stage"
    $parameters = "@.local\$Environment.parameters.json"
    $ceiling = if ($PSBoundParameters.ContainsKey('ApprovedCeilingUsd')) { $ApprovedCeilingUsd } else { [double]$definition.budgetUsd }
    New-Item -ItemType Directory -Force .local\deploy | Out-Null

    $account = az account show --subscription $subscription --query '{id:id, state:state}' -o json | ConvertFrom-Json
    Assert-Exit "Reading the $Environment subscription"
    if ($account.id -ne $subscription -or $account.state -ne 'Enabled') {
        throw "The $Environment subscription is not available and enabled for this identity."
    }

    # 1. The deployment preflight writes .local\<environment>.parameters.json; exit 2 lists NEEDS_CONTEXT.
    & pwsh -NoProfile -NonInteractive -File tools\env\preflight.ps1 -Environment $Environment
    if ($LASTEXITCODE -notin 0, 2) { throw "The deployment preflight failed with exit code $LASTEXITCODE." }
    if (-not (Test-Path ".local\$Environment.parameters.json")) { throw "The preflight wrote no .local\$Environment.parameters.json." }

    # 2. Provider-level validation.
    az deployment sub validate --subscription $subscription --location $location `
        --name $deployment --template-file infra\main.bicep `
        --parameters $parameters `
        --validation-level Provider -o none
    Assert-Exit 'Validation'

    $preview = ".local\deploy\$Environment-$Stage.whatif.json"
    if (-not $Apply) {
        # 3. The preview the apply is later gated against.
        $whatIf = az deployment sub what-if --subscription $subscription --location $location `
            --name $deployment --template-file infra\main.bicep `
            --parameters $parameters `
            --no-pretty-print -o json
        Assert-Exit 'What-if'
        Write-Utf8 $preview ($whatIf -join "`n")
        & $python -m tools.env.whatif summary $preview
        Assert-Exit 'What-if summary'
        Write-Output "Preview written to $preview. Nothing was deployed. Re-run with -Apply within 60 minutes."
        return
    }
    if (-not (Test-Path $preview)) { throw "No preview at ${preview}: run without -Apply first." }

    # 4a. The subscription-wide cost gate (Task 1 Step 8 rule).
    & $python -m tools.env.costgate bodies --out-dir .local\deploy
    Assert-Exit 'Building the cost queries'
    Remove-Item .local\deploy\cost-actual.json, .local\deploy\cost-forecast.json -ErrorAction SilentlyContinue
    # Retries only throttling, as long as Cost Management's retry-after hints ask, for at most
    # 10 minutes a query (D36). A failed fetch writes no file, so the gate below refuses.
    & $python -m tools.env.costgate fetch --subscription $subscription --url "https://management.azure.com/subscriptions/$subscription/providers/Microsoft.CostManagement/query?api-version=2025-03-01" --body .local\deploy\cost-query.json --out .local\deploy\cost-actual.json
    if ($LASTEXITCODE -eq 0) {
        & $python -m tools.env.costgate fetch --subscription $subscription --url "https://management.azure.com/subscriptions/$subscription/providers/Microsoft.CostManagement/forecast?api-version=2025-03-01" --body .local\deploy\cost-forecast-body.json --out .local\deploy\cost-forecast.json
    }
    $costArguments = @('--actual', '.local\deploy\cost-actual.json', '--forecast', '.local\deploy\cost-forecast.json',
        '--increment-usd', [string]$IncrementUsd, '--approved-ceiling-usd', [string]$ceiling)
    & $python -m tools.env.costgate check @costArguments
    Assert-Exit 'The cost gate'

    # 4b. A fresh what-if must repeat the preview exactly.
    $current = ".local\deploy\$Environment-$Stage.apply.whatif.json"
    $whatIf = az deployment sub what-if --subscription $subscription --location $location `
        --name $deployment --template-file infra\main.bicep `
        --parameters $parameters `
        --no-pretty-print -o json
    Assert-Exit 'What-if'
    Write-Utf8 $current ($whatIf -join "`n")
    & $python -m tools.env.whatif summary $current
    Assert-Exit 'What-if summary'
    & $python -m tools.env.whatif gate $preview $current --subscription $subscription
    Assert-Exit 'The what-if gate'

    # 4c. Deploy.
    $outputs = az deployment sub create --subscription $subscription --location $location `
        --name $deployment --template-file infra\main.bicep `
        --parameters $parameters `
        --query properties.outputs -o json
    Assert-Exit "Deployment $deployment"
    Write-Output "Deployment $deployment succeeded. Outputs:"
    Write-Output ($outputs -join "`n")
}
finally {
    Pop-Location
}
