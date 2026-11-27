param(
    [Parameter(Mandatory)][string]$ImageTag,
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')][string]$Environment = 'dev'
)
# Cloud build of ARROW (the ops app) in the environment's registry from an isolated context; the
# image runs its tests, mypy and ruff.
$ErrorActionPreference = 'Stop'
if ($ImageTag -notmatch '^[a-z0-9][a-z0-9.-]{0,80}$') { throw 'Invalid image tag' }
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$names = (& $python (Join-Path $root 'tools\env\environment.py') $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unknown environment $Environment." }
$context = Join-Path $env:TEMP ("ops-build-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $context, "$context\airline_systems", "$context\ops_app", "$context\tests" | Out-Null
try {
    Copy-Item "$root\src\airline_systems\*.py", "$root\src\airline_systems\requirements.txt" "$context\airline_systems"
    Copy-Item "$root\src\ops_app\*.py" "$context\ops_app"
    Copy-Item "$root\src\ops_app\static" "$context\ops_app" -Recurse
    Copy-Item "$root\src\ops_app\Dockerfile" $context
    Copy-Item "$root\tests\ops_app\*.py" "$context\tests"
    Copy-Item "$root\tests\arrow_scenario.py" "$context\tests"
    Copy-Item "$root\tests\day_store.py" "$context\tests"
    Copy-Item "$root\tools\checks\requirements.txt" "$context\check-requirements.txt"
    & az acr build --subscription $names.subscriptionId `
        --registry $names.registry --image "airrec-ops-app:$ImageTag" `
        --file "$context\Dockerfile" --platform linux --timeout 900 $context --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Ops app cloud build failed' }
} finally {
    Remove-Item -LiteralPath $context -Recurse -Force
}
