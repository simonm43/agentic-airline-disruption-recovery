param([ValidatePattern('^[a-z][a-z0-9]{1,5}$')][string]$Environment = 'dev')
# Regenerates src\testbench\requirements.txt in the cloud (ACR run), on the image's Python.
$ErrorActionPreference = 'Stop'
$python = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$names = (& $python (Join-Path $PSScriptRoot '..\env\environment.py') $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unknown environment $Environment." }
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$context = Join-Path $env:TEMP ("testbench-lock-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $context | Out-Null
Copy-Item (Join-Path $repository 'src\testbench\requirements.in') (Join-Path $context 'requirements.in')
Copy-Item (Join-Path $repository 'tools\systems\lock.py') $context
$image = 'python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc'
"version: v1.1.0`nsteps:`n  - cmd: $image python lock.py`n" |
    Set-Content (Join-Path $context 'acb.yaml') -Encoding utf8NoBOM
$log = & az acr run --subscription $names.subscriptionId `
    --registry $names.registry -f acb.yaml $context --only-show-errors 2>&1
Remove-Item -Recurse -Force $context
$text = ($log | Out-String)
if ($text -notmatch '(?s)----LOCK-BEGIN----\s*(.*?)\s*----LOCK-END----') { $text; throw 'lock run failed' }
$lines = @('# Linux CPython 3.13 lock from tools\testbench\lock.ps1 (ACR run); one wheel hash each.') +
    ($Matches[1] -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '==' })
$lines | Set-Content (Join-Path $repository 'src\testbench\requirements.txt') -Encoding utf8NoBOM
"locked $($lines.Count - 1) packages"
