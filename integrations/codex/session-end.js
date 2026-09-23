#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { readSessionState, requestJson } = require('./common');

/**
 * Fire-and-forget background tasks: drain any spooled queue payloads and
 * ingest Codex user prompts from ~/.codex/history.jsonl (Codex has no
 * UserPromptSubmit hook, so history.jsonl is the only prompt source).
 *
 * Detached + unref'd so this can never block session teardown or write to
 * this hook's stdout — the hook's stdout is a strict JSON contract.
 */
function spawnBackgroundTask(scriptName) {
  try {
    const child = spawn(process.execPath, [path.join(__dirname, scriptName)], {
      detached: true,
      stdio: 'ignore',
      cwd: process.cwd(),
    });
    child.unref();
  } catch { /* best-effort; never fail session teardown */ }
}

function readHookEvent() {
  if (process.stdin.isTTY) return null;
  try {
    const raw = fs.readFileSync(0, 'utf8').trim();
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

async function main() {
  const hookMode = !!readHookEvent();

  // Kick these off regardless of session state: the spool and the prompt
  // history both outlive any single session.
  spawnBackgroundTask('drain-spool.js');
  spawnBackgroundTask('ingest-history.js');

  const state = readSessionState();
  if (!state?.session_id) {
    console.log(JSON.stringify(hookMode ? {} : { ok: true, skipped: 'no_session_state' }));
    return;
  }

  try {
    await requestJson('PATCH', `/api/sessions/${encodeURIComponent(state.session_id)}`, {
      status: 'completed',
    }, 3000);
    console.log(JSON.stringify(hookMode ? {} : { ok: true, session_id: state.session_id }));
  } catch (e) {
    if (e.status === 404) {
      console.log(JSON.stringify(hookMode ? {} : { ok: true, skipped: 'session_not_found', session_id: state.session_id }));
      return;
    }
    if (hookMode) {
      console.error(`[agent-memory:session-end] ${e.message || String(e)}`);
      return;
    }
    throw e;
  }
}

main().catch((e) => {
  if (!process.stdin.isTTY) {
    console.error(`[agent-memory:session-end] ${e.message || String(e)}`);
    console.log(JSON.stringify({}));
    return;
  }
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
