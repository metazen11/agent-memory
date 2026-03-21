#!/usr/bin/env node
const fs = require('fs');
const http = require('http');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

const SERVER_BASE = process.env.AGENT_MEMORY_SERVER || 'http://localhost:3377';
const STATE_DIR = path.join(process.cwd(), '.agent-memory-codex');
const SESSION_FILE = path.join(STATE_DIR, 'current-session.json');
const CONTEXT_FILE = path.join(STATE_DIR, 'session-context.md');
const SPOOL_DIR = path.join(STATE_DIR, 'spool');
const LESSONS_FILE = path.join(STATE_DIR, 'lessons.snapshot.json');
const RECENT_FILE = path.join(STATE_DIR, 'recent.snapshot.json');
const WATCHER_PID_FILE = path.join(STATE_DIR, 'host-watch.pid');

function ensureStateDir() {
  fs.mkdirSync(STATE_DIR, { recursive: true });
}

function ensureSpoolDir() {
  ensureStateDir();
  fs.mkdirSync(SPOOL_DIR, { recursive: true });
}

function projectNameFromCwd(cwd = process.cwd()) {
  return path.basename(cwd) || cwd;
}

function generateSessionId() {
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  return `codex-${os.hostname()}-${process.pid}-${ts}`;
}

function requestJson(method, route, body, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const url = new URL(route, SERVER_BASE);
    const payload = body == null ? null : JSON.stringify(body);
    const req = http.request({
      hostname: url.hostname,
      port: url.port,
      path: `${url.pathname}${url.search}`,
      method,
      headers: payload ? {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      } : {},
      timeout: timeoutMs,
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        let parsed = null;
        try { parsed = data ? JSON.parse(data) : null; } catch {}
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve({ status: res.statusCode, data: parsed, raw: data });
          return;
        }
        const err = new Error(`HTTP ${res.statusCode} ${method} ${route}`);
        err.status = res.statusCode;
        err.data = parsed;
        err.raw = data;
        reject(err);
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error(`Timeout ${method} ${route}`)));
    if (payload) req.write(payload);
    req.end();
  });
}

function writeSessionState(state) {
  ensureStateDir();
  fs.writeFileSync(SESSION_FILE, JSON.stringify(state, null, 2) + '\n', 'utf8');
}

function readSessionState() {
  try {
    return JSON.parse(fs.readFileSync(SESSION_FILE, 'utf8'));
  } catch {
    return null;
  }
}

function writeContext(text) {
  ensureStateDir();
  fs.writeFileSync(CONTEXT_FILE, text, 'utf8');
}

