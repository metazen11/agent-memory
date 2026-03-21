#!/usr/bin/env node
const {
  readSessionState,
  runEnsureServices,
  drainSpooledQueue,
  refreshSnapshots,
} = require('./common');

async function main() {
  const state = readSessionState();
  if (!state?.project_path) {
    console.log(JSON.stringify({ ok: true, skipped: 'no_session_state' }));
    return;
  }

  const allowHostRecovery = process.env.AGENT_MEMORY_CODEX_HOST_RECOVERY === '1';
  const recovery = allowHostRecovery
    ? runEnsureServices()
    : { ok: false, status: null, stdout: '', stderr: '' };
  let drained = 0;
  let snapshots = false;
  if (recovery.ok) {
    drained = await drainSpooledQueue();
    await refreshSnapshots({
      projectPath: state.project_path,
      projectName: state.project,
    }).catch(() => {});
    snapshots = true;
  }

  console.log(JSON.stringify({
    ok: true,
    drained,
    snapshots,
    mode: allowHostRecovery ? (recovery.ok ? 'host-online' : 'host-recovery-failed') : 'sandbox-no-recovery',
    recovery_status: recovery.status ?? null,
  }));
}

main().catch((e) => {
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
