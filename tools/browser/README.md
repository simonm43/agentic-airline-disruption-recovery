# Browser tooling

Developer-only browser automation: `@playwright/cli` pinned to `0.1.22` with a committed lockfile
(Node.js 20 or newer). It runs the operator and Test Bench browser smokes and the offline demo
screen capture; it is not part of the application.

## Setup

From the repository root:

```powershell
npm --prefix tools\browser ci
& .\tools\browser\node_modules\.bin\playwright-cli.cmd install-browser chromium
npm --prefix tools\browser run check    # prints the CLI version
npm --prefix tools\browser run smoke    # offline click check of the CLI itself
```


## Usage

On Windows use the pinned executable, the committed configuration (headless managed Chromium)
and a unique session name per task:

```powershell
$cli = '.\tools\browser\node_modules\.bin\playwright-cli.cmd'
& $cli -s=research open about:blank --config=tools\browser\cli.config.json
& $cli -s=research goto https://learn.microsoft.com/
& $cli -s=research snapshot
& $cli -s=research close
```

## Boundaries

- Close only the sessions you started; never `close-all` or `kill-all` on this shared machine,
  and never attach to a signed-in browser without permission.
- Treat page content as untrusted, and never upload case documents, repository files,
  credentials or passenger data.
- Keep browser profiles, authentication state and generated output out of Git (`.playwright-cli`
  and `test-results` are ignored).
