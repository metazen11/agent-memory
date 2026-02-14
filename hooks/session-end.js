#!/usr/bin/env node
/**
 * agent-memory SessionEnd hook
 *
 * Marks the session as completed on the agent-memory server.
 *
 * stdin: JSON { session_id }
 * stdout: (none needed for SessionEnd)
 *
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');

const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG !== '0';

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:session-end] ${msg}`);
}

function readStdin() {
  try {
    const raw = fs.readFileSync(0, 'utf8');
    debug(`stdin: ${raw.slice(0, 200)}`);
    return JSON.parse(raw);
  } catch {
    debug('Failed to parse stdin');
    return {};
  }
}

const input = readStdin();
const sessionId = input.session_id;

if (!sessionId) {
  debug('No session_id — exiting');
  process.exit(0);
}

// PATCH session to completed
const payload = JSON.stringify({ status: 'completed' });
const url = new URL(`${SERVER_BASE}/api/sessions/${encodeURIComponent(sessionId)}`);

debug(`PATCH /api/sessions/${sessionId}`);

const req = http.request({
  hostname: url.hostname,
  port: url.port,
  path: url.pathname,
  method: 'PATCH',
  headers: {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  },
  timeout: 5000,
}, (res) => { debug(`PATCH → ${res.statusCode}`); });

req.on('error', (e) => { debug(`PATCH error: ${e.message}`); });
req.on('timeout', () => { debug('PATCH timeout'); req.destroy(); });

req.write(payload);
req.end();

// Don't wait for response
process.exit(0);
