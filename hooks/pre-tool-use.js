#!/usr/bin/env node
/**
 * agent-memory PreToolUse hook — Lessons system
 *
 * Checks active lessons before Edit|Write|Bash|NotebookEdit operations.
 * If matching lessons are found, injects them as a systemMessage warning.
 *
 * stdin: JSON { tool_name, tool_input, session_id, cwd }
 * stdout: JSON { systemMessage?: string } or {}
 *
 * Timeout: 2s (must be fast — no embeddings, just regex + index lookup)
 * Set AGENT_MEMORY_DEBUG=1 for verbose stderr logging.
 */

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { authHeaders } = require('./auth-header');
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
const PRE_TOOL_HINTS_ENABLED = envFlagEnabled('AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED', GLOBAL_HINTS_ENABLED);

function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:pre-tool-use] ${msg}`);
}

function readStdin() {
  try {
    const raw = fs.readFileSync(0, 'utf8');
    debug(`stdin: ${raw.slice(0, 200)}`);
    return JSON.parse(raw);
  } catch {
    debug('Failed to parse stdin');
    return null;
  }
}

function output(obj) {
  console.log(JSON.stringify(obj));
  process.exit(0);
}

/**
 * Extract a preview string from tool_input for pattern matching.
 * For Bash: the command. For Edit/Write: the file path + content snippet.
 */
function extractToolInputPreview(toolName, toolInput) {
  if (!toolInput) return '';

  if (toolName === 'Bash') {
    return toolInput.command || toolInput.cmd || '';
  }
  if (toolName === 'Edit' || toolName === 'Write') {
    const parts = [];
    if (toolInput.file_path) parts.push(toolInput.file_path);
    if (toolInput.new_string) parts.push(toolInput.new_string.slice(0, 500));
    if (toolInput.content) parts.push(toolInput.content.slice(0, 500));
    return parts.join(' ');
  }
  if (toolName === 'NotebookEdit') {
    const parts = [];
    if (toolInput.notebook_path) parts.push(toolInput.notebook_path);
    if (toolInput.new_source) parts.push(toolInput.new_source.slice(0, 500));
    return parts.join(' ');
  }

  // Generic: stringify and truncate
  try {
    return JSON.stringify(toolInput).slice(0, 500);
  } catch {
    return '';
  }
}

/**
 * Extract file paths a tool call will touch, for file_scope lesson matching.
 * The API also falls back to basename matching, so both `.mcp.json` and
 * `/abs/path/.mcp.json` match a `.mcp.json` glob.
 */
function extractModifiedFiles(toolName, toolInput) {
  if (!toolInput) return [];
  const files = [];
  const push = (v) => { if (typeof v === 'string' && v) files.push(v); };

  push(toolInput.file_path);
  push(toolInput.notebook_path);
  push(toolInput.path);

  if (Array.isArray(toolInput.edits)) {
    for (const e of toolInput.edits) push(e && e.file_path);
  }

  // Bash: pull path-looking tokens out of the command so editing a file via
  // sed -i / heredoc / redirect still matches file_scope lessons. Deliberately
  // loose — a false positive surfaces one extra lesson, a false negative
  // silently loses a CRITICAL warning.
  if (toolName === 'Bash') {
    const cmd = toolInput.command || toolInput.cmd || '';
    // Split on shell whitespace and redirects first, so a token like
    // `.mcp.json` keeps its leading dot instead of being eaten by a
    // greedy path alternative (an earlier regex turned it into `mcp.json`,
    // which silently failed basename matching).
    const words = cmd.split(/[\s;|&<>()'"`]+/);
    for (const w of words) {
      const t = w.replace(/^[=]+/, '');
      if (!t || t.length > 300) continue;
      // Keep anything that looks like a path or a dotted filename.
      if (/\//.test(t) || /^[\w.~-]+\.[A-Za-z0-9]{1,8}$/.test(t)) push(t);
    }
  }

  return [...new Set(files)].slice(0, 40);
}

/**
 * GET /api/lessons/match — fast lookup of matching lessons
 */
