param(
    [Parameter(Mandatory)][string]$ImageTag,
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')][string]$Environment = 'dev'
)
# Builds the test bench image in the environment's registry from an isolated context; the image
# runs its tests, mypy and ruff.
$ErrorActionPreference = 'Stop'
if ($ImageTag -notmatch '^[a-z0-9][a-z0-9.-]{0,80}$') { throw 'Invalid image tag' }
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$names = (& $python (Join-Path $root 'tools\env\environment.py') $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unknown environment $Environment." }
$context = Join-Path $env:TEMP ("testbench-build-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $context, "$context\testbench", "$context\tests" | Out-Null
try {
    Copy-Item "$root\src\testbench\*.py", "$root\src\testbench\requirements.txt" "$context\testbench"
    Copy-Item "$root\src\testbench\static" "$context\testbench" -Recurse
    Copy-Item "$root\src\testbench\Dockerfile" $context
    Copy-Item "$root\tests\testbench\*.py" "$context\tests"
    Copy-Item "$root\tools\checks\requirements.txt" "$context\check-requirements.txt"
    & az acr build --subscription $names.subscriptionId `
        --registry $names.registry --image "airrec-testbench:$ImageTag" `
        --file "$context\Dockerfile" --platform linux --timeout 900 $context --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Test bench cloud build failed' }
} finally {
    Remove-Item -LiteralPath $context -Recurse -Force
}
