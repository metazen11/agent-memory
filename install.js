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

  const errors = [];

  // Docker
  try {
    const ver = run('docker --version', { ignoreError: true });
    if (ver) {
      ok(`Docker: ${ver.replace('Docker version ', '').split(',')[0]}`);
    } else {
      throw new Error();
    }
  } catch {
    fail('Docker not found');
    if (PLATFORM === 'darwin') info('Install: brew install --cask docker');
    else if (IS_WSL) info('Install Docker Desktop for Windows with WSL2 backend');
    else info('Install: sudo apt install docker.io docker-compose-plugin');
    errors.push('docker');
  }

  // Docker daemon
  try {
    run('docker info', { ignoreError: false, timeout: 5000 });
    ok('Docker daemon running');
  } catch {
    fail('Docker daemon not running');
    if (PLATFORM === 'darwin') info('Start Docker Desktop from Applications');
    errors.push('docker-daemon');
  }

  // Python
  let pythonCmd = '';
  for (const cmd of ['python3.12', 'python3', 'python']) {
    try {
      const ver = run(`${cmd} --version`, { ignoreError: true });
      if (ver) {
        const match = ver.match(/(\d+)\.(\d+)/);
        if (match && parseInt(match[1]) >= 3 && parseInt(match[2]) >= 12) {
          pythonCmd = cmd;
          ok(`Python: ${ver.replace('Python ', '')}`);
          break;
        }
      }
    } catch {}
  }
  if (!pythonCmd) {
    fail('Python 3.12+ not found');
    if (PLATFORM === 'darwin') info('Install: brew install python@3.12');
    else info('Install: sudo apt install python3.12 python3.12-venv');
    errors.push('python');
  }

  // Node.js (we're already running, but check version)
  const nodeVer = process.version;
  ok(`Node.js: ${nodeVer}`);

  // Claude Code
  const claudeDir = path.join(HOME, '.claude');
  if (fs.existsSync(claudeDir)) {
    ok('Claude Code detected');
  } else {
    skip('Claude Code not detected (hooks will not be installed)');
  }

  if (errors.includes('docker') || errors.includes('python')) {
    console.log('');
    fail('Missing critical prerequisites. Install them and retry.');
    process.exit(1);
  }

  return { pythonCmd };
}

// ── Install steps ─────────────────────────────────────────────

function createVenv(pythonCmd) {
  head('Python environment');

  if (fs.existsSync(PYTHON)) {
    const ver = run(`${PYTHON} --version`, { ignoreError: true });
    ok(`venv exists: ${ver || 'ready'}`);
    return;
  }

  info('Creating virtual environment...');
  run(`${pythonCmd} -m venv "${VENV_DIR}"`);
  ok('Created .venv/');
}

function installDeps() {
  head('Python dependencies');

  info('Installing from requirements.txt...');
  try {
    run(`"${PIP}" install -q -r "${path.join(INSTALL_DIR, 'requirements.txt')}"`, { timeout: 300000 });
    ok('Dependencies installed');
  } catch (e) {
    fail('pip install failed');
    info(e.message.split('\n').slice(-3).join('\n'));
    process.exit(1);
  }
}

function downloadEmbeddingModel() {
  head('Embedding model');

  info('Downloading nomic-ai/nomic-embed-text-v1.5 (~400MB)...');
  info('(This only happens once — model is cached locally)');
  try {
    run(
      `"${PYTHON}" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True); print(f'Loaded: {m.get_sentence_embedding_dimension()}d')"`,
      { timeout: 600000 }
    );
    ok('Embedding model cached');
  } catch (e) {
    fail('Embedding model download failed');
    info('Will download automatically on first use');
  }
}

function downloadGGUFModel() {
  head('Observation LLM (GGUF)');

  // Check if already configured in .env
  if (fs.existsSync(ENV_FILE)) {
    const envContent = fs.readFileSync(ENV_FILE, 'utf8');
    const match = envContent.match(/^OBSERVATION_LLM_MODEL=(.+)$/m);
    if (match && match[1].trim() && fs.existsSync(match[1].trim())) {
      ok(`GGUF model already configured: ${path.basename(match[1].trim())}`);
      return;
    }
  }

  info('Downloading Qwen2.5-1.5B-Instruct (~1GB)...');
  info('(This only happens once — model is cached locally)');
  try {
    const modelPath = run(
      `"${PYTHON}" -c "from huggingface_hub import hf_hub_download; print(hf_hub_download('Qwen/Qwen2.5-1.5B-Instruct-GGUF', 'qwen2.5-1.5b-instruct-q4_k_m.gguf'))"`,
      { timeout: 600000 }
    );
    if (modelPath && fs.existsSync(modelPath)) {
      ok(`GGUF model cached: ${path.basename(modelPath)}`);
      // Write path into .env
      updateEnvVar('OBSERVATION_LLM_MODEL', modelPath);
      ok('Set OBSERVATION_LLM_MODEL in .env');
    }
  } catch (e) {
    skip('GGUF download failed — will use Anthropic Haiku fallback');
    info('Set ANTHROPIC_API_KEY in .env for observation extraction');
  }
}

