#!/usr/bin/env node
// launchd invokes a bounded pass; crashes and offline dependencies are retried.
const fs = require('fs');
const path = require('path');
const os = require('os');
const { requestJson, runEnsureServices } = require('./common');
const { atomicJson, files, discover, drain } = require('./spool');
const root = process.env.AGENT_MEMORY_RECOVERY_ROOT || path.join(os.homedir(), '_CODING');
const statusFile = process.env.AGENT_MEMORY_RECOVERY_STATUS || path.join(os.homedir(), '.codex', 'agent-memory-recovery.json');

async function main() {
  const directories = discover(root);
  const pending = directories.flatMap(files).sort((a, b) => path.basename(a).localeCompare(path.basename(b)));
  const status = { at: new Date().toISOString(), root, pending: pending.length, directories: directories.length, drained: 0, quarantined: 0, error: null };
  atomicJson(statusFile, { ...status, phase: 'checking' });
  let reachable = false;
  try { reachable = (await requestJson('GET', '/api/health', null, 10000)).data?.db?.status === 'ok'; } catch {}
  if (!reachable) {
    const recovery = runEnsureServices();
    if (!recovery.ok) {
      atomicJson(statusFile, { ...status, phase: 'offline', error: 'service_recovery_failed' });
      return;
    }
    try { reachable = (await requestJson('GET', '/api/health', null, 10000)).data?.db?.status === 'ok'; } catch {}
  }
  if (!reachable) {
    atomicJson(statusFile, { ...status, phase: 'offline', error: 'database_unavailable' });
    return;
  }
  const result = await drain(pending, (route, payload) => requestJson('POST', route, payload, 10000), { limit: 40, delayMs: 800 });
  atomicJson(statusFile, { ...status, ...result, at: new Date().toISOString(), pending: directories.flatMap(files).length, phase: result.error ? 'retrying' : 'online' });
}
main().catch(error => {
  // Log counts/status only, never payloads or connection credentials.
  atomicJson(statusFile, { at: new Date().toISOString(), phase: 'error', error: error.code || 'worker_failed' });
  process.exitCode = 1;
});
