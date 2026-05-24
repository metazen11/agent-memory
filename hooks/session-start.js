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
const { execFileSync, spawn } = require('child_process');

// Resolve sibling modules against the script's real path, not the path node
// was invoked with. Without this, NODE_PRESERVE_SYMLINKS=1 (or
// --preserve-symlinks) breaks `require('./auth-header')` when the script is
// invoked via the ~/.claude/hooks/ symlink — MODULE_NOT_FOUND at cjs loader
// 1459, surfaces as a red hook-failure banner in Claude Code.
const { authHeaders } = require(
  path.join(path.dirname(fs.realpathSync(__filename)), 'auth-header')
);
const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';

function envFlagEnabled(name, defaultValue = true) {
  const raw = process.env[name];
  if (raw == null || raw === '') return defaultValue;
  const normalized = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'off', 'no'].includes(normalized)) return false;
  return defaultValue;
}

const GLOBAL_HINTS_ENABLED = envFlagEnabled('AGENT_MEMORY_HINTS_ENABLED', true);
const SESSION_HINTS_ENABLED = envFlagEnabled('AGENT_MEMORY_SESSION_HINTS_ENABLED', GLOBAL_HINTS_ENABLED);

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

// ── Preamble strings moved to abilities_memory() MCP tool ─────────
// The full operator manual (MCP_HINT, MEMORY_VISIBILITY_RULES, project
// scoping rules, tool inventory, project counts) now lives in
// mcp_server.py::_abilities_memory and is fetched on demand. Keeping the
// per-session preamble tiny prevents Claude Code's <persisted-output> from
// truncating the payload above its ~2KB preview cap.

// ── Health check ────────────────────────────────────────────

