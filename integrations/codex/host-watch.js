#!/usr/bin/env node
const fs = require('fs');
const {
  WATCHER_PID_FILE,
  readSessionState,
  runEnsureServices,
  drainSpooledQueue,
  refreshSnapshots,
} = require('./common');

const INTERVAL_MS = Number(process.env.AGENT_MEMORY_CODEX_WATCH_INTERVAL_MS || 15000);
const SNAPSHOT_EVERY_MS = Number(process.env.AGENT_MEMORY_CODEX_SNAPSHOT_INTERVAL_MS || 60000);
let lastSnapshotAt = 0;
let stopping = false;

function writePid() {
  fs.mkdirSync(require('path').dirname(WATCHER_PID_FILE), { recursive: true });
  fs.writeFileSync(WATCHER_PID_FILE, String(process.pid), 'utf8');
}

function cleanup() {
  try { fs.unlinkSync(WATCHER_PID_FILE); } catch {}
}

async function tick() {
  const state = readSessionState();
  if (!state?.project_path) return;

  const recovery = runEnsureServices();
  if (!recovery.ok) return;

  await drainSpooledQueue().catch(() => {});

  const now = Date.now();
  if (now - lastSnapshotAt >= SNAPSHOT_EVERY_MS) {
    await refreshSnapshots({
      projectPath: state.project_path,
      projectName: state.project,
    }).catch(() => {});
    lastSnapshotAt = now;
  }
}

async function loop() {
  while (!stopping) {
    await tick();
    await new Promise((r) => setTimeout(r, INTERVAL_MS));
  }
}

process.on('SIGINT', () => { stopping = true; cleanup(); process.exit(0); });
process.on('SIGTERM', () => { stopping = true; cleanup(); process.exit(0); });
process.on('exit', cleanup);

writePid();
loop().catch(() => {
  cleanup();
  process.exit(1);
});
