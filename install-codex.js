#!/usr/bin/env node
/**
 * agent-memory Codex adapter installer (separate from install.js)
 *
 * - Registers MCP server in Codex CLI config via `codex mcp add`
 * - Verifies local Codex adapter files exist
 * - Prints wrapper + hook/trigger usage for Codex
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname);
const PLATFORM = os.platform();
const PYTHON = path.join(ROOT, '.venv', PLATFORM === 'win32' ? 'Scripts' : 'bin', 'python');
const MCP_SERVER = path.join(ROOT, 'mcp_server.py');
const REQUIRED = [
  'integrations/codex/session-start.js',
  'integrations/codex/session-end.js',
  'integrations/codex/pre-tool-trigger.js',
  'integrations/codex/post-tool-hook.js',
  'integrations/codex/drain-spool.js',
  'integrations/codex/host-watch.js',
  'scripts/codex-agent-memory.sh',
  'codex.agent-memory.md',
];

function run(cmd, opts = {}) {
  const out = execSync(cmd, { encoding: 'utf8', stdio: opts.stdio || 'pipe' });
  return typeof out === 'string' ? out.trim() : '';
}

function exists(rel) {
  return fs.existsSync(path.join(ROOT, rel));
}

function checkFiles() {
  const missing = REQUIRED.filter((f) => !exists(f));
  if (missing.length) {
    console.error('Missing Codex adapter files:');
    for (const f of missing) console.error(`- ${f}`);
    process.exit(1);
  }
  if (!fs.existsSync(PYTHON)) {
    console.error(`Python venv not found at ${PYTHON}`);
    console.error('Run `node install.js` first (or create .venv manually).');
    process.exit(1);
  }
  if (!fs.existsSync(MCP_SERVER)) {
    console.error(`MCP server file not found: ${MCP_SERVER}`);
    process.exit(1);
  }
}

function ensureExecutableBits() {
  if (PLATFORM === 'win32') return;
  for (const rel of ['scripts/codex-agent-memory.sh']) {
    const fp = path.join(ROOT, rel);
    try {
      fs.chmodSync(fp, 0o755);
    } catch {}
  }
}

function registerMcp() {
  try {
    run('codex --version');
  } catch {
    console.error('Codex CLI not found in PATH.');
    process.exit(1);
  }

  try {
    run('codex mcp get agent-memory');
    run('codex mcp remove agent-memory');
  } catch {
    // ignore if not present
  }

  const cmd = `codex mcp add agent-memory -- "${PYTHON}" "${MCP_SERVER}"`;
  run(cmd, { stdio: 'inherit' });
}

function printSummary() {
  console.log('\nagent-memory Codex adapter installed');
  console.log('');
  console.log(`MCP: agent-memory -> ${PYTHON} ${MCP_SERVER}`);
  console.log(`Wrapper: ${path.join(ROOT, 'scripts', 'codex-agent-memory.sh')}`);
  console.log(`Installer wrapper: ${path.join(ROOT, 'scripts', 'install-agent-memory-codex.sh')}`);
  console.log(`Instructions: ${path.join(ROOT, 'codex.agent-memory.md')}`);
  console.log('');
  console.log('Sandbox-safe mode: wrapper starts a host watcher that auto-restarts services, drains spooled tool events, and refreshes lesson snapshots.');
  console.log('');
  console.log('Usage:');
  console.log('  ./scripts/install-agent-memory-codex.sh');
  console.log('  ./scripts/codex-agent-memory.sh');
  console.log('  node integrations/codex/pre-tool-trigger.js --tool Bash --input "..."');
  console.log('  node integrations/codex/post-tool-hook.js --tool Bash --input \'{"command":"..."}\' --output "..."');
}

function main() {
  const args = new Set(process.argv.slice(2));
  const skipMcp = args.has('--skip-mcp');
  checkFiles();
  ensureExecutableBits();
  if (!skipMcp) registerMcp();
  printSummary();
}

main();
