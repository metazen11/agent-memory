#!/usr/bin/env node
// ─────────────────────────────────────────────────────────────
// install.js  —  Cross-platform installer for agent-memory
// ─────────────────────────────────────────────────────────────
//
//  Usage:
//    node install.js              # Full setup + install
//    node install.js --uninstall  # Remove hooks, MCP, skills
//    node install.js --status     # Show what's installed and running
//    node install.js --start      # Start services (Docker + FastAPI)
//    node install.js --stop       # Stop services
//
// ─────────────────────────────────────────────────────────────

const fs   = require('fs');
const path = require('path');
const os   = require('os');
const { execSync, spawn } = require('child_process');

// ── Constants ─────────────────────────────────────────────────

const INSTALL_DIR = path.resolve(__dirname);
const HOME        = os.homedir();
const PLATFORM    = os.platform();       // darwin | linux | win32
const IS_WSL      = PLATFORM === 'linux' && (() => {
  try { return fs.readFileSync('/proc/version', 'utf8').toLowerCase().includes('microsoft'); }
  catch { return false; }
})();

const VENV_DIR    = path.join(INSTALL_DIR, '.venv');
const PIP         = path.join(VENV_DIR, PLATFORM === 'win32' ? 'Scripts' : 'bin', 'pip');
const PYTHON      = path.join(VENV_DIR, PLATFORM === 'win32' ? 'Scripts' : 'bin', 'python');
const UVICORN     = path.join(VENV_DIR, PLATFORM === 'win32' ? 'Scripts' : 'bin', 'uvicorn');
const PID_FILE    = path.join(INSTALL_DIR, '.server.pid');
const LOG_DIR     = path.join(INSTALL_DIR, 'logs');
const SERVER_LOG  = path.join(LOG_DIR, 'server.log');
const ENV_FILE    = path.join(INSTALL_DIR, '.env');
const ENV_EXAMPLE = path.join(INSTALL_DIR, '.env.example');
const COMPOSE_FILE = path.join(INSTALL_DIR, 'docker', 'docker-compose.yml');

const TOTAL_STEPS = 11;

const HOOK_FILES = [
  'post-tool-use.js',
  'session-start.js',
  'session-end.js',
];

const SKILL_FILES = [
  { src: 'skills/mem-search/SKILL.md', dest: 'mem-search/SKILL.md' },
];

// Agent targets — extensible for future agents
const AGENTS = {
  claude: {
    detect: () => fs.existsSync(path.join(HOME, '.claude')),
    hooksDir: path.join(HOME, '.claude', 'hooks'),
    settingsFile: path.join(HOME, '.claude', 'settings.json'),
    mcpFile: path.join(HOME, '.claude', '.mcp.json'),
    skillsDir: path.join(HOME, '.claude', 'skills'),
    hookEntries: [
      {
        event: 'PostToolUse',
        entry: {
          matcher: 'Read|Edit|Write|Bash|Grep|Glob|NotebookEdit|WebFetch|WebSearch',
          hooks: [{
            type: 'command',
            command: `node ~/.claude/hooks/agent-memory-post-tool-use.js`,
            timeout: 5,
          }],
        },
      },
      {
        event: 'SessionStart',
        entry: {
          hooks: [{
            type: 'command',
            command: `node ~/.claude/hooks/agent-memory-session-start.js`,
            timeout: 60,
          }],
        },
      },
      {
        event: 'Stop',
        entry: {
          hooks: [{
            type: 'command',
            command: `node ~/.claude/hooks/agent-memory-session-end.js`,
            timeout: 10,
          }],
        },
      },
    ],
  },
};

// ── Helpers ───────────────────────────────────────────────────

