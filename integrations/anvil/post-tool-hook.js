#!/usr/bin/env node
/**
 * agent-memory Anvil PostTool hook adapter
 *
 * Accepts stdin JSON (or args) and forwards a rich queue payload to /api/queue.
 * Designed for cross-platform use when wiring Anvil lifecycle hooks externally.
 */

const http = require('http');

const SERVER_BASE = process.env.AGENT_MEMORY_SERVER || 'http://localhost:3377';

function parseArgs(argv) {
  const args = {
    tool: '',
    input: '',
    output: '',
    session: process.env.AGENT_MEMORY_SESSION_ID || '',
    cwd: process.cwd(),
    success: '',
    error: '',
    user_message: '',
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--tool') args.tool = argv[++i] || '';
    else if (a === '--input') args.input = argv[++i] || '';
    else if (a === '--output') args.output = argv[++i] || '';
    else if (a === '--session') args.session = argv[++i] || '';
    else if (a === '--cwd') args.cwd = argv[++i] || args.cwd;
    else if (a === '--success') args.success = argv[++i] || '';
    else if (a === '--error') args.error = argv[++i] || '';
    else if (a === '--user-message') args.user_message = argv[++i] || '';
  }
  return args;
}

function parseJsonLoose(value) {
  if (!value) return null;
  try { return JSON.parse(value); } catch { return value; }
}

function parseBoolLoose(value) {
  if (!value) return null;
  const v = String(value).trim().toLowerCase();
  if (v === '1' || v === 'true' || v === 'yes') return true;
  if (v === '0' || v === 'false' || v === 'no') return false;
  return null;
}

function readStdinJson() {
  try {
    const raw = require('fs').readFileSync(0, 'utf8').trim();
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function postQueue(payload) {
  return new Promise((resolve, reject) => {
    const url = new URL('/api/queue', SERVER_BASE);
    const body = JSON.stringify(payload);
    const req = http.request({
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 2000,
    }, (res) => {
      let data = '';
      res.on('data', (d) => { data += d; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve({ status: res.statusCode, body: data });
          return;
        }
        reject(new Error(`POST /api/queue failed: ${res.statusCode} ${data}`));
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('POST /api/queue timeout')));
    req.write(body);
    req.end();
  });
}

async function main() {
  const stdinEvent = readStdinJson();
  const args = parseArgs(process.argv);

  const sessionId = (stdinEvent && stdinEvent.session_id) || args.session;
  if (!sessionId) {
    console.error('No session_id provided');
    process.exit(2);
  }

  const toolInput = (stdinEvent && stdinEvent.tool_input) || parseJsonLoose(args.input);
  const toolResponse = (stdinEvent && stdinEvent.tool_response) || parseJsonLoose(args.output);
  const preview = typeof toolResponse === 'string'
    ? toolResponse.slice(0, 2000)
    : JSON.stringify(toolResponse || '').slice(0, 2000);

  const payload = {
    session_id: sessionId,
    hook_event_name: 'PostToolUse',
    tool_name: (stdinEvent && stdinEvent.tool_name) || args.tool || null,
    tool_input: toolInput || null,
    tool_response: toolResponse || null,
    tool_response_preview: preview || null,
    tool_success: (stdinEvent && typeof stdinEvent.tool_success === 'boolean')
      ? stdinEvent.tool_success
      : parseBoolLoose(args.success),
    tool_error: (stdinEvent && stdinEvent.tool_error) || (args.error ? args.error.slice(0, 2000) : null),
    raw_event: stdinEvent || {
      tool_name: args.tool || null,
      tool_input: toolInput || null,
      tool_response: toolResponse || null,
      cwd: args.cwd || process.cwd(),
      session_id: sessionId,
    },
    cwd: (stdinEvent && stdinEvent.cwd) || args.cwd || process.cwd(),
    last_user_message: (stdinEvent && stdinEvent.last_user_message) || args.user_message || null,
    source_system: (stdinEvent && stdinEvent.source_system) || 'anvil',
    source_mode: (stdinEvent && stdinEvent.source_mode) || 'hook',
    source_agent: (stdinEvent && stdinEvent.source_agent) || 'anvil',
  };

  if (payload.tool_success == null && payload.tool_error) payload.tool_success = false;
  await postQueue(payload);
  console.log(JSON.stringify({ ok: true, queued: true, session_id: sessionId }));
}

main().catch((e) => {
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
