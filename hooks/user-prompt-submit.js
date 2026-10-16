#!/usr/bin/env node
/**
 * agent-memory UserPromptSubmit hook
 *
 * Two responsibilities, each fire-and-forget:
 *
 *   1. POST the prompt to /api/prompts so live capture writes a
 *      mem_user_prompts row (unchanged behavior — pre-existing).
 *
 *   2. Inject active, project-relevant lessons BEFORE the model reads the prompt,
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
const crypto = require('crypto');

// Resolve sibling modules against the script's real path, not the path node
// was invoked with. Without this, NODE_PRESERVE_SYMLINKS=1 (or
// --preserve-symlinks) breaks `require('./auth-header')` when the script is
// invoked via the ~/.claude/hooks/ symlink — node looks next to the symlink
// (no auth-header.js there) and throws MODULE_NOT_FOUND at line 1459 of
// the cjs loader, which Claude Code shows as a red hook-failure banner.
const { authHeaders } = require(
  path.join(path.dirname(fs.realpathSync(__filename)), 'auth-header')
);
// Path normalizer mirror of app/path_normalize.py — rewrites stale
// /Dropbox/_CODING/ → /_CODING/ at the hook boundary so re-fired hooks
// from a stale shell never reintroduce the v4 path-bias problem.
const { normalizeText } = require(
  path.join(path.dirname(fs.realpathSync(__filename)), '_path-normalize')
);
const { renderLessonBlock } = require(
  path.join(path.dirname(fs.realpathSync(__filename)), 'lesson-block')
);
const { selectScopedLessons } = require('./lesson-selection');
const SERVER_BASE = process.env.AGENT_MEMORY_SERVER || 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';

const PROMPT_POST_TIMEOUT_MS = 300;
const FETCH_TIMEOUT_MS = 3000;
const TOTAL_BUDGET_MS = 4000;

const DELIVERY_DIR = path.join(os.homedir(), '.agent-memory', 'delivery', 'claude-code');

function envFlagEnabled(name, defaultValue = true) {
  const raw = process.env[name];
  if (raw == null || raw === '') return defaultValue;
  const normalized = String(raw).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'off', 'no'].includes(normalized)) return false;
  return defaultValue;
}

const INJECT_ENABLED = envFlagEnabled('AGENT_MEMORY_PROMPT_INJECT_ENABLED', true);

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

// Reserve the one lesson delivery for this session atomically.
// Pending markers prevent concurrent duplicate injections; stale markers are
// reclaimed after a crashed hook. Successful delivery is durable.
function deliveryReservation(sessionId) {
  if (!sessionId) return null;
  const key = crypto.createHash('sha256').update(sessionId).digest('hex');
  const pending = path.join(DELIVERY_DIR, `${key}.pending`);
  const done = path.join(DELIVERY_DIR, `${key}.done`);
  try {
    fs.mkdirSync(DELIVERY_DIR, { recursive: true });
    if (fs.existsSync(done)) return null;
    try {
      const fd = fs.openSync(pending, 'wx', 0o600);
      fs.closeSync(fd);
      if (fs.existsSync(done)) {
        fs.unlinkSync(pending);
        return null;
      }
      return { pending, done };
    } catch (e) {
      if (e.code === 'EEXIST') {
        const age = Date.now() - fs.statSync(pending).mtimeMs;
        if (age > 10000) { fs.unlinkSync(pending); return deliveryReservation(sessionId, project); }
      }
      return null;
    }
  } catch (e) { debug(`delivery reservation failed: ${e.message}`); return null; }
}

function finishDelivery(reservation, success) {
  if (!reservation) return;
  try {
    if (success) fs.renameSync(reservation.pending, reservation.done);
    else fs.unlinkSync(reservation.pending);
  } catch {}
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
        if (res.statusCode != null && (res.statusCode < 200 || res.statusCode >= 300)) {
          resolve(null);
          return;
        }
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
  // Path normalization (migration 014): strip stale /Dropbox/_CODING/ from
  // both the prompt text and the cwd before POSTing — the server also
  // normalizes, but doing it here keeps the wire format clean and means
  // the content_hash on the server side matches what the model saw.
  const payload = JSON.stringify({
    session_id: input.session_id || `session-${Date.now()}`,
    prompt: normalizeText(input.prompt),
    cwd: normalizeText(input.cwd || process.cwd()),
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
  if (project) {
    params.set('project', project);
    params.set('strict_scope', 'true');
  }
  if (severity) params.set('severity', severity);
  return httpGet(`/api/lessons?${params}`, FETCH_TIMEOUT_MS)
    .then(r => ({ ok: Array.isArray(r), lessons: Array.isArray(r) ? r : [] }));
}

// ── Main ────────────────────────────────────────────────────

async function main() {
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
  const project = normalizeText(cwd) || cwd;
  const reservation = deliveryReservation(sessionId);
  debug(`session=${sessionId} reserved=${!!reservation} project=${project}`);

  if (!reservation) {
    allow();
    return;
  }

  // Wall-clock budget so we never blow the 5s hook timeout.
  const budgetTimer = setTimeout(() => {
    debug('total budget exceeded — falling through');
    finishDelivery(reservation, false);
    allow();
  }, TOTAL_BUDGET_MS);

  try {
    // Lessons only. Project knowledge and recent observations were dropped
    // from the inject: they're low-signal as a push and the model can pull
    // them on demand via search()/get_observations() when actually needed.
    //
    // Fetch scoped and global lessons separately so the client can apply the
    // same boundary and precedence rules even against older servers.
    const projectResult = await fetchLessons(project, null, 100);
    const globalResult = await fetchLessons(null, null, 100);
    clearTimeout(budgetTimer);
    if (!projectResult.ok || !globalResult.ok) {
      finishDelivery(reservation, false);
      debug('lesson fetch failed — reservation released for retry');
      allow();
      return;
    }
    const projectLessons = projectResult.lessons;
    const globalLessons = globalResult.lessons;

    const allLessons = selectScopedLessons([...projectLessons, ...globalLessons], project, 20)
      .filter(l => l && l.id);
    const ctx = renderLessonBlock(allLessons);   // sanitise/bound/wrap: lesson-block.js (#89)

    if (!ctx) {
      finishDelivery(reservation, true);
      debug('nothing to inject');
      allow();
      return;
    }

    debug(`injecting ${ctx.length} chars (lessons=${allLessons.length})`);

    const payload = {
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: ctx,
      },
    };
    const serialized = JSON.stringify(payload) + '\n';
    let outputFailed = false;
    process.stdout.once('error', () => {
      outputFailed = true;
      finishDelivery(reservation, false);
    });
    process.stdout.write(serialized, (error) => {
      if (error || outputFailed) finishDelivery(reservation, false);
      else finishDelivery(reservation, true);
    });
  } catch (e) {
    clearTimeout(budgetTimer);
    finishDelivery(reservation, false);
    notice(`unexpected error in main: ${e && e.message}`);
    allow();
  }
}

if (require.main === module) {
  main();
} else {
  module.exports = { formatLessons: renderLessonBlock };   // alias kept for tests
}
