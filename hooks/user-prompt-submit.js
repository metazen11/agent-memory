#!/usr/bin/env node
/**
 * agent-memory UserPromptSubmit hook
 *
 * Two responsibilities, each fire-and-forget:
 *
 *   1. POST the prompt to /api/prompts so live capture writes a
 *      mem_user_prompts row (unchanged behavior — pre-existing).
 *
 *   2. Inject active CRITICAL lessons BEFORE the model reads the prompt,
 *      via hookSpecificOutput.additionalContext. This is the splice path
 *      that actually reaches the model — SessionStart's systemMessage
 *      does not survive session resume in some Claude Code builds.
 *
 *      Only lessons are injected. Project knowledge and recent activity
 *      are deliberately NOT pushed — they're low-signal as a per-prompt
 *      inject and the model can pull them on demand via search() and
 *      get_observations() when actually needed.
 *
 *      Lessons that have a paired enforcement artifact (hook, runtime
 *      check, GH issue) should be deactivated in the DB — the system
 *      enforces them, the model doesn't need to remember.
 *
 * Failure modes:
 *   - Memory server down → fall through to allow() (no context, no harm)
 *   - Fetch slower than 4s budget → fall through to allow()
 *   - Any unexpected error → fall through to allow()
 *
 * The prompt POST never blocks injection and vice versa.
 *
 * stdin: JSON { session_id, cwd, prompt, transcript_path, ... }
 * stdout: JSON { hookSpecificOutput?: { hookEventName, additionalContext } }
 *
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Resolve sibling modules against the script's real path, not the path node
// was invoked with. Without this, NODE_PRESERVE_SYMLINKS=1 (or
// --preserve-symlinks) breaks `require('./auth-header')` when the script is
// invoked via the ~/.claude/hooks/ symlink — node looks next to the symlink
// (no auth-header.js there) and throws MODULE_NOT_FOUND at line 1459 of
// the cjs loader, which Claude Code shows as a red hook-failure banner.
const { authHeaders } = require(
  path.join(path.dirname(fs.realpathSync(__filename)), 'auth-header')
);
const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';

const PROMPT_POST_TIMEOUT_MS = 300;
const FETCH_TIMEOUT_MS = 3000;
const TOTAL_BUDGET_MS = 4000;

// Sentinel directory is agent-scoped so codex/gemini/anvil get their own
// first-prompt tracking under the same agent-agnostic memory root.
const PRIMED_DIR = path.join(os.homedir(), '.agent-memory', 'primed', 'claude-code');

function envFlagEnabled(name, defaultValue = true) {
  const raw = process.env[name];
  if (raw == null || raw === '') return defaultValue;
  const normalized = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'off', 'no'].includes(normalized)) return false;
  return defaultValue;
}

const INJECT_ENABLED = envFlagEnabled('AGENT_MEMORY_PROMPT_INJECT_ENABLED', true);
const INJECT_GLOBAL_LESSONS = envFlagEnabled('AGENT_MEMORY_INJECT_GLOBAL_LESSONS', true);
const GLOBAL_LESSONS_CAP = parseInt(process.env.AGENT_MEMORY_GLOBAL_LESSONS_CAP || '5', 10);

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:user-prompt-submit] ${msg}`);
}

// Runtime errors worth surfacing (server down, fetch failed, sentinel write
// failed) — append to the same log file uncaughtException uses, never to
// stderr. Use this for "something is silently degrading" signals.
function notice(msg) {
  try {
    const dir = path.join(os.homedir(), '.agent-memory', 'logs');
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(
      path.join(dir, 'user-prompt-submit.errors.log'),
      `[${new Date().toISOString()}] notice: ${msg}\n`,
    );
  } catch {}
  if (DEBUG) console.error(`[agent-memory:user-prompt-submit] notice: ${msg}`);
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

// ── Sentinel: track which sessions have received the heavy first-prompt block ──

function sessionPrimed(sessionId) {
  if (!sessionId) return false;
  try {
    return fs.existsSync(path.join(PRIMED_DIR, sessionId));
  } catch {
    return false;
  }
}

function markSessionPrimed(sessionId) {
  if (!sessionId) return;
  try {
    fs.mkdirSync(PRIMED_DIR, { recursive: true });
    fs.writeFileSync(path.join(PRIMED_DIR, sessionId), String(Date.now()));
  } catch (e) {
    debug(`failed to write sentinel: ${e.message}`);
  }
}

// ── HTTP helpers ────────────────────────────────────────────

function httpGet(pathAndQuery, timeoutMs) {
  return new Promise((resolve) => {
    const url = new URL(`${SERVER_BASE}${pathAndQuery}`);
    const req = http.get({
      headers: { ...authHeaders() },
      hostname: url.hostname,
      port: url.port,
      path: `${url.pathname}${url.search}`,
      timeout: timeoutMs,
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch { resolve(null); }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
  });
}

// ── Pre-existing behavior: log the prompt (fire-and-forget) ──

function logPrompt(input) {
  // Don't await — let it race the rest of the hook. Errors are swallowed.
  const payload = JSON.stringify({
    session_id: input.session_id || `session-${Date.now()}`,
    prompt: input.prompt,
    cwd: input.cwd || process.cwd(),
    agent_name: 'claude-code',
  });
  const url = new URL(`${SERVER_BASE}/api/prompts`);
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
    timeout: PROMPT_POST_TIMEOUT_MS,
  }, (res) => {
    res.on('data', () => {});
    res.on('end', () => debug(`prompt POST response ${res.statusCode}`));
  });
  req.on('error', (err) => debug(`prompt POST error: ${err.message}`));
  req.on('timeout', () => { req.destroy(); });
  req.write(payload);
  req.end();
}

// ── Memory fetches (same endpoints as session-start.js) ──

function fetchLessons(project, severity, limit) {
  const params = new URLSearchParams({ active: 'true', limit: String(limit) });
  if (project) params.set('project', project);
  if (severity) params.set('severity', severity);
  return httpGet(`/api/lessons?${params}`, FETCH_TIMEOUT_MS)
    .then(r => Array.isArray(r) ? r : []);
}

// ── Formatters ──────────────────────────────────────────────

function formatLessons(lessons) {
  if (!lessons || lessons.length === 0) return '';
  const severityIcon = { critical: 'CRITICAL', warning: 'WARNING', info: 'INFO' };
  const lines = lessons.map((l, i) => {
    const icon = severityIcon[l.severity] || 'LESSON';
    const scope = l.project_name ? `[${l.project_name}]` : '[global]';
    return `  ${i + 1}. ${icon} ${scope}: ${l.rule}`;
  });
  return `## Active Lessons\n\nLearned from past mistakes. Follow them.\n\n${lines.join('\n')}\n\n`;
}

// ── Main ────────────────────────────────────────────────────

(async () => {
  const input = readStdin();
  if (!input || !input.prompt) {
    debug('no prompt in input — skipping');
    allow();
    return;
  }

  // Always log the prompt. Pre-existing behavior. Fire-and-forget.
  logPrompt(input);

  if (!INJECT_ENABLED) {
    debug('inject disabled via env');
    allow();
    return;
  }

  const sessionId = input.session_id || '';
  const cwd = input.cwd || process.cwd();
  const project = cwd;
  const projectName = path.basename(cwd);
  const isFirstPrompt = !sessionPrimed(sessionId);

  debug(`session=${sessionId} firstPrompt=${isFirstPrompt} project=${project}`);

  // Wall-clock budget so we never blow the 5s hook timeout.
  const budgetTimer = setTimeout(() => {
    debug('total budget exceeded — falling through');
    allow();
  }, TOTAL_BUDGET_MS);

  try {
    // Lessons only. Project knowledge and recent observations were dropped
    // from the inject: they're low-signal as a push and the model can pull
    // them on demand via search()/get_observations() when actually needed.
    const fetches = [fetchLessons(project, 'critical', 10)];
    if (INJECT_GLOBAL_LESSONS && GLOBAL_LESSONS_CAP > 0) {
      fetches.push(fetchLessons(null, 'critical', GLOBAL_LESSONS_CAP));
    } else {
      fetches.push(Promise.resolve([]));
    }

    const results = await Promise.all(fetches);
    clearTimeout(budgetTimer);

    const projectLessons = results[0] || [];
    const globalLessonsRaw = results[1] || [];

    // Filter globals to only those tagged null/empty project_name (truly
    // unscoped). Cap to GLOBAL_LESSONS_CAP. Dedupe against project lessons.
    const projectLessonIds = new Set(projectLessons.filter(l => l && l.id).map(l => l.id));
    const globalLessons = globalLessonsRaw
      .filter(l => l && l.id && !l.project_name && !projectLessonIds.has(l.id))
      .slice(0, GLOBAL_LESSONS_CAP);

    const allLessons = [...projectLessons, ...globalLessons];
    const ctx = formatLessons(allLessons).trim();

    if (!ctx) {
      debug('nothing to inject');
      allow();
      return;
    }

    // Sentinel still written so future first-prompt-only blocks remain
    // possible without re-priming the session.
    if (isFirstPrompt) {
      markSessionPrimed(sessionId);
    }

    debug(`injecting ${ctx.length} chars (projectLessons=${projectLessons.length}, globalLessons=${globalLessons.length})`);

    output({
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: `<agent-memory>\n${ctx}\n</agent-memory>`,
      },
    });
  } catch (e) {
    clearTimeout(budgetTimer);
    notice(`unexpected error in main: ${e && e.message}`);
    allow();
  }
})();
