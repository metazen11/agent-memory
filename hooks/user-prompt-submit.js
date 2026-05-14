#!/usr/bin/env node
/**
 * agent-memory UserPromptSubmit hook
 *
 * Fires on every user prompt submission. POSTs the prompt text + session
 * + cwd to /api/prompts so live capture writes a mem_user_prompts row.
 * Fire-and-forget: on any error, exits 0 silently so the agent isn't
 * blocked.
 *
 * Closes the prompt-capture gap that left mem_user_prompts effectively
 * empty between 2026-03-29 and now (no live writer existed before #30).
 *
 * stdin: JSON { session_id, cwd, prompt, transcript_path, ... }
 * stdout: JSON { } (always allow)
 *
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');

const { authHeaders } = require('./auth-header');
const SERVER_URL = 'http://localhost:3377/api/prompts';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';
const POST_TIMEOUT_MS = 300;

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:user-prompt-submit] ${msg}`);
}

function output(obj) {
  console.log(JSON.stringify(obj));
  process.exit(0);
}

function allow() {
  output({});
}

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

const input = readStdin();
if (!input || !input.prompt) {
  // No prompt to record. Exit silently.
  debug('no prompt in input — skipping');
  allow();
}

const payload = JSON.stringify({
  session_id: input.session_id || `session-${Date.now()}`,
  prompt: input.prompt,
  cwd: input.cwd || process.cwd(),
  agent_name: 'claude-code',
});

debug(`POST /api/prompts session=${input.session_id} bytes=${payload.length}`);

const url = new URL(SERVER_URL);
const req = http.request({
  hostname: url.hostname,
  port: url.port,
  path: url.pathname,
  method: 'POST',
  headers: {
    ...authHeaders(),
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  },
  timeout: POST_TIMEOUT_MS,
}, (res) => {
  // Drain the response so the socket can close.
  res.on('data', () => {});
  res.on('end', () => {
    debug(`response ${res.statusCode}`);
    allow();
  });
});

req.on('error', (err) => {
  debug(`request error: ${err.message}`);
  allow();
});

req.on('timeout', () => {
  debug('timeout');
  req.destroy();
  allow();
});

req.write(payload);
req.end();

// Safety: if neither response nor error fires in time, exit anyway.
const exitTimer = setTimeout(() => {
  debug('exit-timer fallback');
  allow();
}, POST_TIMEOUT_MS + 100);
exitTimer.unref();
