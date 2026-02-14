#!/usr/bin/env node
/**
 * agent-memory PostToolUse hook
 *
 * Fire-and-forget: sends tool call data to agent-memory server for async
 * observation processing. Never blocks — on any error, exits 0 silently.
 *
 * stdin: JSON { tool_name, tool_input, tool_response, session_id, cwd }
 * stdout: JSON { } (always allow)
 *
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');

const SERVER_URL = 'http://localhost:3377/api/queue';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:post-tool-use] ${msg}`);
}

// Tools that produce no useful observations
const SKIP_TOOLS = new Set([
  'ListMcpResourcesTool', 'SlashCommand', 'Skill', 'TodoWrite',
  'AskUserQuestion', 'TaskCreate', 'TaskUpdate', 'TaskGet', 'TaskList',
  'TaskOutput', 'TaskStop', 'EnterPlanMode', 'ExitPlanMode',
]);

function readStdin() {
  try {
    const raw = fs.readFileSync(0, 'utf8');
    debug(`stdin: ${raw.slice(0, 150)}`);
    return JSON.parse(raw);
  } catch {
    debug('Failed to parse stdin');
    return null;
  }
}

function allow() {
  debug('→ allow');
  console.log(JSON.stringify({}));
  process.exit(0);
}

const input = readStdin();
if (!input) {
  allow();
}

const toolName = input.tool_name || '';

// Skip low-value tools
if (SKIP_TOOLS.has(toolName)) {
  debug(`Skipping ${toolName} (in SKIP_TOOLS)`);
  allow();
}

// Build queue payload
const payload = JSON.stringify({
  session_id: input.session_id || `session-${Date.now()}`,
  tool_name: toolName,
  tool_input: input.tool_input || null,
  tool_response_preview: typeof input.tool_response === 'string'
    ? input.tool_response.slice(0, 2000)
    : JSON.stringify(input.tool_response || '').slice(0, 2000),
  cwd: input.cwd || process.cwd(),
  last_user_message: null,
});

debug(`POST /api/queue tool=${toolName} payload=${payload.length}b`);

// Write stdout FIRST so Claude Code can proceed immediately
console.log(JSON.stringify({}));

// Fire-and-forget HTTP POST — unref so it doesn't block exit
const url = new URL(SERVER_URL);
const req = http.request({
  hostname: url.hostname,
  port: url.port,
  path: url.pathname,
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  },
  timeout: 2000,
}, (res) => {
  debug(`POST /api/queue → ${res.statusCode}`);
  res.resume(); // drain response
});

req.on('error', (e) => { debug(`POST error: ${e.message}`); });
req.on('timeout', () => { debug('POST timeout'); req.destroy(); });
req.on('socket', (socket) => { socket.unref(); });

req.write(payload);
req.end();
