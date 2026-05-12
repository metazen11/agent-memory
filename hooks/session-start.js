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

const { authHeaders } = require('./auth-header');
const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG !== '0';

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

**save_memory(text)** — Manually save important findings for future sessions.

**IMPORTANT — Project scoping:**
When using \`create_lesson\`, \`search_lessons\`, \`search\`, or \`save_memory\`, ALWAYS pass the \`project\` parameter
with the full project path (cwd) to scope results. The current project path is provided below.`;

// ── Memory visibility rules ─────────────────────────────────

const MEMORY_VISIBILITY_RULES = `## Memory Visibility Rules (MUST FOLLOW)

**When you use memory tools (search, get_observations, timeline, save_memory), you MUST show the user what was returned.**
Do NOT silently consume memory results — always print a brief summary so the user knows what memories were found and used.

Example format when using search results:
> **Memory recall:** Found 3 relevant memories for "auth bug"
> 1. [bugfix] Fixed JWT refresh token race condition (2026-02-15)
> 2. [decision] Switched to httpOnly cookies for token storage (2026-02-14)
> 3. [pattern] Auth errors often caused by stale Redis cache (2026-02-12)

**Periodic memory check:** Every ~10 prompts in a session, proactively search memory for context related to your current task. Print what you find (or "No relevant memories found").

**At session start:** Briefly mention the recent memories shown above so the user knows you have context.`;

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
    // Read the MCP config to find the server command
    let mcpConfig;
    try {
      const mcpJsonPath = path.join(require('os').homedir(), '.claude', '.mcp.json');
      mcpConfig = JSON.parse(fs.readFileSync(mcpJsonPath, 'utf8'));
    } catch {
      debug('Cannot read .mcp.json');
      resolve(false);
      return;
    }

    const server = mcpConfig.mcpServers && mcpConfig.mcpServers['agent-memory'];
    if (!server) {
      debug('agent-memory not found in .mcp.json');
      resolve(false);
      return;
    }

    const cmd = server.command;
    const args = server.args || [];

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

// ── Fetch active lessons ────────────────────────────

