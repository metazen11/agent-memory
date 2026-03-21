#!/usr/bin/env node
const { readSessionState, postQueuePayload, saveSpooledQueuePayload } = require('./common');

function parseArgs(argv) {
  const args = {
    tool: '',
    input: '',
    output: '',
    cwd: process.cwd(),
    session: process.env.AGENT_MEMORY_SESSION_ID || '',
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--tool') args.tool = argv[++i] || '';
    else if (a === '--input') args.input = argv[++i] || '';
    else if (a === '--output') args.output = argv[++i] || '';
    else if (a === '--cwd') args.cwd = argv[++i] || args.cwd;
    else if (a === '--session') args.session = argv[++i] || args.session;
  }
  return args;
}

function parseJsonLoose(value) {
  if (!value) return null;
  try { return JSON.parse(value); } catch { return { raw: value }; }
}

async function main() {
  const args = parseArgs(process.argv);
  const state = readSessionState();
  const sessionId = args.session || state?.session_id;
  if (!sessionId) {
    console.error('No session id found. Run session-start first or pass --session.');
    process.exit(2);
  }

  const payload = {
    session_id: sessionId,
    tool_name: args.tool || null,
    tool_input: parseJsonLoose(args.input),
    tool_response_preview: (args.output || '').slice(0, 2000) || null,
    cwd: args.cwd || process.cwd(),
    last_user_message: null,
  };

  try {
    await postQueuePayload(payload, 1200);
    console.log(JSON.stringify({ ok: true, queued: true, session_id: sessionId, mode: 'online' }));
  } catch {
    const file = saveSpooledQueuePayload(payload);
    console.log(JSON.stringify({ ok: true, queued: true, session_id: sessionId, mode: 'spooled', file }));
  }
}

main().catch((e) => {
  console.error(JSON.stringify({ ok: false, error: e.message || String(e) }));
  process.exit(1);
});
