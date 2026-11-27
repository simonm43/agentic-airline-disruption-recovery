import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const directory = fileURLToPath(new URL('.', import.meta.url));
const cli = fileURLToPath(
  new URL('node_modules/@playwright/cli/playwright-cli.js', import.meta.url),
);
const session = `tooling-smoke-${process.pid}`;

function run(...args) {
  const result = spawnSync(process.execPath, [cli, `-s=${session}`, ...args], {
    cwd: directory,
    encoding: 'utf8',
  });
  if (result.error) throw result.error;
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  assert.equal(result.status, 0, `Browser command failed: ${args[0]}`);
}

const check = async (page) => {
  await page.setContent(`
    <title>Browser setup check</title>
    <button id="confirm">Confirm setup</button>
    <p id="status">Pending</p>
    <script>
      document.getElementById('confirm').addEventListener('click', () => {
        document.getElementById('status').textContent = 'Ready';
      });
    </script>
  `);
  await page.getByRole('button', { name: 'Confirm setup' }).click();
  const status = await page.locator('#status').textContent();
  const title = await page.title();
  if (status !== 'Ready') throw new Error('Browser interaction failed');
  if (title !== 'Browser setup check') throw new Error('Unexpected page title');
  return { title, status };
};

run('open', 'about:blank', '--config=cli.config.json', '--json');
try {
  run('run-code', check.toString(), '--json');
  run('screenshot', '--filename=.playwright-cli/setup-smoke.png', '--json');
  assert.ok(
    statSync(new URL('.playwright-cli/setup-smoke.png', import.meta.url)).size > 0,
    'Screenshot was not created',
  );
} finally {
  run('close');
}
