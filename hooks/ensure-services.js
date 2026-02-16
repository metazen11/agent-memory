#!/usr/bin/env node
/**
 * ensure-services.js — Start Docker + FastAPI if not running
 *
 * Called by session-start.js as a child process.
 * Resolves the install directory from its own symlink target.
 *
 * Exit 0: all services healthy
 * Exit 1: failed to start services
 */

const fs   = require('fs');
const path = require('path');
const { execSync, spawn } = require('child_process');

const DEBUG = process.env.AGENT_MEMORY_DEBUG !== '0';
function debug(msg) {
  if (DEBUG) console.error(`[agent-memory:ensure-services] ${msg}`);
}

function run(cmd, opts = {}) {
  try {
    return execSync(cmd, {
      encoding: 'utf8',
      timeout: opts.timeout || 30000,
      stdio: opts.stdio || ['pipe', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return '';
  }
}

// ── Resolve install directory ─────────────────────────────────
// This file is symlinked from ~/.claude/hooks/ → agentMemory/hooks/
// We need to find the agentMemory root directory.

function findInstallDir() {
  // Resolve symlink to get real path
  let realPath;
  try {
    realPath = fs.realpathSync(__filename);
  } catch {
    realPath = __filename;
  }

  // Walk up from hooks/ensure-services.js to find docker/docker-compose.yml
  let dir = path.dirname(realPath);  // hooks/
  dir = path.dirname(dir);           // agentMemory/

  if (fs.existsSync(path.join(dir, 'docker', 'docker-compose.yml'))) {
    return dir;
  }

  // Fallback: try one more level up
  const parent = path.dirname(dir);
  if (fs.existsSync(path.join(parent, 'docker', 'docker-compose.yml'))) {
    return parent;
  }

  debug(`Cannot find install dir from ${realPath}`);
  return null;
}

const INSTALL_DIR = findInstallDir();
if (!INSTALL_DIR) {
  console.error('Cannot find agent-memory install directory');
  process.exit(1);
}

const PLATFORM    = require('os').platform();
const VENV_BIN    = path.join(INSTALL_DIR, '.venv', PLATFORM === 'win32' ? 'Scripts' : 'bin');
const PYTHON      = path.join(VENV_BIN, 'python');
const UVICORN     = path.join(VENV_BIN, 'uvicorn');
const COMPOSE     = path.join(INSTALL_DIR, 'docker', 'docker-compose.yml');
const PID_FILE    = path.join(INSTALL_DIR, '.server.pid');
const LOG_DIR     = path.join(INSTALL_DIR, 'logs');
const SERVER_LOG  = path.join(LOG_DIR, 'server.log');

const ENV_FILE    = path.join(INSTALL_DIR, '.env');

debug(`Install dir: ${INSTALL_DIR}`);

function readEnvVar(key) {
  try {
    const content = fs.readFileSync(ENV_FILE, 'utf8');
    const match = content.match(new RegExp(`^${key}=(.*)$`, 'm'));
    if (!match) return '';
    return match[1].trim().replace(/^["']|["']$/g, '');
  } catch {
    return '';
  }
}

function isExternalDatabase() {
  const dbUrl = readEnvVar('DATABASE_URL') || readEnvVar('AGENT_MEMORY_DATABASE_URL');
  return !!dbUrl;
}

// ── Health checks ─────────────────────────────────────────────

function isServerHealthy() {
  const resp = run(`curl -s --max-time 2 http://localhost:3377/api/health`);
  try {
    const data = JSON.parse(resp);
    return data.status === 'ok' || data.status === 'degraded';
  } catch {
    return false;
  }
}

function isContainerRunning() {
  const out = run('docker ps --filter name=agent-memory-db --format "{{.Status}}"');
  return out.toLowerCase().includes('up');
}

// ── Docker ────────────────────────────────────────────────────

function ensureDocker() {
  // Check daemon
  const dockerOk = run('docker info', { timeout: 5000 });
  if (!dockerOk) {
    debug('Docker daemon not running');
    return false;
  }

  if (isContainerRunning()) {
    debug('PostgreSQL container already running');
    return true;
  }

  debug('Starting PostgreSQL container...');
  run(`docker compose -f "${COMPOSE}" up -d`, { timeout: 60000 });

  // Wait for pg_isready
  for (let i = 0; i < 30; i++) {
    const ready = run('docker exec agent-memory-db pg_isready -U ${POSTGRES_USER:-agentmem}');
    if (ready.includes('accepting connections')) {
      debug('PostgreSQL ready');
      return true;
    }
    run('sleep 1');
  }

  debug('PostgreSQL did not become ready');
  return false;
}

// ── FastAPI server ────────────────────────────────────────────

function ensureServer() {
  if (isServerHealthy()) {
    debug('FastAPI server already healthy');
    return true;
  }

  // Check if venv exists
  if (!fs.existsSync(UVICORN)) {
    debug(`uvicorn not found at ${UVICORN} — run install.js first`);
    return false;
  }

  debug('Starting FastAPI server...');

  // Ensure log directory
  if (!fs.existsSync(LOG_DIR)) {
    fs.mkdirSync(LOG_DIR, { recursive: true });
  }

  const logStream = fs.openSync(SERVER_LOG, 'a');
  const child = spawn(UVICORN, ['app.main:app', '--port', '3377', '--host', '0.0.0.0'], {
    cwd: INSTALL_DIR,
    detached: true,
    stdio: ['ignore', logStream, logStream],
    env: { ...process.env, PATH: `${VENV_BIN}:${process.env.PATH}` },
  });
  child.unref();
  fs.writeFileSync(PID_FILE, String(child.pid), 'utf8');
  fs.closeSync(logStream);
  debug(`Spawned uvicorn (PID ${child.pid})`);

  // Wait for health endpoint
  for (let i = 0; i < 15; i++) {
    run('sleep 1');
    if (isServerHealthy()) {
      debug('FastAPI server healthy');
      return true;
    }
  }

  debug('FastAPI server did not become healthy in 15s');
  return false;
}

// ── Main ──────────────────────────────────────────────────────

if (isExternalDatabase()) {
  debug('Using external database — skipping Docker');
} else {
  const dbOk = ensureDocker();
  if (!dbOk) {
    console.error('Failed to start PostgreSQL');
    process.exit(1);
  }
}

const serverOk = ensureServer();
if (!serverOk) {
  console.error('Failed to start FastAPI server');
  process.exit(1);
}

debug('All services healthy');
process.exit(0);