function writeJsonFile(file, data) {
  ensureStateDir();
  fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

function readJsonFile(file, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function findRepoRoot(start = __dirname) {
  let dir = start;
  for (let i = 0; i < 6; i++) {
    if (fs.existsSync(path.join(dir, 'hooks', 'ensure-services.js')) && fs.existsSync(path.join(dir, 'mcp_server.py'))) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return process.cwd();
}

function runEnsureServices() {
  const root = findRepoRoot(__dirname);
  const script = path.join(root, 'hooks', 'ensure-services.js');
  if (!fs.existsSync(script)) return { ok: false, reason: 'missing_ensure_services' };
  const res = spawnSync('node', [script], {
    cwd: root,
    encoding: 'utf8',
    timeout: 90000,
    env: process.env,
  });
  return {
    ok: res.status === 0,
    status: res.status,
    stdout: (res.stdout || '').trim(),
    stderr: (res.stderr || '').trim(),
  };
}

function saveSpooledQueuePayload(payload) {
  ensureSpoolDir();
  const file = path.join(SPOOL_DIR, `${Date.now()}-${process.pid}.json`);
  fs.writeFileSync(file, JSON.stringify(payload) + '\n', 'utf8');
  return file;
}

function listSpooledPayloadFiles() {
  try {
    return fs.readdirSync(SPOOL_DIR)
      .filter((f) => f.endsWith('.json'))
      .sort()
      .map((f) => path.join(SPOOL_DIR, f));
  } catch {
    return [];
  }
}

async function postQueuePayload(payload, timeoutMs = 1500) {
  return requestJson('POST', '/api/queue', payload, timeoutMs);
}

async function drainSpooledQueue() {
  const files = listSpooledPayloadFiles();
  let drained = 0;
  for (const file of files) {
    const payload = readJsonFile(file, null);
    if (!payload) {
      try { fs.unlinkSync(file); } catch {}
      continue;
    }
    try {
      await postQueuePayload(payload, 2500);
      drained += 1;
      try { fs.unlinkSync(file); } catch {}
    } catch {
      break;
    }
  }
  return drained;
}

async function refreshSnapshots({ projectPath, projectName }) {
  const [recentResp, lessonsResp] = await Promise.all([
    requestJson('POST', '/api/observations/search', {
      query: 'recent work',
      project: projectPath,
      limit: 8,
      mode: 'hybrid',
    }, 5000).catch(() => ({ data: { observations: [] } })),
    requestJson('GET', `/api/lessons?project=${encodeURIComponent(projectPath)}&active=true&limit=25`, null, 3000)
      .catch(() => ({ data: [] })),
  ]);

  const recent = recentResp.data || { observations: [] };
  const lessons = Array.isArray(lessonsResp.data) ? lessonsResp.data : [];
  writeJsonFile(RECENT_FILE, {
    generated_at: new Date().toISOString(),
    project_path: projectPath,
    project: projectName,
    observations: recent.observations || [],
  });
  writeJsonFile(LESSONS_FILE, {
    generated_at: new Date().toISOString(),
    project_path: projectPath,
    project: projectName,
    lessons,
  });
  return { recent, lessons };
}

function formatRecentObservations(searchResp) {
  const observations = searchResp?.observations || [];
  if (!observations.length) return '## Recent Memory\n\nNo recent observations found.\n';
  const lines = ['## Recent Memory', ''];
  for (const obs of observations.slice(0, 8)) {
    const kind = obs.type || 'discovery';
    const title = obs.title || '(untitled)';
    lines.push(`- [${kind}] ${title}`);
    if (obs.narrative) lines.push(`  ${String(obs.narrative).replace(/\s+/g, ' ').slice(0, 220)}`);
  }
  lines.push('');
  return lines.join('\n');
}

function formatLessons(lessons) {
  if (!Array.isArray(lessons) || lessons.length === 0) return '## Active Lessons\n\nNo active lessons matched for this project.\n';
  const lines = ['## Active Lessons', ''];
  for (const l of lessons.slice(0, 10)) {
    lines.push(`- [${l.severity || 'warning'}] ${l.title}: ${l.rule}`);
  }
  lines.push('');
  return lines.join('\n');
}

function compileLessonMatchesFromSnapshot({ toolName, toolInputPreview, projectPath }) {
  const snapshot = readJsonFile(LESSONS_FILE, { lessons: [] });
  const lessons = Array.isArray(snapshot?.lessons) ? snapshot.lessons : [];
  const matches = [];
  for (const l of lessons) {
    if (!l || l.active === false) continue;
    if (l.trigger_tool && l.trigger_tool !== toolName) continue;
    const scopedProject = l.project_name || null;
    if (projectPath && l.project_name && !projectPath.startsWith(l.project_name)) {
      // best-effort scope check based on stored project_name/path
      continue;
    }
    if (l.trigger_pattern) {
      try {
        const re = new RegExp(l.trigger_pattern, 'i');
        if (!re.test(toolInputPreview || '')) continue;
      } catch {
        continue;
      }
    }
    matches.push({
      id: l.id,
      title: l.title,
      rule: l.rule,
      severity: l.severity,
      project_name: scopedProject,
      trigger_count: l.trigger_count || 0,
      source: 'snapshot',
    });
    if (matches.length >= 5) break;
  }
  return matches;
}

module.exports = {
  SERVER_BASE,
  STATE_DIR,
  SESSION_FILE,
  CONTEXT_FILE,
  SPOOL_DIR,
  LESSONS_FILE,
  RECENT_FILE,
  WATCHER_PID_FILE,
  ensureStateDir,
  ensureSpoolDir,
  projectNameFromCwd,
  generateSessionId,
  requestJson,
  postQueuePayload,
  writeSessionState,
  readSessionState,
  writeContext,
  writeJsonFile,
  readJsonFile,
  runEnsureServices,
  saveSpooledQueuePayload,
  listSpooledPayloadFiles,
  drainSpooledQueue,
  refreshSnapshots,
  compileLessonMatchesFromSnapshot,
  formatRecentObservations,
  formatLessons,
};