const LOG_PREFIX = '  ';
const ok   = (msg) => console.log(`${LOG_PREFIX}\x1b[32m✓\x1b[0m  ${msg}`);
const fail = (msg) => console.log(`${LOG_PREFIX}\x1b[31m✗\x1b[0m  ${msg}`);
const info = (msg) => console.log(`${LOG_PREFIX}   ${msg}`);
const skip = (msg) => console.log(`${LOG_PREFIX}\x1b[33m-\x1b[0m  ${msg}`);
const head = (msg) => console.log(`\n\x1b[1m  ${msg}\x1b[0m`);
const step = (n, total, msg) => console.log(`\n\x1b[36m  [${n}/${total}]\x1b[0m \x1b[1m${msg}\x1b[0m`);
const dots = (msg) => process.stdout.write(`${LOG_PREFIX}   ${msg}`);

function run(cmd, opts = {}) {
  try {
    return execSync(cmd, { encoding: 'utf8', timeout: opts.timeout || 30000, ...opts }).trim();
  } catch (e) {
    if (opts.ignoreError) return '';
    throw e;
  }
}

function ensureDir(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function symlink(src, dest) {
  try { fs.unlinkSync(dest); } catch {}
  fs.symlinkSync(src, dest);
}

function removeSymlink(dest) {
  try {
    const stat = fs.lstatSync(dest);
    if (stat.isSymbolicLink()) {
      fs.unlinkSync(dest);
      return true;
    }
  } catch {}
  return false;
}

function readJSON(file) {
  if (!fs.existsSync(file)) return {};
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJSON(file, obj) {
  fs.writeFileSync(file, JSON.stringify(obj, null, 2) + '\n', 'utf8');
}

function generatePassword(length = 24) {
  const chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const bytes = require('crypto').randomBytes(length);
  return Array.from(bytes).map(b => chars[b % chars.length]).join('');
}

function readEnvVar(key) {
  if (!fs.existsSync(ENV_FILE)) return '';
  const content = fs.readFileSync(ENV_FILE, 'utf8');
  const match = content.match(new RegExp(`^${key}=(.*)$`, 'm'));
  if (!match) return '';
  return match[1].trim().replace(/^["']|["']$/g, '');
}

function isExternalDatabase() {
  const dbUrl = readEnvVar('DATABASE_URL') || readEnvVar('AGENT_MEMORY_DATABASE_URL');
  return !!dbUrl;
}

function httpGet(url, timeoutMs = 3000) {
  try {
    return run(`curl -s --max-time ${Math.ceil(timeoutMs/1000)} "${url}"`, { ignoreError: true });
  } catch {
    return '';
  }
}

function isServerRunning() {
  const resp = httpGet('http://localhost:3377/api/health', 2000);
  try {
    const data = JSON.parse(resp);
    return data.status === 'ok' || data.status === 'degraded';
  } catch {
    return false;
  }
}

function isContainerRunning() {
  try {
    const out = run('docker ps --filter name=agent-memory-db --format "{{.Status}}"', { ignoreError: true });
    return out.toLowerCase().includes('up');
  } catch {
    return false;
  }
}

// ── Prerequisites ─────────────────────────────────────────────

function checkPrereqs() {
  head('Checking prerequisites');
  info('Scanning your system for required tools...');
  console.log('');

  const errors = [];
  const externalDb = fs.existsSync(ENV_FILE) && isExternalDatabase();

  // Docker (skip if using external database)
  if (externalDb) {
    skip('Docker — not required (using external DATABASE_URL)');
  } else {
    dots('Looking for Docker... ');
    try {
      const ver = run('docker --version', { ignoreError: true });
      if (ver) {
        console.log('');
        ok(`Found Docker ${ver.replace('Docker version ', '').split(',')[0]}`);
      } else {
        throw new Error();
      }
    } catch {
      console.log('');
      fail('Docker not found — required for PostgreSQL database');
      info('  Or set DATABASE_URL in .env to use an external PostgreSQL');
      if (PLATFORM === 'darwin') info('  Install Docker: brew install --cask docker');
      else if (IS_WSL) info('  Install Docker: Docker Desktop for Windows with WSL2 backend');
      else info('  Install Docker: sudo apt install docker.io docker-compose-plugin');
      errors.push('docker');
    }

    // Docker daemon
    if (!errors.includes('docker')) {
      dots('Checking Docker daemon... ');
      try {
        run('docker info', { ignoreError: false, timeout: 5000 });
        console.log('');
        ok('Docker daemon is running');
      } catch {
        console.log('');
        fail('Docker daemon is not running');
        if (PLATFORM === 'darwin') info('  Start Docker Desktop from Applications');
        else info('  Run: sudo systemctl start docker');
        errors.push('docker-daemon');
      }
    }
  }

  // Python
  dots('Looking for Python 3.12+... ');
  let pythonCmd = '';
  for (const cmd of ['python3.12', 'python3', 'python']) {
    try {
      const ver = run(`${cmd} --version`, { ignoreError: true });
      if (ver) {
        const match = ver.match(/(\d+)\.(\d+)/);
        if (match && parseInt(match[1]) >= 3 && parseInt(match[2]) >= 12) {
          pythonCmd = cmd;
          console.log('');
          ok(`Found Python ${ver.replace('Python ', '')} (${cmd})`);
          break;
        }
      }
    } catch {}
  }
  if (!pythonCmd) {
    console.log('');
    fail('Python 3.12+ not found — required for the memory server');
    if (PLATFORM === 'darwin') info('  Install: brew install python@3.12');
    else info('  Install: sudo apt install python3.12 python3.12-venv');
    errors.push('python');
  }

  // Node.js (we're already running, but show version)
  ok(`Found Node.js ${process.version}`);

  // Claude Code
  dots('Looking for Claude Code... ');
  const claudeDir = path.join(HOME, '.claude');
  if (fs.existsSync(claudeDir)) {
    console.log('');
    ok('Found Claude Code (~/.claude/)');
  } else {
    console.log('');
    skip('Claude Code not detected — hooks and MCP will not be installed');
    info('  Install Claude Code first, then re-run this installer');
  }

  const dockerRequired = !externalDb && (errors.includes('docker') || errors.includes('docker-daemon'));
  if (dockerRequired || errors.includes('python')) {
    console.log('');
    fail('Missing critical prerequisites. Install them and retry.');
    process.exit(1);
  }

  return { pythonCmd };
}

// ── Install steps ─────────────────────────────────────────────

function createVenv(pythonCmd) {
  step(1, TOTAL_STEPS, 'Python virtual environment');

  if (fs.existsSync(PYTHON)) {
    const ver = run(`"${PYTHON}" --version`, { ignoreError: true });
    ok(`Virtual environment already exists (${ver || 'ready'})`);
    return;
  }

  info('Creating isolated Python environment in .venv/...');
  run(`${pythonCmd} -m venv "${VENV_DIR}"`);
  ok('Created virtual environment at .venv/');
}

function installDeps() {
  step(2, TOTAL_STEPS, 'Python dependencies');

  info('Installing packages: FastAPI, sentence-transformers, asyncpg, llama-cpp-python...');
  info('This may take a few minutes on first run.');
  try {
    run(`"${PIP}" install -q -r "${path.join(INSTALL_DIR, 'requirements.txt')}"`, { timeout: 300000 });
    ok('All Python dependencies installed');
  } catch (e) {
    fail('pip install failed');
    info(e.message.split('\n').slice(-3).join('\n'));
    process.exit(1);
  }
}

function downloadEmbeddingModel() {
  step(4, TOTAL_STEPS, 'Embedding model (for semantic search)');

  info('Model: nomic-ai/nomic-embed-text-v1.5 (768 dimensions)');
  info('Downloading from Hugging Face (~400MB, cached after first download)...');
  info('This converts your text into vectors for similarity search.');
  try {
    run(
      `"${PYTHON}" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True); print(f'Loaded: {m.get_sentence_embedding_dimension()}d')"`,
      { timeout: 600000 }
    );
    ok('Embedding model downloaded and cached');
  } catch (e) {
    skip('Embedding model download failed — will retry automatically on first use');
  }
}

function downloadGGUFModel() {
  step(5, TOTAL_STEPS, 'Observation LLM (for extracting memories from tool calls)');

  // Check if already configured in .env
  if (fs.existsSync(ENV_FILE)) {
    const envContent = fs.readFileSync(ENV_FILE, 'utf8');
    const match = envContent.match(/^OBSERVATION_LLM_MODEL=(.+)$/m);
    if (match && match[1].trim() && fs.existsSync(match[1].trim())) {
      ok(`Local LLM already configured: ${path.basename(match[1].trim())}`);
      return;
    }
  }

  info('Model: Qwen2.5-1.5B-Instruct (quantized Q4_K_M)');
  info('Downloading from Hugging Face (~1GB, cached after first download)...');
  info('This small local LLM reads each tool call and extracts structured observations.');
  info('No API key needed — runs entirely on your machine.');
  try {
    const modelPath = run(
      `"${PYTHON}" -c "from huggingface_hub import hf_hub_download; print(hf_hub_download('Qwen/Qwen2.5-1.5B-Instruct-GGUF', 'qwen2.5-1.5b-instruct-q4_k_m.gguf'))"`,
      { timeout: 600000 }
    );
    if (modelPath && fs.existsSync(modelPath)) {
      ok(`Local LLM downloaded and cached`);
      // Write path into .env
      updateEnvVar('OBSERVATION_LLM_MODEL', modelPath);
      ok('Configured OBSERVATION_LLM_MODEL in .env');
    }
  } catch (e) {
    skip('Local LLM download failed — will fall back to Anthropic Haiku API');
    info('Set ANTHROPIC_API_KEY in .env to use the cloud fallback');
  }
}

function generateEnv() {
  step(3, TOTAL_STEPS, 'Environment configuration');

  if (fs.existsSync(ENV_FILE)) {
    ok('.env already exists — keeping current configuration');
    return;
  }

  info('Generating .env from template...');
  // Read template
  let content = fs.readFileSync(ENV_EXAMPLE, 'utf8');

  // Generate random password
  const password = generatePassword();
  content = content.replace(
    /^POSTGRES_PASSWORD=.*$/m,
    `POSTGRES_PASSWORD=${password}`
  );

  fs.writeFileSync(ENV_FILE, content, 'utf8');
  ok('Generated .env with secure random Postgres password');
  info('Edit .env to customize database settings or add API keys');
  info('To use an external PostgreSQL, set DATABASE_URL in .env and re-run');
}

function updateEnvVar(key, value) {
  if (!fs.existsSync(ENV_FILE)) return;
  let content = fs.readFileSync(ENV_FILE, 'utf8');
  const regex = new RegExp(`^${key}=.*$`, 'm');
  if (regex.test(content)) {
    content = content.replace(regex, `${key}=${value}`);
  } else {
    content += `\n${key}=${value}\n`;
  }
  fs.writeFileSync(ENV_FILE, content, 'utf8');
}

function startDocker() {
  step(6, TOTAL_STEPS, 'Database (PostgreSQL + pgvector)');

  if (isExternalDatabase()) {
    const dbUrl = readEnvVar('DATABASE_URL') || readEnvVar('AGENT_MEMORY_DATABASE_URL');
    const safeUrl = dbUrl.replace(/:([^@]+)@/, ':***@');
    ok(`Using external database: ${safeUrl}`);
    info('Skipping Docker — your DATABASE_URL will be used directly');
    info('Migrations will run in the next step to ensure schema is up to date');
    return;
  }

  if (isContainerRunning()) {
    ok('Database container agent-memory-db is already running');
    return;
  }

  info('Starting PostgreSQL 16 with pgvector extension via Docker...');
  info('Image: pgvector/pgvector:pg16 (will pull if not cached)');
  try {
    run(`docker compose -f "${COMPOSE_FILE}" up -d`, { timeout: 120000 });
  } catch (e) {
    fail('Failed to start database container');
    info(e.message.split('\n').slice(-3).join('\n'));
    process.exit(1);
  }

  // Read user/db from .env for pg_isready
  const pgUser = readEnvVar('POSTGRES_USER') || 'agentmem';
  const pgDb = readEnvVar('POSTGRES_DB') || 'agent_memory';

  // Wait for healthcheck
  dots('Waiting for PostgreSQL to accept connections');
  for (let i = 0; i < 30; i++) {
    try {
      const result = run(
        `docker exec agent-memory-db pg_isready -U ${pgUser} -d ${pgDb}`,
        { ignoreError: true }
      );
      if (result.includes('accepting connections')) {
        console.log('');
        ok('PostgreSQL is ready and accepting connections');
        return;
      }
    } catch {}
    process.stdout.write('.');
    run('sleep 1');
  }
  console.log('');
  fail('PostgreSQL did not become ready in 30s');
  info('Check Docker logs: docker logs agent-memory-db');
  process.exit(1);
}

function runMigrations() {
  step(7, TOTAL_STEPS, 'Database schema (migrations)');

  info('Running versioned SQL migrations against the database...');
  info('Migrations create tables, indexes, and the pgvector extension.');
  info('Already-applied migrations are skipped automatically.');
  try {
    const output = run(
      `"${PYTHON}" "${path.join(INSTALL_DIR, 'scripts', 'run_migrations.py')}"`,
      { timeout: 60000 }
    );
    if (output) {
      for (const line of output.split('\n')) {
        if (line.trim()) ok(line.trim());
      }
    }
  } catch (e) {
    fail('Migration failed');
    info(e.message.split('\n').slice(-5).join('\n'));
    info('Check database connectivity and try again');
    process.exit(1);
  }
}

function startServer() {
  step(8, TOTAL_STEPS, 'Memory server (FastAPI)');

  if (isServerRunning()) {
    ok('Memory server already running on http://localhost:3377');
    return;
  }

  ensureDir(LOG_DIR);

  info('Starting FastAPI server on port 3377...');
  info('The server processes tool calls, extracts observations, and handles search.');
  const logStream = fs.openSync(SERVER_LOG, 'a');
  const child = spawn(UVICORN, ['app.main:app', '--port', '3377', '--host', '0.0.0.0'], {
    cwd: INSTALL_DIR,
    detached: true,
    stdio: ['ignore', logStream, logStream],
    env: { ...process.env, PATH: `${path.dirname(PYTHON)}:${process.env.PATH}` },
  });
  child.unref();
  fs.writeFileSync(PID_FILE, String(child.pid), 'utf8');
  fs.closeSync(logStream);

  // Wait for health endpoint
  dots('Waiting for health check to pass');
  for (let i = 0; i < 15; i++) {
    if (isServerRunning()) {
      console.log('');
      ok(`Memory server running (PID ${child.pid}) at http://localhost:3377`);
      return;
    }
    process.stdout.write('.');
    run('sleep 1');
  }
  console.log('');
  fail('Server did not become ready in 15s');
  info(`Check logs: ${SERVER_LOG}`);
}

function registerMCP() {
  step(9, TOTAL_STEPS, 'MCP server (for in-session memory search)');

  info('The MCP server gives your agent tools: search, timeline, get_observations, save_memory');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) {
      skip(`${name} not detected — skipping MCP registration`);
      continue;
    }

    const mcpConfig = readJSON(agent.mcpFile);
    if (!mcpConfig.mcpServers) mcpConfig.mcpServers = {};

    mcpConfig.mcpServers['agent-memory'] = {
      type: 'stdio',
      command: PYTHON,
      args: [path.join(INSTALL_DIR, 'mcp_server.py')],
    };

    writeJSON(agent.mcpFile, mcpConfig);
    ok(`Registered MCP server in ${name} (${agent.mcpFile})`);
  }
}

function installHooks() {
  step(10, TOTAL_STEPS, 'Lifecycle hooks (auto-capture tool calls)');

  info('Hooks capture every tool call and auto-start services on session begin.');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) {
      skip(`${name} not detected — skipping hook installation`);
      continue;
    }

    info(`Installing hooks for ${name}...`);
    ensureDir(agent.hooksDir);

    // Symlink hook files
    for (const file of HOOK_FILES) {
      const src  = path.join(INSTALL_DIR, 'hooks', file);
      const dest = path.join(agent.hooksDir, `agent-memory-${file}`);
      symlink(src, dest);
      ok(`Linked ${path.basename(dest)}`);
    }

    // Register in settings
    info('Registering hooks in settings.json...');
    const settings = readJSON(agent.settingsFile);
    if (!settings.hooks) settings.hooks = {};

    let changed = false;
    for (const { event, entry } of agent.hookEntries) {
      if (!settings.hooks[event]) settings.hooks[event] = [];

      const cmd = entry.hooks[0].command;
      const exists = settings.hooks[event].some(
        (e) => e.hooks && e.hooks.some((h) => h.command === cmd)
      );
      if (exists) {
        skip(`${event} hook already registered`);
        continue;
      }

      settings.hooks[event].push(entry);
      ok(`Registered ${event} hook (timeout: ${entry.hooks[0].timeout}s)`);
      changed = true;
    }

    if (changed) writeJSON(agent.settingsFile, settings);
  }
}

