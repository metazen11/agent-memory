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

// User-visible notices (captured by session-start.js via stdout)
function notice(msg) {
  console.log(`[agent-memory] ${msg}`);
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

function getExternalDatabaseUrl() {
  return readEnvVar('DATABASE_URL') || readEnvVar('AGENT_MEMORY_DATABASE_URL') || '';
}

function isExternalDatabase() {
  return !!getExternalDatabaseUrl();
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
    notice('Memory server not installed — run `node install.js` first');
    debug(`uvicorn not found at ${UVICORN} — run install.js first`);
    return false;
  }

  notice('Starting memory server (FastAPI)...');
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
      notice('Memory server ready on port 3377');
      debug('FastAPI server healthy');
      return true;
    }
  }

  notice('Memory server failed to start within 15s');
  debug('FastAPI server did not become healthy in 15s');
  return false;
}

// ── External database (BYOP) ─────────────────────────────────

function ensureExternalDb() {
  const dbUrl = getExternalDatabaseUrl();
  // Parse port from DATABASE_URL (postgresql://user:pass@host:port/db)
  const portMatch = dbUrl.match(/:(\d+)\//);
  const port = portMatch ? portMatch[1] : '5432';

  // Check if something is listening on that port
  const listening = run(`lsof -i :${port} -sTCP:LISTEN -t`, { timeout: 3000 });
  if (listening) {
    debug(`External database port ${port} is listening`);
    return true;
  }

  notice(`Database port ${port} is not listening — looking for Docker container...`);
  debug(`External database port ${port} not listening — looking for Docker container`);

  // Check if Docker daemon is running
  const dockerOk = run('docker info', { timeout: 5000 });
  if (!dockerOk) {
    notice('Docker daemon is not running — cannot start database');
    debug('Docker daemon not running — cannot start external DB container');
    return false;
  }

  // Find a stopped container that maps to this port
  const containers = run(
    `docker ps -a --filter "status=exited" --filter "status=created" --format "{{.ID}} {{.Names}} {{.Ports}}" 2>/dev/null`
  );
  // Also check running containers with port mapping (might be restarting)
  const allContainers = run(
    `docker ps -a --format "{{.ID}} {{.Names}}" 2>/dev/null`
  );

  // Try to find container by inspecting port bindings for all containers
  const ids = allContainers.split('\n').filter(Boolean).map(l => l.split(' ')[0]);
  let targetContainer = '';
  let targetName = '';

  for (const id of ids) {
    const portBindings = run(`docker inspect --format='{{json .HostConfig.PortBindings}}' ${id}`, { timeout: 3000 });
    if (portBindings.includes(`"HostPort":"${port}"`)) {
      const name = run(`docker inspect --format='{{.Name}}' ${id}`, { timeout: 3000 }).replace(/^\//, '');
      const status = run(`docker inspect --format='{{.State.Status}}' ${id}`, { timeout: 3000 });
      debug(`Found container '${name}' (${id}) mapped to port ${port}, status: ${status}`);
      if (status !== 'running') {
        notice(`Found stopped container '${name}' — restarting...`);
        targetContainer = id;
        targetName = name;
      } else {
        // Running but port not yet ready — just wait
        notice(`Container '${name}' is running but port ${port} not ready — waiting...`);
        debug(`Container already running, waiting for port ${port}`);
        return waitForPort(port, name);
      }
      break;
    }
  }

  if (!targetContainer) {
    notice(`No Docker container found for port ${port} — database is unavailable`);
    debug(`No Docker container found for port ${port}`);
    return false;
  }

  debug(`Starting container ${targetContainer}...`);
  run(`docker start ${targetContainer}`, { timeout: 30000 });

  return waitForPort(port, targetName);
}

function waitForPort(port, containerName) {
  for (let i = 0; i < 30; i++) {
    const listening = run(`lsof -i :${port} -sTCP:LISTEN -t`, { timeout: 3000 });
    if (listening) {
      const label = containerName ? `'${containerName}' is` : `Port ${port} is`;
      notice(`${label} ready`);
      debug(`Port ${port} is now listening`);
      return true;
    }
    run('sleep 1');
  }
  notice(`Database did not become available on port ${port} after 30s`);
  debug(`Port ${port} did not become available in 30s`);
  return false;
}

// ── Main ──────────────────────────────────────────────────────

if (isExternalDatabase()) {
  debug('Using external database (BYOP)');
  const dbOk = ensureExternalDb();
  if (!dbOk) {
    console.error('Failed to ensure external database is running');
    process.exit(1);
  }
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