function fetchLessons(project, severity, limit) {
  return new Promise((resolve) => {
    const params = new URLSearchParams({ active: 'true', limit: String(limit) });
    if (project) params.set('project', project);
    if (severity) params.set('severity', severity);

    const url = new URL(`${SERVER_BASE}/api/lessons?${params}`);
    const req = http.get({
      headers: { ...authHeaders() },
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

// ── Fetch recent observations ───────────────────────────────

function fetchObservations(project) {
  return new Promise((resolve) => {
    const url = new URL(`${SERVER_BASE}/api/observations?project=${encodeURIComponent(project)}&limit=5`);
    const req = http.get({
      headers: { ...authHeaders() },
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

// ── Search for high-value project context ────────────────────

function searchProjectContext(project, projectName) {
  return new Promise((resolve) => {
    // Search for decisions, patterns, gotchas, and bugfixes — the high-value types
    const payload = JSON.stringify({
      query: `${projectName} architecture decisions patterns configuration`,
      project: project,
      type: ['decision', 'pattern', 'gotcha', 'bugfix'],
      limit: 10,
    });

    const url = new URL(`${SERVER_BASE}/api/observations/search`);
    const req = http.request({
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: 'POST',
      headers: { ...authHeaders(),
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      },
      timeout: 5000,
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(data);
          resolve(result.observations || []);
        } catch {
          resolve([]);
        }
      });
    });
    req.on('error', () => resolve([]));
    req.on('timeout', () => { req.destroy(); resolve([]); });
    req.write(payload);
    req.end();
  });
}

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
const projectCtx = `\n\n**Current project:** \`${projectName}\` (path: \`${cwd}\`)\nUse \`project="${project}"\` when calling create_lesson, search_lessons, search, or save_memory.`;
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
        ? `${MCP_HINT}${projectCtx}\n\n⚠ agent-memory services are not running. Run \`node install.js --start\` to start them.`
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

  // Step 6: Fetch recent observations + search context + active lessons in parallel
  const [observations, projectContext, projectLessons, globalLessons] = await Promise.all([
    fetchObservations(project),
    searchProjectContext(project, projectName),
    SESSION_HINTS_ENABLED ? fetchLessons(project, 'critical', 10) : Promise.resolve([]),
    SESSION_HINTS_ENABLED ? fetchLessons(null, 'critical', 5) : Promise.resolve([]),
  ]);

  // Build startup notice block (if services had to be started)
  const noticeBlock = startupNotices.length > 0
    ? `**Startup:** ${startupNotices.join(' → ')}\n\n`
    : '';

  // Deduplicate global + project lessons by id
  const lessonMap = new Map();
  const rawLessons = [
    ...(Array.isArray(globalLessons) ? globalLessons : []),
    ...(Array.isArray(projectLessons) ? projectLessons : []),
  ];
  for (const l of rawLessons) {
    if (l?.id) lessonMap.set(l.id, l);
  }
  const allLessons = [...lessonMap.values()];

  // Format lessons block
  let lessonsBlock = '';
  if (allLessons.length > 0) {
    const severityIcon = { critical: 'CRITICAL', warning: 'WARNING', info: 'INFO' };
    const lessonLines = allLessons.map((l, i) => {
      const icon = severityIcon[l.severity] || 'LESSON';
      const scope = l.project_name ? `[${l.project_name}]` : '[global]';
      return `  ${i + 1}. ${icon} ${scope}: ${l.rule}`;
    });
    lessonsBlock = `## Active Lessons\n\nThese lessons were learned from past mistakes. Follow them.\n\n${lessonLines.join('\n')}\n\n`;
    debug(`Injecting ${allLessons.length} lessons`);
  }

  const hasObservations = Array.isArray(observations) && observations.length > 0;
  const hasContext = Array.isArray(projectContext) && projectContext.length > 0;

  if (!hasObservations && !hasContext && allLessons.length === 0) {
    debug('No recent observations, context, or lessons — injecting MCP hint only');
    output({
      systemMessage: SESSION_HINTS_ENABLED
        ? `${noticeBlock}${MCP_HINT}${projectCtx}\n\n${MEMORY_VISIBILITY_RULES}`
        : `${noticeBlock}agent-memory is online. Session-start hint injection is disabled (\`AGENT_MEMORY_SESSION_HINTS_ENABLED=0\`).`,
    });
    return;
  }

  // Format recent activity (chronological: oldest first, titles only)
  let recentCtx = '';
  if (hasObservations) {
    const sorted = observations.reverse();
    const lines = sorted.map((obs, i) => {
      const date = obs.created_at ? obs.created_at.replace('T', ' ').slice(0, 19) : '';
      const type = obs.type ? `[${obs.type}]` : '';
      return `  ${i + 1}. ${date} ${type} ${obs.title}`;
    });
    recentCtx = `## Recent Activity\n\n${lines.join('\n')}\n\n`;
  }

  // Format project knowledge (rich — includes narratives and facts)
  let knowledgeCtx = '';
  if (hasContext) {
    // Deduplicate against recent observations
    const recentIds = new Set(hasObservations ? observations.map(o => o.id) : []);
    const unique = projectContext.filter(o => !recentIds.has(o.id));

    if (unique.length > 0) {
      const lines = unique.map((obs, i) => {
        const type = obs.type ? `[${obs.type}]` : '';
        let entry = `  ${i + 1}. ${type} **${obs.title}**`;
        if (obs.narrative) entry += `\n     ${obs.narrative}`;
        if (obs.facts && Array.isArray(obs.facts)) {
          const facts = typeof obs.facts === 'string' ? JSON.parse(obs.facts) : obs.facts;
          if (facts.length > 0) {
            entry += '\n     ' + facts.slice(0, 3).map(f => `• ${f}`).join('\n     ');
          }
        }
        return entry;
      });
      knowledgeCtx = `## Project Knowledge (from past sessions)\n\nKey decisions, patterns, and gotchas for "${projectName}":\n\n${lines.join('\n\n')}\n\n`;
    }
  }

  const msg = SESSION_HINTS_ENABLED
    ? `${noticeBlock}${MCP_HINT}${projectCtx}\n\n${MEMORY_VISIBILITY_RULES}\n\n${lessonsBlock}${knowledgeCtx}${recentCtx}`
    : `${noticeBlock}agent-memory is online. Session-start hint injection is disabled (\`AGENT_MEMORY_SESSION_HINTS_ENABLED=0\`).`;
  debug(`Injecting hint=${SESSION_HINTS_ENABLED} + ${allLessons.length} lessons + ${hasContext ? projectContext.length : 0} knowledge + ${hasObservations ? observations.length : 0} recent`);
  output({ systemMessage: msg });
})();