function installSkills() {
  step(11, TOTAL_STEPS, 'Skills (/mem-search command)');

  info('The /mem-search command lets you search past sessions from the chat.');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect() || !agent.skillsDir) continue;

    for (const skill of SKILL_FILES) {
      const src  = path.join(INSTALL_DIR, skill.src);
      const dest = path.join(agent.skillsDir, skill.dest);
      ensureDir(path.dirname(dest));
      symlink(src, dest);
      ok(`Installed /mem-search skill for ${name}`);
    }
  }
}

// ── Uninstall ─────────────────────────────────────────────────

function uninstall() {
  console.log('');
  console.log('\x1b[1magent-memory — uninstalling\x1b[0m');
  console.log('─'.repeat(50));

  // Remove hooks and settings
  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) continue;

    head(`${name} hooks`);

    for (const file of HOOK_FILES) {
      const dest = path.join(agent.hooksDir, `agent-memory-${file}`);
      if (removeSymlink(dest)) ok(`Removed ${path.basename(dest)}`);
      else skip(`${path.basename(dest)} not found`);
    }

    // Remove from settings
    if (fs.existsSync(agent.settingsFile)) {
      const settings = readJSON(agent.settingsFile);
      let changed = false;
      for (const { event, entry } of agent.hookEntries) {
        const arr = settings.hooks?.[event];
        if (!arr) continue;
        const cmd = entry.hooks[0].command;
        const before = arr.length;
        settings.hooks[event] = arr.filter(
          (e) => !(e.hooks && e.hooks.some((h) => h.command === cmd))
        );
        if (settings.hooks[event].length < before) {
          ok(`Removed ${event} from settings`);
          changed = true;
        }
      }
      if (changed) writeJSON(agent.settingsFile, settings);
    }

    // Remove MCP registration
    if (fs.existsSync(agent.mcpFile)) {
      const mcpConfig = readJSON(agent.mcpFile);
      if (mcpConfig.mcpServers?.['agent-memory']) {
        delete mcpConfig.mcpServers['agent-memory'];
        writeJSON(agent.mcpFile, mcpConfig);
        ok('Removed MCP server registration');
      }
    }

    // Remove skills
    if (agent.skillsDir) {
      for (const skill of SKILL_FILES) {
        const dest = path.join(agent.skillsDir, skill.dest);
        if (removeSymlink(dest)) ok(`Removed skill ${skill.dest}`);
      }
    }
  }

  console.log('');
  console.log('─'.repeat(50));
  console.log('  Done. Restart your agent to deactivate.');
  console.log('');
}

