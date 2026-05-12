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

const { authHeaders } = require('./auth-header');
const SERVER_BASE = 'http://localhost:3377';
const SERVER_URL = `${SERVER_BASE}/api/queue`;
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

function envFlagEnabled(name, defaultValue = true) {
  const raw = process.env[name];
  if (raw == null || raw === '') return defaultValue;
  const normalized = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'off', 'no'].includes(normalized)) return false;
  return defaultValue;
}

const GLOBAL_HINTS_ENABLED = envFlagEnabled('AGENT_MEMORY_HINTS_ENABLED', true);
const POST_TOOL_HINTS_ENABLED = envFlagEnabled('AGENT_MEMORY_POST_TOOL_HINTS_ENABLED', GLOBAL_HINTS_ENABLED);

function output(obj) {
  console.log(JSON.stringify(obj));
  process.exit(0);
}

function allow() {
  debug('→ allow');
  output({});
}

/**
 * GET /api/lessons/match with output trigger params.
 */
function fetchOutputLessonMatches(toolName, outputPreview, project) {
  return new Promise((resolve) => {
    const params = new URLSearchParams({
      tool_name: toolName,
      trigger_on: 'output',
      tool_output_preview: outputPreview.slice(0, 1000),
    });
    if (project) params.set('project', project);

    const url = new URL(`${SERVER_BASE}/api/lessons/match?${params}`);
    const req = http.get({
      headers: { ...authHeaders() },
      hostname: url.hostname,
      port: url.port,
      path: `${url.pathname}${url.search}`,
      timeout: 1500,
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch {
          resolve([]);
        }
      });
    });
    req.on('error', () => resolve([]));
    req.on('timeout', () => { req.destroy(); resolve([]); });
  });
}

/**
 * Fire-and-forget POST to track that a lesson was triggered.
 */
function trackTrigger(lessonId) {
  const url = new URL(`${SERVER_BASE}/api/lessons/${lessonId}/trigger`);
  const req = http.request({
    hostname: url.hostname,
    port: url.port,
    path: url.pathname,
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json', 'Content-Length': 2 },
    timeout: 1000,
  }, () => {});
  req.on('error', () => {});
  req.on('timeout', () => { req.destroy(); });
  req.on('socket', (s) => { s.unref(); });
  req.write('{}');
  req.end();
}

function asErrorText(value) {
  if (!value) return null;
  if (typeof value === 'string') return value.slice(0, 2000);
  try { return JSON.stringify(value).slice(0, 2000); } catch { return String(value).slice(0, 2000); }
}

function inferOutcome(input) {
  let toolSuccess = null;
  let toolError = null;

  if (typeof input.tool_success === 'boolean') toolSuccess = input.tool_success;
  if (typeof input.success === 'boolean') toolSuccess = input.success;

  if (input.tool_error) toolError = asErrorText(input.tool_error);
  if (!toolError && input.error) toolError = asErrorText(input.error);

  const resp = input.tool_response;
  if (!toolError && resp && typeof resp === 'object') {
    if (resp.error) toolError = asErrorText(resp.error);
  }

  if (toolSuccess === null) {
    if (toolError) toolSuccess = false;
    else if (input.failed === true || input.is_error === true) toolSuccess = false;
    else if (resp && typeof resp === 'object' && typeof resp.success === 'boolean') toolSuccess = resp.success;
  }

  return { toolSuccess, toolError };
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
        headers: { ...authHeaders(),
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
const { toolSuccess, toolError } = inferOutcome(input);
const toolResponsePreview = typeof input.tool_response === 'string'
  ? input.tool_response.slice(0, 2000)
  : JSON.stringify(input.tool_response || '').slice(0, 2000);
const payload = JSON.stringify({
  session_id: input.session_id || `session-${Date.now()}`,
  hook_event_name: 'PostToolUse',
  tool_name: toolName,
  tool_input: input.tool_input || null,
  tool_response: input.tool_response || null,
  tool_response_preview: toolResponsePreview,
  tool_success: toolSuccess,
  tool_error: toolError,
  raw_event: input,
  cwd: input.cwd || process.cwd(),
  last_user_message: null,
  source_system: input.source_system || 'claude-code',
  source_mode: input.source_mode || 'hook',
  source_agent: input.source_agent || null,
});

debug(`POST /api/queue tool=${toolName} payload=${payload.length}b`);

/**
 * Fire-and-forget the queue POST (non-blocking).
 * Called after stdout is written.
 */
function fireQueuePost() {
  const exitTimer = setTimeout(() => {
    debug('Exit timer — checking for unsent payload');
    if (!requestCompleted) {
      spoolPayload(payload);
      triggerRecovery();
    }
    process.exit(0);
  }, 200);
  exitTimer.unref();

  const url = new URL(SERVER_URL);
  const req = http.request({
    hostname: url.hostname,
    port: url.port,
    path: url.pathname,
    method: 'POST',
    headers: { ...authHeaders(),
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
}

let requestCompleted = false;

// Check output-based lessons before writing stdout
const outputPreview = asErrorText(input.tool_response) || '';
if (outputPreview && GLOBAL_HINTS_ENABLED && POST_TOOL_HINTS_ENABLED) {
  const project = input.cwd || process.cwd();
  debug(`Checking output lessons for ${toolName}`);

  (async () => {
    const matches = await fetchOutputLessonMatches(toolName, outputPreview, project);

    if (Array.isArray(matches) && matches.length > 0) {
      debug(`${matches.length} output lesson(s) matched`);
      const severity_icons = { critical: 'CRITICAL', warning: 'WARNING', info: 'INFO' };
      const lines = matches.map((lesson) => {
        const icon = severity_icons[lesson.severity] || 'LESSON';
        const scope = lesson.project_name ? `[${lesson.project_name}]` : '[global]';
        return `${icon} ${scope}: ${lesson.rule}`;
      });

      // Fire-and-forget trigger tracking
      for (const lesson of matches) {
        trackTrigger(lesson.id);
      }

      console.log(JSON.stringify({ systemMessage: `## Output Lesson\n${lines.join('\n')}` }));
    } else {
      debug('No output lesson matches');
      console.log(JSON.stringify({}));
    }

    // Fire queue POST after stdout
    fireQueuePost();
  })();
} else {
  // No output to check — write stdout and fire queue POST
  console.log(JSON.stringify({}));
  fireQueuePost();
}
