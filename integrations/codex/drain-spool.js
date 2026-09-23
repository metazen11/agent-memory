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

  // Draining is gated on the API being REACHABLE, not on service *recovery*
  // having run. Previously both were coupled, so a perfectly healthy service
  // drained nothing unless AGENT_MEMORY_CODEX_HOST_RECOVERY=1 was set (which
  // only the wrapper sets) — leaving spool files stranded indefinitely.
  // Recovery is now a fallback: try the drain first, and only attempt to
  // start services if the first attempt got us nowhere.
  const allowHostRecovery = process.env.AGENT_MEMORY_CODEX_HOST_RECOVERY === '1';
  let drained = 0;
  let snapshots = false;
  let recovery = { ok: false, status: null, stdout: '', stderr: '' };

  drained = await drainSpooledQueue().catch(() => 0);

  if (drained === 0 && allowHostRecovery) {
    recovery = runEnsureServices();
    if (recovery.ok) {
      drained = await drainSpooledQueue().catch(() => 0);
    }
  }

  if (drained > 0 || recovery.ok) {
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