function healthCheck(timeoutMs) {
  return new Promise((resolve) => {
    const url = new URL(`${SERVER_BASE}/api/health`);
    const req = http.get({
      headers: { ...authHeaders() },
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

let startupNotices = [];

function startServices() {
  const script = resolveEnsureServicesPath();
  if (!fs.existsSync(script)) {
    debug(`ensure-services.js not found at ${script}`);
    return false;
  }
  debug(`Running ensure-services.js...`);
  try {
    const stdout = execFileSync('node', [script], {
      timeout: 45000,
      stdio: ['ignore', 'pipe', 'pipe'],
      encoding: 'utf8',
      env: { ...process.env, AGENT_MEMORY_DEBUG: DEBUG ? '1' : '0' },
    });
    // Capture notice lines from ensure-services.js
    if (stdout) {
      startupNotices = stdout.split('\n')
        .filter(l => l.startsWith('[agent-memory]'))
        .map(l => l.replace('[agent-memory] ', ''));
    }
    debug('ensure-services.js completed successfully');
    return true;
  } catch (e) {
    debug(`ensure-services.js failed: ${e.message}`);
    if (e.stdout) {
      startupNotices = e.stdout.toString().split('\n')
        .filter(l => l.startsWith('[agent-memory]'))
        .map(l => l.replace('[agent-memory] ', ''));
    }
    return false;
  }
}

// ── MCP server probe ───────────────────────────────────────

function mcpProbe() {
  return new Promise((resolve) => {
    // Read the MCP config — check global (~/.claude/.mcp.json) then project-level (.mcp.json in cwd)
    let mcpConfig;
    let mcpJsonDir;
    const candidates = [
      path.join(require('os').homedir(), '.claude', '.mcp.json'),
      path.join(cwd, '.mcp.json'),
    ];
    for (const candidate of candidates) {
      try {
        mcpConfig = JSON.parse(fs.readFileSync(candidate, 'utf8'));
        mcpJsonDir = path.dirname(candidate);
        debug(`Found MCP config at ${candidate}`);
        break;
      } catch {
        // try next
      }
    }
    if (!mcpConfig) {
      debug('Cannot read .mcp.json from any location');
      resolve(false);
      return;
    }

    const server = mcpConfig.mcpServers && mcpConfig.mcpServers['agent-memory'];
    if (!server) {
      debug('agent-memory not found in .mcp.json');
      resolve(false);
      return;
    }

    // Resolve relative command paths against the directory containing .mcp.json
    let cmd = server.command;
    if (cmd.startsWith('./') || cmd.startsWith('../')) {
      cmd = path.resolve(mcpJsonDir, cmd);
    }
    const args = server.args ? server.args.map(a => (a.startsWith('./') || a.startsWith('../')) ? path.resolve(mcpJsonDir, a) : a) : [];

    // Spawn the MCP server and send initialize
    const proc = spawn(cmd, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 8000,
    });

    let stdout = '';
    let resolved = false;

    const timer = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        debug('MCP probe timed out after 8s');
        proc.kill();
        resolve(false);
      }
    }, 8000);

    proc.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      // Check if we got a valid initialize response
      if (stdout.includes('"serverInfo"') && !resolved) {
        resolved = true;
        clearTimeout(timer);
        proc.kill();
        debug(`MCP probe got response: ${stdout.slice(0, 100)}`);
        resolve(true);
      }
    });

    proc.on('error', (e) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timer);
        debug(`MCP probe spawn error: ${e.message}`);
        resolve(false);
      }
    });

    proc.on('exit', (code) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timer);
        debug(`MCP probe exited with code ${code}`);
        resolve(false);
      }
    });

    // Send initialize request
    const initMsg = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'agent-memory-probe', version: '1.0' },
      },
    });

    proc.stdin.write(initMsg + '\n');
  });
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
    headers: { ...authHeaders(),
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

// fetchLessons / fetchObservations / searchProjectContext removed —
// the preamble no longer pushes lessons/observations/project-context on
// session start. The model pulls them on demand via abilities_memory(),
// recall(), search(), and search_lessons() MCP tools. Lessons themselves
// still inject every prompt via user-prompt-submit.js (unchanged).

// ── Main ────────────────────────────────────────────────────

const input = readStdin();

if (input.reason === 'clear') {
  debug('Skipping — reason is clear');
  output({});
}

const cwd = input.cwd || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const project = cwd;
const projectName = path.basename(cwd);
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
      systemMessage: SESSION_HINTS_ENABLED
        ? `# agent-memory degraded\n\n⚠ agent-memory services are not running for cwd \`${cwd}\`. ` +
          `Run \`node install.js --start\` from the agent-memory repo to start them. ` +
          `Memory tools will return errors until services come back.`
        : '⚠ agent-memory services are not running. Run `node install.js --start` to start them.',
    });
    return;
  }

  debug('Services healthy');

  // Step 4: Verify MCP server can start and respond
  let mcpHealthy = false;
  try {
    mcpHealthy = await mcpProbe();
  } catch (e) {
    debug(`MCP probe error: ${e.message}`);
  }
  if (!mcpHealthy) {
    debug('MCP server probe FAILED — read path may be broken');
    startupNotices.push('WARNING: MCP server failed probe — memory search tools may not be available. Check Python venv and dependencies.');
  } else {
    debug('MCP server probe passed');
  }

  // Step 5: Register session (fire-and-forget)
  registerSession(sessionId, project, cwd);

  // Step 6: Emit a minimal preamble. The session-start systemMessage used to
  // carry the MCP usage manual + visibility rules + 10 critical lessons + 10
  // project-knowledge observations + 5 recent observations (~15KB total),
  // which blew past Claude Code's ~2KB <persisted-output> preview cap so the
  // tail got file-stashed and never reached the model.
  //
  // Lessons still fire every turn via the user-prompt-submit hook — that's
  // the load-bearing surface. The operator manual + project counts + tool
  // inventory moved to the `abilities_memory()` MCP tool, which the model
  // can pull on demand (and which renders LIVE from list_tools() + DB
  // counts, so it never drifts).
  const noticeBlock = startupNotices.length > 0
    ? `**Startup:** ${startupNotices.join(' → ')}\n\n`
    : '';

  const stub = SESSION_HINTS_ENABLED
    ? (
        `${noticeBlock}` +
        `# agent-memory online\n` +
        `Project: \`${projectName}\` (cwd: \`${cwd}\`)\n\n` +
        `Pass \`project="${project}"\` on every memory tool call. ` +
        `Active CRITICAL lessons are injected automatically on every prompt ` +
        `under \`<agent-memory>\` — you do not need to fetch them.\n\n` +
        `For the full operator manual + live tool inventory + project counts, ` +
        `call \`abilities_memory(project="${project}")\` once per session.`
      )
    : `${noticeBlock}agent-memory is online. Session-start hint injection is disabled (\`AGENT_MEMORY_SESSION_HINTS_ENABLED=0\`).`;

  debug(`Injecting stub preamble (${stub.length} chars)`);
  output({ systemMessage: stub });
})();
