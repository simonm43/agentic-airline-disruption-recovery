# Operator app browser smokes against mocked /api routes. Needs, from the repository root:
#   .venv\Scripts\python.exe -m http.server 8768 --bind 127.0.0.1
# On a shared machine, give a run its own port and browser session: -Port 8791 -Session my-smoke.
param([int]$Port = 8768, [string]$Session = "ops-smoke", [string[]]$Smokes = @("smoke.js", "map_smoke.js"))
$cli = ".\tools\browser\node_modules\.bin\playwright-cli.cmd"
$failed = $false
foreach ($smoke in $Smokes) {
  & $cli "-s=$Session" open "about:blank?base=http://127.0.0.1:$Port" --config=tools\browser\cli.config.json | Out-Null
  $out = & $cli "-s=$Session" run-code --filename=tests\ops_app\browser\$smoke 2>&1 | Out-String
  & $cli "-s=$Session" close | Out-Null
  $i = $out.IndexOf("### Ran Playwright code"); if ($i -gt 0) { $out = $out.Substring(0, $i) }
  Write-Output "== $smoke"
  Write-Output $out.Trim()
  if ($out -match "### Error" -or $out -notmatch "### Result") { $failed = $true }
}
if ($failed) { exit 1 } else { exit 0 }
