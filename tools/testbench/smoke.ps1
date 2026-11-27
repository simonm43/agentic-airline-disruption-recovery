# Test bench browser smoke against mocked /api routes. Needs, from the repository root:
#   .venv\Scripts\python.exe -m http.server 8768 --bind 127.0.0.1
$cli = ".\tools\browser\node_modules\.bin\playwright-cli.cmd"
& $cli -s=testbench-smoke open 'about:blank?base=http://127.0.0.1:8768' --config=tools\browser\cli.config.json | Out-Null
$out = & $cli -s=testbench-smoke run-code --filename=tests\testbench\browser\smoke.js 2>&1 | Out-String
& $cli -s=testbench-smoke close | Out-Null
$i = $out.IndexOf("### Ran Playwright code"); if ($i -gt 0) { $out = $out.Substring(0, $i) }
Write-Output $out.Trim()
if ($out -match "### Error" -or $out -notmatch "### Result") { exit 1 } else { exit 0 }
