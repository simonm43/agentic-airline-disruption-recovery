param(
    [Parameter(Mandatory)][string]$App,
    [Parameter(Mandatory)][string]$Script,
    [string]$Arg = '',
    [ValidatePattern('^[a-z][a-z0-9]{1,5}$')][string]$Environment = 'dev'
)
# Runs a Python file inside one of the environment's Container App replicas as one az containerapp
# exec session (read-only checks run as that app's identity, inside the workload network).
# The file is stripped of docstrings and comments, zlib-compressed and inlined: the exec command
# must stay under about 2,048 URL-encoded characters, and the Windows az wrapper cannot pass
# double quotes. Exec is rate-limited (about 8 sessions per 10 minutes).
$ErrorActionPreference = 'Stop'
if ($Arg -notmatch '^[A-Za-z0-9_-]*$') { throw 'Arg must be a single plain word.' }
$python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
$pack = @'
import ast, base64, sys, zlib
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
for node in ast.walk(tree):
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
        if isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
print(base64.b64encode(zlib.compress(ast.unparse(tree).encode(), 9)).decode())
'@
$b64 = $pack | & $python - (Resolve-Path $Script).Path
if ($LASTEXITCODE -ne 0) { throw 'Could not pack the script.' }
$command = "python -c exec(__import__('zlib').decompress(__import__('base64').b64decode('$b64'))) $Arg".Trim()
$encoded = [uri]::EscapeDataString($command).Length
Write-Host "exec command: $encoded URL-encoded characters"
if ($encoded -gt 1900) { throw "Command too long for az containerapp exec ($encoded)." }
$names = (& $python (Join-Path $PSScriptRoot '..\env\environment.py') $Environment) -join "`n" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Unknown environment $Environment." }
az containerapp exec --subscription $names.subscriptionId -g $names.resourceGroup `
    -n $App --command $command
