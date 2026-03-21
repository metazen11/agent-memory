#!/usr/bin/env node
const { readSessionState, requestJson } = require('./common');

async function main() {
  const state = readSessionState();
  if (!state?.session_id) {
    console.log(JSON.stringify({ ok: true, skipped: 'no_session_state' }));
    return;
  }

  try {
    await requestJson('PATCH', `/api/sessions/${encodeURIComponent(state.session_id)}`, {
      status: 'completed',
    }, 3000);
    console.log(JSON.stringify({ ok: true, session_id: state.session_id }));
  } catch (e) {
    if (e.status === 404) {
      console.log(JSON.stringify({ ok: true, skipped: 'session_not_found', session_id: state.session_id }));
      return;
    }
    throw e;
  }
}

main().catch((e) => {
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
