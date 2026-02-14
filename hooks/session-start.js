#!/usr/bin/env node
/**
 * agent-memory SessionStart hook
 *
 * BLOCKS until all services are confirmed running:
 * 1. Health check (fast path, 500ms)
 * 2. If down, spawn ensure-services.js to start Docker + FastAPI
 * 3. Retry health check up to 10 times
 * 4. Fetch recent observations and inject as systemMessage
 *
 * stdin: JSON { cwd, session_id, reason }
 * stdout: JSON { systemMessage?: string }
 *
 * Timeout: 60s (set in settings.json by install.js)
 * Set AGENT_MEMORY_DEBUG=0 to disable verbose logging.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG !== '0';

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:session-start] ${msg}`);
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

function output(obj) {
  const json = JSON.stringify(obj);
  debug(`stdout: ${json.slice(0, 300)}`);
  console.log(json);
  process.exit(0);
}

// ── MCP usage hint ──────────────────────────────────────────

const MCP_HINT = `# Agent Memory (MCP)

You have access to a persistent memory system via MCP tools (server: "agent-memory").
This stores observations from all past coding sessions — bugs found, decisions made,
patterns discovered, files modified. Use it to avoid repeating mistakes and build on prior work.

**When to search memory:**
- Before starting unfamiliar work ("have I solved this before?")
- When debugging ("did I hit this bug in a previous session?")
- When making architecture decisions ("what did I decide last time?")
- When the user asks about past work or previous sessions

**3-layer search workflow (saves 10x tokens):**
1. \`search(query)\` → Get index with IDs and titles (~50-100 tokens/result)
2. \`timeline(anchor=ID)\` → See what happened around an interesting result
3. \`get_observations([IDs])\` → Fetch full details ONLY for relevant IDs

**Never skip to step 3.** Always filter with search first.

**save_memory(text)** — Manually save important findings for future sessions.`;

// ── Health check ────────────────────────────────────────────

function healthCheck(timeoutMs) {
  return new Promise((resolve) => {
    const url = new URL(`${SERVER_BASE}/api/health`);
    const req = http.get({
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      timeout: timeoutMs,
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          resolve(json.status === 'ok' || json.status === 'degraded');
        } catch {
          resolve(false);
        }
      });
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// ── Ensure services ─────────────────────────────────────────

function resolveEnsureServicesPath() {
  // This file is symlinked: ~/.claude/hooks/agent-memory-session-start.js
  // → agentMemory/hooks/session-start.js
  // ensure-services.js is in the same directory
  let realDir;
  try {
    realDir = path.dirname(fs.realpathSync(__filename));
  } catch {
    realDir = __dirname;
  }
  return path.join(realDir, 'ensure-services.js');
}

function startServices() {
  const script = resolveEnsureServicesPath();
  if (!fs.existsSync(script)) {
    debug(`ensure-services.js not found at ${script}`);
    return false;
  }
  debug(`Running ensure-services.js...`);
  try {
    execFileSync('node', [script], {
      timeout: 45000,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, AGENT_MEMORY_DEBUG: DEBUG ? '1' : '0' },
    });
    debug('ensure-services.js completed successfully');
    return true;
  } catch (e) {
    debug(`ensure-services.js failed: ${e.message}`);
    return false;
  }
}

// ── Register session (fire-and-forget) ──────────────────────

function registerSession(sessionId, project, cwd) {
  const payload = JSON.stringify({
    session_id: sessionId,
    project: project,
    project_path: cwd,
    agent_type: 'claude-code',
  });

  const url = new URL(`${SERVER_BASE}/api/sessions`);
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
  }, (res) => { debug(`POST /api/sessions → ${res.statusCode}`); });
  req.on('error', (e) => { debug(`POST /api/sessions error: ${e.message}`); });
  req.on('timeout', () => { req.destroy(); });
  req.write(payload);
  req.end();
}

// ── Fetch recent observations ───────────────────────────────

function fetchObservations(project) {
  return new Promise((resolve) => {
    const url = new URL(`${SERVER_BASE}/api/observations?project=${encodeURIComponent(project)}&limit=5`);
    const req = http.get({
      hostname: url.hostname,
      port: url.port,
      path: `${url.pathname}${url.search}`,
      timeout: 3000,
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

// ── Main ────────────────────────────────────────────────────

const input = readStdin();

if (input.reason === 'clear') {
  debug('Skipping — reason is clear');
  output({});
}

const cwd = input.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const project = path.basename(cwd);
const sessionId = input.session_id || `session-${Date.now()}`;
debug(`project=${project} cwd=${cwd}`);

(async () => {
  // Step 1: Fast health check (500ms)
  let healthy = await healthCheck(500);

  if (!healthy) {
    debug('Services not running — starting...');
    // Step 2: Start services (blocking, up to 45s)
    startServices();

    // Step 3: Retry health check up to 10 times
    for (let i = 0; i < 10; i++) {
      await sleep(1000);
      healthy = await healthCheck(2000);
      if (healthy) {
        debug(`Health check passed after ${i + 1} retries`);
        break;
      }
    }
  }

  if (!healthy) {
    debug('Services still not healthy after retries');
    output({
      systemMessage: `${MCP_HINT}\n\n⚠ agent-memory services are not running. Run \`node install.js --start\` to start them.`,
    });
    return;
  }

  debug('Services healthy');

  // Step 4: Register session (fire-and-forget)
  registerSession(sessionId, project, cwd);

  // Step 5: Fetch recent observations
  const observations = await fetchObservations(project);

  if (!Array.isArray(observations) || observations.length === 0) {
    debug('No recent observations, injecting MCP hint only');
    output({ systemMessage: MCP_HINT });
    return;
  }

  // Format observations (chronological: oldest first)
  const sorted = observations.reverse();
  const lines = sorted.map((obs, i) => {
    const date = obs.created_at ? obs.created_at.replace('T', ' ').slice(0, 19) : '';
    const type = obs.type ? `[${obs.type}]` : '';
    return `  ${i + 1}. ${date} ${type} ${obs.title}`;
  });

  const recentCtx = `Recent memory for "${project}" (${observations.length} entries):\n${lines.join('\n')}`;
  const msg = `${MCP_HINT}\n\n${recentCtx}`;
  debug(`Injecting hint + ${observations.length} observations`);
  output({ systemMessage: msg });
})();
