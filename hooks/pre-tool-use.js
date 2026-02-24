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
const path = require('path');

const SERVER_BASE = 'http://localhost:3377';
const DEBUG = process.env.AGENT_MEMORY_DEBUG === '1';

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
 * GET /api/lessons/match — fast lookup of matching lessons
 */
function fetchLessonMatches(toolName, toolInputPreview, project) {
  return new Promise((resolve) => {
    const params = new URLSearchParams({
      tool_name: toolName,
      tool_input_preview: toolInputPreview.slice(0, 1000),
    });
    if (project) params.set('project', project);

    const url = new URL(`${SERVER_BASE}/api/lessons/match?${params}`);
    const req = http.get({
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
    headers: { 'Content-Type': 'application/json', 'Content-Length': 2 },
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

const toolName = input.tool_name || '';
const toolInput = input.tool_input || {};
const cwd = input.cwd || process.cwd();
const project = path.basename(cwd);

debug(`tool=${toolName} project=${project}`);

const toolInputPreview = extractToolInputPreview(toolName, toolInput);
debug(`preview=${toolInputPreview.slice(0, 100)}`);

(async () => {
  const matches = await fetchLessonMatches(toolName, toolInputPreview, project);

  if (!Array.isArray(matches) || matches.length === 0) {
    debug('No lesson matches');
    output({});
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