function fetchLessonMatches(toolName, toolInputPreview, project, opts = {}) {
  return new Promise((resolve) => {
    const params = new URLSearchParams({
      tool_name: toolName,
      tool_input_preview: toolInputPreview.slice(0, 1000),
    });
    if (project) params.set('project', project);
    // trigger_on defaults to 'input' server-side. file_scope lessons are
    // invisible unless we ask for them explicitly AND send modified_files —
    // that mismatch made every file_scope lesson unreachable from this hook.
    if (opts.triggerOn) params.set('trigger_on', opts.triggerOn);
    if (opts.modifiedFiles && opts.modifiedFiles.length) {
      params.set('modified_files', opts.modifiedFiles.join(','));
    }

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

// ── Main ────────────────────────────────────────────

const input = readStdin();
if (!input) {
  output({});
}
if (!PRE_TOOL_HINTS_ENABLED) {
  debug('Pre-tool lesson hints disabled');
  output({});
}

const toolName = input.tool_name || '';
const toolInput = input.tool_input || {};
const cwd = input.cwd || process.cwd();
const project = cwd;

debug(`tool=${toolName} project=${project}`);

const toolInputPreview = extractToolInputPreview(toolName, toolInput);
debug(`preview=${toolInputPreview.slice(0, 100)}`);

// Once-per-session reminder when no lessons match. Marker lives in the OS
// temp dir keyed by session_id so it auto-clears with reboots and never
// collides across sessions. If no session_id is provided, suppress the
// reminder rather than reminding on every tool call.
function shouldEmitEmptyReminder(sessionId) {
  if (!sessionId) return false;
  const safeId = String(sessionId).replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);
  const marker = path.join(os.tmpdir(), `agent-memory-reminded-${safeId}`);
  try {
    if (fs.existsSync(marker)) return false;
    fs.writeFileSync(marker, String(Date.now()));
    return true;
  } catch {
    return false;
  }
}

const sessionId = input.session_id || '';

(async () => {
  // Query BOTH trigger types. The server filters on trigger_on, so a single
  // default ('input') request can never surface a file_scope lesson.
  const modifiedFiles = extractModifiedFiles(toolName, toolInput);
  debug(`files=${modifiedFiles.slice(0, 5).join(',')}`);

  const requests = [
    fetchLessonMatches(toolName, toolInputPreview, project, { triggerOn: 'input' }),
  ];
  if (modifiedFiles.length) {
    requests.push(
      fetchLessonMatches(toolName, toolInputPreview, project, {
        triggerOn: 'file_scope',
        modifiedFiles,
      })
    );
  }

  const results = await Promise.all(requests);

  // Merge, de-duplicate by id, critical first, cap at 5 to protect the
  // systemMessage budget (same cap the server applies per-query).
  const severityRank = { critical: 0, warning: 1, info: 2 };
  const seen = new Set();
  const matches = [];
  for (const lesson of results.flat()) {
    if (!lesson || typeof lesson.id === 'undefined' || seen.has(lesson.id)) continue;
    seen.add(lesson.id);
    matches.push(lesson);
  }
  matches.sort((a, b) => (severityRank[a.severity] ?? 3) - (severityRank[b.severity] ?? 3));
  matches.splice(5);

  if (!Array.isArray(matches) || matches.length === 0) {
    debug('No lesson matches');
    if (shouldEmitEmptyReminder(sessionId)) {
      output({
        systemMessage: 'agent-memory: no active lessons match this tool call. For recent project context, call the `search` skill (semantic+text search of past observations) or `timeline` (context window around an anchor). Pass `project="<cwd>"`.',
      });
    } else {
      output({});
    }
    return;
  }

  debug(`${matches.length} lesson(s) matched`);

  // Build warning message
  const severity_icons = { critical: 'CRITICAL', warning: 'WARNING', info: 'INFO' };
  const lines = matches.map((lesson) => {
    const icon = severity_icons[lesson.severity] || 'LESSON';
    const scope = lesson.project_name ? `[${lesson.project_name}]` : '[global]';
    return `${icon} ${scope}: ${lesson.rule}`;
  });

  const systemMessage = `## Active Lessons\n${lines.join('\n')}`;

  // Fire-and-forget trigger tracking
  for (const lesson of matches) {
    trackTrigger(lesson.id);
  }

  output({ systemMessage });
})();