function generateEnv() {
  head('Environment configuration');

  if (fs.existsSync(ENV_FILE)) {
    ok('.env already exists');
    return;
  }

  // Read template
  let content = fs.readFileSync(ENV_EXAMPLE, 'utf8');

  // Generate random password
  const password = generatePassword();
  content = content.replace(
    /^POSTGRES_PASSWORD=.*$/m,
    `POSTGRES_PASSWORD=${password}`
  );

  fs.writeFileSync(ENV_FILE, content, 'utf8');
  ok('Generated .env with random Postgres password');
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
  head('Docker (PostgreSQL + pgvector)');

  if (isContainerRunning()) {
    ok('Container agent-memory-db already running');
    return;
  }

  info('Starting container...');
  try {
    run(`docker compose -f "${COMPOSE_FILE}" up -d`, { timeout: 60000 });
  } catch (e) {
    fail('docker compose up failed');
    info(e.message.split('\n').slice(-3).join('\n'));
    process.exit(1);
  }

  // Wait for healthcheck
  info('Waiting for PostgreSQL...');
  for (let i = 0; i < 30; i++) {
    try {
      const result = run(
        'docker exec agent-memory-db pg_isready -U ${POSTGRES_USER:-agentmem} -d ${POSTGRES_DB:-agent_memory}',
        { ignoreError: true }
      );
      if (result.includes('accepting connections')) {
        ok('PostgreSQL ready');
        return;
      }
    } catch {}
    run('sleep 1');
  }
  fail('PostgreSQL did not become ready in 30s');
  process.exit(1);
}

function startServer() {
  head('FastAPI server');

  if (isServerRunning()) {
    ok('Server already running on port 3377');
    return;
  }

  ensureDir(LOG_DIR);

  info('Starting uvicorn...');
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
  info('Waiting for server...');
  for (let i = 0; i < 15; i++) {
    if (isServerRunning()) {
      ok(`Server running (PID ${child.pid})`);
      return;
    }
    run('sleep 1');
  }
  fail('Server did not become ready in 15s');
  info(`Check logs: ${SERVER_LOG}`);
}

function registerMCP() {
  head('MCP server registration');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) {
      skip(`${name} not detected — skipping MCP`);
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
    ok(`Registered MCP server in ${name}`);
  }
}

function installHooks() {
  head('Hook installation');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect()) {
      skip(`${name} not detected — skipping hooks`);
      continue;
    }

    info(`${name}:`);
    ensureDir(agent.hooksDir);

    // Symlink hook files
    for (const file of HOOK_FILES) {
      const src  = path.join(INSTALL_DIR, 'hooks', file);
      const dest = path.join(agent.hooksDir, `agent-memory-${file}`);
      symlink(src, dest);
      ok(`  ${path.basename(dest)} → hooks/${file}`);
    }

    // Register in settings
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
        skip(`  ${event} hook already registered`);
        continue;
      }

      settings.hooks[event].push(entry);
      ok(`  Registered ${event} hook`);
      changed = true;
    }

    if (changed) writeJSON(agent.settingsFile, settings);
  }
}

function installSkills() {
  head('Skills');

  for (const [name, agent] of Object.entries(AGENTS)) {
    if (!agent.detect() || !agent.skillsDir) continue;

    for (const skill of SKILL_FILES) {
      const src  = path.join(INSTALL_DIR, skill.src);
      const dest = path.join(agent.skillsDir, skill.dest);
      ensureDir(path.dirname(dest));
      symlink(src, dest);
      ok(`${skill.dest} → ${name}`);
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
  console.log('\x1b[1magent-memory — installing\x1b[0m');
  console.log('─'.repeat(50));

  const { pythonCmd } = checkPrereqs();
  createVenv(pythonCmd);
  installDeps();
  generateEnv();
  downloadEmbeddingModel();
  downloadGGUFModel();
  startDocker();
  startServer();
  registerMCP();
  installHooks();
  installSkills();

  console.log('');
  console.log('─'.repeat(50));
  console.log('\x1b[1m  Installation complete!\x1b[0m');
  console.log('');
  ok('Services running on http://localhost:3377');
  ok('Restart your agent to activate hooks');
  console.log('');
  info('Commands:');
  info('  node install.js --status     Show status');
  info('  node install.js --start      Start services');
  info('  node install.js --stop       Stop services');
  info('  node install.js --uninstall  Remove everything');
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
} else if (args.includes('--help') || args.includes('-h')) {
  console.log(`
agent-memory installer

Usage:
  node install.js              Full setup + install
  node install.js --uninstall  Remove hooks, MCP, skills
  node install.js --status     Show what's installed and running
  node install.js --start      Start services (Docker + FastAPI)
  node install.js --stop       Stop services
  node install.js --help       Show this help
`);
} else {
  install();
}