// ── Status ────────────────────────────────────────────────────

function status() {
  console.log('');
  console.log('\x1b[1magent-memory — status\x1b[0m');
  console.log('─'.repeat(50));

  head('Services');
  if (isContainerRunning()) ok('PostgreSQL: running');
  else fail('PostgreSQL: stopped');

  if (isServerRunning()) ok('FastAPI: running on port 3377');
  else fail('FastAPI: stopped');

  head('Environment');
  if (fs.existsSync(VENV_DIR)) ok('.venv exists');
  else fail('.venv missing');

  if (fs.existsSync(ENV_FILE)) ok('.env exists');
  else fail('.env missing');

  head('Agents');
  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) {
      skip(`${name}: not installed`);
      continue;
    }

    // Check hooks
    const allHooksOk = HOOK_FILES.every(f => {
      const dest = path.join(agent.hooksDir, `agent-memory-${f}`);
      try { return fs.lstatSync(dest).isSymbolicLink(); } catch { return false; }
    });

    // Check MCP
    const mcpConfig = readJSON(agent.mcpFile);
    const mcpOk = !!mcpConfig.mcpServers?.['agent-memory'];

    // Check settings
    const settings = readJSON(agent.settingsFile);
    const hooksRegistered = agent.hookEntries.every(({ event, entry }) => {
      const arr = settings.hooks?.[event] || [];
      const cmd = entry.hooks[0].command;
      return arr.some(e => e.hooks && e.hooks.some(h => h.command === cmd));
    });

    if (allHooksOk && mcpOk && hooksRegistered) {
      ok(`${name}: fully configured`);
    } else {
      fail(`${name}: incomplete (hooks=${allHooksOk}, mcp=${mcpOk}, settings=${hooksRegistered})`);
    }
  }

  console.log('');
}

