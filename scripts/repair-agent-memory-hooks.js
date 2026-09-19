#!/usr/bin/env node
/**
 * Repair local host wiring for agent-memory without running the full installer.
 *
 * This is intentionally idempotent: it symlinks hook shims from the current
 * checkout, registers missing hook entries, and rewrites stale Dropbox MCP
 * paths in Codex config to the operational checkout.
 */
const os = require('os');
const path = require('path');
const {
  createHostWiring,
  installHookSymlinks,
  registerHookEntries,
  repairCodexMcpPath,
  unregisterHookEntries,
} = require('./lib/agent-memory-host-wiring');

const ROOT = path.resolve(__dirname, '..');
const HOME = os.homedir();
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');
const MCP_SERVER = path.join(ROOT, 'mcp_server.py');
const WIRING = createHostWiring({ root: ROOT, home: HOME });

function repairHost(host) {
  if (!host.detect()) return { name: host.name, skipped: true };
  installHookSymlinks(host, ROOT);
  const added = registerHookEntries(host.settingsFile, host.hookEntries);
  if (host.name === 'codex') {
    const sessionEnd = host.hookEntries.find((item) => item.event === 'SessionEnd');
    if (sessionEnd) unregisterHookEntries(host.settingsFile, [{ ...sessionEnd, event: 'Stop' }]);
  }
  return { name: host.name, skipped: false, symlinks: host.hookFiles.length, added };
}

function main() {
  const results = [WIRING.claude, WIRING.codex].map(repairHost);
  const codexMcpRewritten = repairCodexMcpPath({
    configFile: WIRING.codex.configFile,
    python: PYTHON,
    server: MCP_SERVER,
  });
  console.log(JSON.stringify({ root: ROOT, results, codexMcpRewritten }, null, 2));
}

main();
