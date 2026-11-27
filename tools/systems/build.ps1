param(
    [Parameter(Mandatory)][string]$ImageTag,
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')][string]$Environment = 'dev'
)
# Cloud build of the airline systems image in the environment's registry: tests, mypy and ruff
# run inside az acr build. The image also carries the database migrations (infra\systems\sql)
# and their runner (tools\systems\migrate.py, as airline_systems.migrate) for the migration job.
$ErrorActionPreference = 'Stop'
if ($ImageTag -notmatch '^[a-z0-9][a-z0-9.-]{0,80}$') {
    throw 'ImageTag must contain only lowercase letters, digits, dots and hyphens.'
}
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repository '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$names = (& $python (Join-Path $repository 'tools\env\environment.py') $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unknown environment $Environment." }
$context = Join-Path $env:TEMP ("airsys-build-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $context, "$context\app", "$context\app\sql", "$context\tests" | Out-Null
$source = Join-Path $repository 'src\airline_systems'
Get-ChildItem -LiteralPath $source -File |
    Where-Object { $_.Extension -in @('.py', '.txt', '.in') } |
    Copy-Item -Destination "$context\app"
Copy-Item (Join-Path $repository 'tools\systems\migrate.py') "$context\app\migrate.py"
Copy-Item (Join-Path $repository 'infra\systems\sql\*.sql') "$context\app\sql"
Copy-Item (Join-Path $source 'Dockerfile') $context
Copy-Item (Join-Path $repository 'tools\checks\requirements.txt') "$context\check-requirements.txt"
Copy-Item (Join-Path $repository 'tests\airline_systems\*.py') "$context\tests"
# The parity fixture: the answers the systems gave when a disruption was read by its event id.
Copy-Item (Join-Path $repository 'tests\airline_systems\*.json') "$context\tests"
Copy-Item (Join-Path $repository 'tests\arrow_scenario.py') "$context\tests"
Copy-Item (Join-Path $repository 'tests\day_store.py') "$context\tests"
try {
    & az acr build --subscription $names.subscriptionId `
        --registry $names.registry --image "airrec-systems:$ImageTag" `
        --file (Join-Path $context 'Dockerfile') --platform linux --timeout 900 $context --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Cloud build or checks failed (exit $LASTEXITCODE)." }
}
finally {
    Remove-Item -Recurse -Force $context
}