// ── Start / Stop ──────────────────────────────────────────────

function startServices() {
  console.log('');
  console.log('\x1b[1magent-memory — starting services\x1b[0m');
  startDocker();
  startServer();
  console.log('');
  ok('All services running');
  console.log('');
}

function stopServices() {
  console.log('');
  console.log('\x1b[1magent-memory — stopping services\x1b[0m');
  console.log('─'.repeat(50));

  head('FastAPI server');
  if (fs.existsSync(PID_FILE)) {
    const pid = fs.readFileSync(PID_FILE, 'utf8').trim();
    try {
      process.kill(parseInt(pid), 'SIGTERM');
      ok(`Stopped server (PID ${pid})`);
    } catch {
      skip(`PID ${pid} not running`);
    }
    fs.unlinkSync(PID_FILE);
  } else {
    skip('No .server.pid found');
  }

  head('Docker');
  if (isContainerRunning()) {
    try {
      run(`docker compose -f "${COMPOSE_FILE}" down`, { timeout: 30000 });
      ok('Stopped agent-memory-db');
    } catch {
      fail('docker compose down failed');
    }
  } else {
    skip('Container not running');
  }

  console.log('');
}

// ── Full install ──────────────────────────────────────────────

function install() {
  console.log('');
  console.log('\x1b[1m  ╔══════════════════════════════════════════════╗\x1b[0m');
  console.log('\x1b[1m  ║  agent-memory installer                     ║\x1b[0m');
  console.log('\x1b[1m  ║  Persistent cross-session memory for AI     ║\x1b[0m');
  console.log('\x1b[1m  ╚══════════════════════════════════════════════╝\x1b[0m');
  console.log('');
  info(`${TOTAL_STEPS} steps: prereqs → venv → deps → config → models → database → migrations → server → MCP → hooks → skills`);

  const { pythonCmd } = checkPrereqs();
  createVenv(pythonCmd);
  installDeps();
  generateEnv();
  downloadEmbeddingModel();
  downloadGGUFModel();
  startDocker();
  runMigrations();
  startServer();
  registerMCP();
  installHooks();
  installSkills();

  console.log('');
  console.log('\x1b[32m  ╔══════════════════════════════════════════════╗\x1b[0m');
  console.log('\x1b[32m  ║  Installation complete!                     ║\x1b[0m');
  console.log('\x1b[32m  ╚══════════════════════════════════════════════╝\x1b[0m');
  console.log('');
  ok('Memory server running on http://localhost:3377');
  ok('MCP tools registered — search, timeline, get_observations, save_memory');
  ok('Hooks installed — tool calls will be recorded automatically');
  ok('Restart your agent to activate everything');
  console.log('');
  info('Manage services:');
  info('  node install.js --status     Show what\'s running');
  info('  node install.js --start      Start Docker + FastAPI');
  info('  node install.js --stop       Stop everything');
  info('  node install.js --uninstall  Remove hooks, MCP, skills');
  console.log('');
}

