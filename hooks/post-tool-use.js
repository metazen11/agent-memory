#!/usr/bin/env node
/**
 * agent-memory PostToolUse hook
 *
 * Fire-and-forget: sends tool call data to agent-memory server for async
 * observation processing. Never blocks — on any error, exits 0 silently.
 *
 * If the server is down or returns an error, spawns ensure-services.js in
 * the background (detached) to restart Docker + FastAPI. A lockfile debounces
 * so only one recovery attempt runs at a time.
 *
 * stdin: JSON { tool_name, tool_input, tool_response, session_id, cwd }
 * stdout: JSON { } (always allow)
 *
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const SERVER_URL = 'http://localhost:3377/api/queue';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';
const RECOVERY_LOCKFILE = path.join(require('os').tmpdir(), 'agent-memory-recovery.lock');
const RECOVERY_COOLDOWN_MS = 60000; // 1 minute between recovery attempts
const SPOOL_DIR = path.join(require('os').tmpdir(), 'agent-memory-spool');

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

/**
 * Save a failed payload to disk so it can be retried after recovery.
 */
function spoolPayload(payloadStr) {
  try {
    if (!fs.existsSync(SPOOL_DIR)) {
      fs.mkdirSync(SPOOL_DIR, { recursive: true });
    }
    const file = path.join(SPOOL_DIR, `${Date.now()}-${process.pid}.json`);
    fs.writeFileSync(file, payloadStr);
    debug(`Spooled payload to ${file}`);
  } catch (e) {
    debug(`Failed to spool payload: ${e.message}`);
  }
}

/**
 * Drain spooled payloads by re-posting them. Fire-and-forget, best-effort.
 * Runs after a successful POST to flush anything saved during downtime.
 */
function drainSpool() {
  let files;
  try {
    files = fs.readdirSync(SPOOL_DIR).filter(f => f.endsWith('.json'));
  } catch {
    return; // no spool dir
  }
  if (files.length === 0) return;

  debug(`Draining ${files.length} spooled payloads`);
  for (const file of files) {
    const filePath = path.join(SPOOL_DIR, file);
    try {
      const data = fs.readFileSync(filePath, 'utf8');
      const reqUrl = new URL(SERVER_URL);
      const r = http.request({
        hostname: reqUrl.hostname,
        port: reqUrl.port,
        path: reqUrl.pathname,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data),
        },
        timeout: 3000,
      }, (res) => {
        if (res.statusCode < 500) {
          debug(`Drained ${file} → ${res.statusCode}`);
          try { fs.unlinkSync(filePath); } catch {}
        }
        res.resume();
      });
      r.on('error', () => {});
      r.on('timeout', () => { r.destroy(); });
      r.on('socket', (s) => { s.unref(); });
      r.write(data);
      r.end();
    } catch {
      debug(`Failed to drain ${file}`);
    }
  }
}

/**
 * Spawn ensure-services.js in the background to restart Docker + FastAPI.
 * Debounced via lockfile — skips if a recovery ran within the last 60s.
 */
function triggerRecovery() {
  // Check lockfile for cooldown
  try {
    const stat = fs.statSync(RECOVERY_LOCKFILE);
    const age = Date.now() - stat.mtimeMs;
    if (age < RECOVERY_COOLDOWN_MS) {
      debug(`Recovery cooldown (${Math.round(age / 1000)}s ago), skipping`);
      return;
    }
  } catch {
    // No lockfile — first recovery attempt
  }

  // Find ensure-services.js relative to this script (follows symlinks)
  let scriptDir;
  try {
    scriptDir = path.dirname(fs.realpathSync(__filename));
  } catch {
    scriptDir = __dirname;
  }
  const ensureScript = path.join(scriptDir, 'ensure-services.js');

  if (!fs.existsSync(ensureScript)) {
    debug(`ensure-services.js not found at ${ensureScript}`);
    return;
  }

  // Write lockfile
  try {
    fs.writeFileSync(RECOVERY_LOCKFILE, String(Date.now()));
  } catch {
    debug('Failed to write recovery lockfile');
  }

  debug(`Spawning background recovery: ${ensureScript}`);
  const child = spawn('node', [ensureScript], {
    detached: true,
    stdio: 'ignore',
    env: { ...process.env, AGENT_MEMORY_DEBUG: DEBUG ? '1' : '0' },
  });
  child.unref();
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

// Fire-and-forget HTTP POST
// Exit after 200ms max so we never block Claude Code, even if the server is slow.
// On failure: spool payload to disk + trigger background recovery.
// On success: drain any previously spooled payloads.
const exitTimer = setTimeout(() => {
  debug('Exit timer — checking for unsent payload');
  if (!requestCompleted) {
    spoolPayload(payload);
    triggerRecovery();
  }
  process.exit(0);
}, 200);
exitTimer.unref();

let requestCompleted = false;

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
  timeout: 150,
}, (res) => {
  requestCompleted = true;
  debug(`POST /api/queue → ${res.statusCode}`);
  if (res.statusCode >= 500) {
    debug('Server error — spooling payload and triggering recovery');
    spoolPayload(payload);
    triggerRecovery();
  } else {
    // Success — drain any spooled payloads from previous failures
    drainSpool();
  }
  res.resume();
});

req.on('error', (e) => {
  if (!requestCompleted) {
    requestCompleted = true;
    debug(`POST error: ${e.message}`);
    spoolPayload(payload);
    triggerRecovery();
  }
});
req.on('timeout', () => {
  debug('POST timeout');
  req.destroy();
});
req.on('socket', (socket) => { socket.unref(); });

req.write(payload);
req.end();
