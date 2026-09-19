#!/usr/bin/env node
const { readSessionState, postQueuePayload, saveSpooledQueuePayload } = require('./common');
const fs = require('fs');

function readStdinEvent() {
  if (process.stdin.isTTY) return null;
  try {
    const raw = fs.readFileSync(0, 'utf8').trim();
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function parseArgs(argv) {
  const event = readStdinEvent();
  const args = {
    tool: event?.tool_name || '',
    input: event?.tool_input == null ? '' : JSON.stringify(event.tool_input),
    output: event?.tool_response == null ? '' : JSON.stringify(event.tool_response),
    success: '',
    error: event?.tool_error || '',
    cwd: event?.cwd || process.cwd(),
    session: event?.session_id || process.env.AGENT_MEMORY_SESSION_ID || '',
    hookMode: !!event,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--tool') args.tool = argv[++i] || '';
    else if (a === '--input') args.input = argv[++i] || '';
    else if (a === '--output') args.output = argv[++i] || '';
    else if (a === '--success') args.success = argv[++i] || '';
    else if (a === '--error') args.error = argv[++i] || '';
    else if (a === '--cwd') args.cwd = argv[++i] || args.cwd;
    else if (a === '--session') args.session = argv[++i] || args.session;
  }
  return args;
}

function parseJsonLoose(value) {
  if (!value) return null;
  try { return JSON.parse(value); } catch { return { raw: value }; }
}

function parseBoolLoose(value) {
  if (!value) return null;
  const v = String(value).trim().toLowerCase();
  if (v === '1' || v === 'true' || v === 'yes') return true;
  if (v === '0' || v === 'false' || v === 'no') return false;
  return null;
}

function asErrorText(value) {
  if (!value) return null;
  if (typeof value === 'string') return value.slice(0, 2000);
  try { return JSON.stringify(value).slice(0, 2000); } catch { return String(value).slice(0, 2000); }
}

async function main() {
  const args = parseArgs(process.argv);
  const state = readSessionState();
  const sessionId = args.session || state?.session_id || (args.hookMode ? `codex-hook-${Date.now()}` : '');
  if (!sessionId) {
    if (args.hookMode) {
      console.log(JSON.stringify({}));
      return;
    }
    console.error('No session id found. Run session-start first or pass --session.');
    process.exit(2);
  }

  const payload = {
    session_id: sessionId,
    hook_event_name: 'PostToolUse',
    tool_name: args.tool || null,
    tool_input: parseJsonLoose(args.input),
    tool_response: parseJsonLoose(args.output),
    tool_response_preview: (args.output || '').slice(0, 2000) || null,
    tool_success: null,
    tool_error: null,
    raw_event: {
      tool_name: args.tool || null,
      tool_input: parseJsonLoose(args.input),
      tool_response: parseJsonLoose(args.output),
      cwd: args.cwd || process.cwd(),
      session_id: sessionId,
    },
    cwd: args.cwd || process.cwd(),
    last_user_message: null,
    source_system: 'codex-cli',
    source_mode: 'hook',
    source_agent: 'codex-cli',
  };

  const successFlag = parseBoolLoose(args.success);
  if (successFlag !== null) payload.tool_success = successFlag;
  if (args.error) payload.tool_error = args.error.slice(0, 2000);
  if (payload.tool_error == null && payload.tool_response && typeof payload.tool_response === 'object' && payload.tool_response.error) {
    payload.tool_error = asErrorText(payload.tool_response.error);
  }
  if (payload.tool_success == null && payload.tool_error) payload.tool_success = false;

  try {
    await postQueuePayload(payload, 1200);
    console.log(JSON.stringify(args.hookMode ? {} : { ok: true, queued: true, session_id: sessionId, mode: 'online' }));
  } catch {
    const file = saveSpooledQueuePayload(payload);
    console.log(JSON.stringify(args.hookMode ? {} : { ok: true, queued: true, session_id: sessionId, mode: 'spooled', file }));
  }
}

main().catch((e) => {
  if (!process.stdin.isTTY) {
    console.log(JSON.stringify({}));
    process.exit(0);
  }
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