// ── Migrate / Backup ────────────────────────────────────────

function migrateOnly() {
  console.log('');
  console.log('\x1b[1magent-memory — database migrations\x1b[0m');
  console.log('─'.repeat(50));

  const flags = [];
  if (args.includes('--dry-run')) flags.push('--dry-run');
  if (args.includes('--backup')) flags.push('--backup');

  const flagStr = flags.length ? ` ${flags.join(' ')}` : '';
  const desc = args.includes('--dry-run') ? 'DRY RUN — showing pending migrations (no changes)' : 'Running database migrations...';
  info(desc);

  try {
    const output = run(
      `"${PYTHON}" "${path.join(INSTALL_DIR, 'scripts', 'run_migrations.py')}"${flagStr}`,
      { timeout: 120000 }
    );
    if (output) {
      for (const line of output.split('\n')) {
        if (line.trim()) ok(line.trim());
      }
    }
  } catch (e) {
    fail('Migration failed');
    info(e.message.split('\n').slice(-5).join('\n'));
    process.exit(1);
  }
  console.log('');
}

function backupOnly() {
  console.log('');
  console.log('\x1b[1magent-memory — database backup\x1b[0m');
  console.log('─'.repeat(50));
  info('Creating timestamped backup of all mem_* tables...');

  try {
    const output = run(
      `"${PYTHON}" "${path.join(INSTALL_DIR, 'scripts', 'run_migrations.py')}" --backup-only`,
      { timeout: 120000 }
    );
    if (output) {
      for (const line of output.split('\n')) {
        if (line.trim()) ok(line.trim());
      }
    }
  } catch (e) {
    fail('Backup failed');
    info(e.message.split('\n').slice(-5).join('\n'));
    process.exit(1);
  }
  console.log('');
}

// ── CLI ──────────────────────────────────────────────────────

const args = process.argv.slice(2);

if (args.includes('--uninstall') || args.includes('-u')) {
  uninstall();
} else if (args.includes('--status') || args.includes('-s')) {
  status();
} else if (args.includes('--start')) {
  startServices();
} else if (args.includes('--stop')) {
  stopServices();
} else if (args.includes('--migrate')) {
  migrateOnly();
} else if (args.includes('--backup')) {
  backupOnly();
} else if (args.includes('--help') || args.includes('-h')) {
  console.log(`
agent-memory installer

Usage:
  node install.js              Full setup + install
  node install.js --uninstall  Remove hooks, MCP, skills
  node install.js --status     Show what's installed and running
  node install.js --start      Start services (Docker + FastAPI)
  node install.js --stop       Stop services
  node install.js --migrate    Run pending database migrations
  node install.js --migrate --dry-run  Show what migrations would run (no changes)
  node install.js --migrate --backup   Backup tables before migrating
  node install.js --backup     Backup mem_* tables (no migration)
  node install.js --help       Show this help
`);
} else {
  install();
}
