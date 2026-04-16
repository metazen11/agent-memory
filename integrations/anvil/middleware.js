#!/usr/bin/env node
const fs = require('fs');
const path = require('path');

function parseEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);
  for (const line of lines) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const idx = t.indexOf('=');
    if (idx <= 0) continue;
    const key = t.slice(0, idx).trim();
    let value = t.slice(idx + 1).trim();
    value = value.replace(/^['"]|['"]$/g, '');
    out[key] = value;
  }
  return out;
}

function envFlagEnabled(rawValue, defaultValue = true) {
  if (rawValue == null || rawValue === '') return defaultValue;
  const normalized = String(rawValue).trim().toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'off'].includes(normalized)) return false;
  return defaultValue;
}

function resolveHintFlags({ processEnv = process.env, envFile, anvilEnvFile }) {
  const merged = {
    ...processEnv,
    ...parseEnvFile(envFile),
    ...parseEnvFile(anvilEnvFile),
  };
  const global = envFlagEnabled(merged.AGENT_MEMORY_HINTS_ENABLED, true);
  const session = envFlagEnabled(merged.AGENT_MEMORY_SESSION_HINTS_ENABLED, global);
  const pretool = envFlagEnabled(merged.AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED, global);
  return {
    merged,
    global,
    session,
    pretool,
    raw: {
      global: merged.AGENT_MEMORY_HINTS_ENABLED || '1',
      session: merged.AGENT_MEMORY_SESSION_HINTS_ENABLED || '(inherits global)',
      pretool: merged.AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED || '(inherits global)',
    },
  };
}

function resolveDebugFlags({ processEnv = process.env, envFile, anvilEnvFile }) {
  const merged = {
    ...processEnv,
    ...parseEnvFile(envFile),
    ...parseEnvFile(anvilEnvFile),
  };
  const debug = envFlagEnabled(merged.AGENT_MEMORY_DEBUG, false);
  const transparency = envFlagEnabled(merged.AGENT_MEMORY_DEBUG_TRANSPARENCY, debug);
  return { debug, transparency, merged };
}

function ensureAgentMemoryInstalled(rootDir) {
  const required = [
    path.join(rootDir, 'mcp_server.py'),
    path.join(rootDir, 'scripts', 'hints-config.js'),
  ];
  const missing = required.filter((f) => !fs.existsSync(f)).map((f) => path.relative(rootDir, f));
  if (missing.length) {
    const msg = [
      'agent-memory is required for /tool-hints but is not installed in this workspace.',
      `Missing: ${missing.join(', ')}`,
    ].join('\n');
    const err = new Error(msg);
    err.code = 'AGENT_MEMORY_NOT_INSTALLED';
    throw err;
  }
}

function loadEnvLines(file) {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').split(/\r?\n/);
}

function setEnvValue(lines, key, value) {
  let updated = false;
  const next = lines.map((line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith(`${key}=`)) return line;
    updated = true;
    return `${key}=${value}`;
  });
  if (!updated) next.push(`${key}=${value}`);
  return next;
}

function writeEnvLines(file, lines) {
  const text = `${lines.join('\n').replace(/\n*$/, '')}\n`;
  fs.writeFileSync(file, text, 'utf8');
}

function setGlobalHintsEnabled({ value, anvilEnvFile }) {
  fs.mkdirSync(path.dirname(anvilEnvFile), { recursive: true });
  let lines = loadEnvLines(anvilEnvFile);
  lines = setEnvValue(lines, 'AGENT_MEMORY_HINTS_ENABLED', value ? '1' : '0');
  writeEnvLines(anvilEnvFile, lines);
}

function setDebugEnabled({ value, anvilEnvFile }) {
  fs.mkdirSync(path.dirname(anvilEnvFile), { recursive: true });
  let lines = loadEnvLines(anvilEnvFile);
  lines = setEnvValue(lines, 'AGENT_MEMORY_DEBUG', value ? '1' : '0');
  lines = setEnvValue(lines, 'AGENT_MEMORY_DEBUG_TRANSPARENCY', value ? '1' : '0');
  writeEnvLines(anvilEnvFile, lines);
}

function printToolHintStatus({ envFile, anvilEnvFile }) {
  const flags = resolveHintFlags({ envFile, anvilEnvFile });
  const debug = resolveDebugFlags({ envFile, anvilEnvFile });
  console.log('tool-hints status');
  console.log(`- global : ${flags.raw.global} (${flags.global ? 'on' : 'off'})`);
  console.log(`- session: ${flags.raw.session} (${flags.session ? 'on' : 'off'})`);
  console.log(`- pretool: ${flags.raw.pretool} (${flags.pretool ? 'on' : 'off'})`);
  console.log(`- debug  : ${debug.debug ? 'on' : 'off'}`);
  console.log(`- trace  : ${debug.transparency ? 'on' : 'off'}`);
  console.log(`- source : ${anvilEnvFile}`);
}

function logDebugEvent({ event, payload, envFile, anvilEnvFile, processEnv = process.env }) {
  const flags = resolveDebugFlags({ processEnv, envFile, anvilEnvFile });
  if (!flags.debug) return;
  const body = {
    source: 'agent-memory/anvil-middleware',
    event,
    ts: new Date().toISOString(),
    payload: payload || {},
  };
  process.stderr.write(`[agent-memory:debug] ${JSON.stringify(body)}\n`);
}

function logTransparencyMessage({
  direction,
  role,
  text,
  meta,
  envFile,
  anvilEnvFile,
  processEnv = process.env,
}) {
  const flags = resolveDebugFlags({ processEnv, envFile, anvilEnvFile });
  if (!flags.transparency) return;
  const body = {
    source: 'agent-memory/anvil-middleware',
    event: 'message_trace',
    ts: new Date().toISOString(),
    direction: direction || 'internal',
    role: role || 'unknown',
    text: String(text || ''),
    meta: meta || {},
  };
  process.stderr.write(`[agent-memory:trace] ${JSON.stringify(body)}\n`);
}

function appendSystemInjection({
  systemPrompt,
  injection,
  reason,
  envFile,
  anvilEnvFile,
  processEnv = process.env,
}) {
  const merged = `${String(systemPrompt || '')}\n\n${String(injection || '')}`.trim();
  logDebugEvent({
    event: 'system_injection',
    payload: {
      reason: reason || 'agent-memory-hints',
      injection: String(injection || ''),
    },
    envFile,
    anvilEnvFile,
    processEnv,
  });
  return merged;
}

function maybeHandleToolHintsSlashCommand({
  argv,
  rootDir,
  envFile,
  anvilEnvFile,
}) {
  if (!Array.isArray(argv) || argv.length < 1) return false;
  const slash = argv[0];
  if (slash !== '/tool-hints' && slash !== 'tool-hints') return false;

  ensureAgentMemoryInstalled(rootDir);
  const sub = argv[1] || 'status';
  if (sub === 'status') {
    printToolHintStatus({ envFile, anvilEnvFile });
    return true;
  }

  if (sub === 'debug') {
    const mode = argv[2] || 'status';
    if (mode === 'status') {
      printToolHintStatus({ envFile, anvilEnvFile });
      return true;
    }
    if (!['on', 'off', 'toggle'].includes(mode)) {
      console.error('Usage: /tool-hints debug <status|on|off|toggle>');
      process.exit(2);
    }
    const current = resolveDebugFlags({ envFile, anvilEnvFile }).debug;
    const next = mode === 'toggle' ? !current : mode === 'on';
    setDebugEnabled({ value: next, anvilEnvFile });
    printToolHintStatus({ envFile, anvilEnvFile });
    return true;
  }

  if (!['on', 'off', 'toggle'].includes(sub)) {
    console.error('Usage: /tool-hints <status|on|off|toggle>');
    process.exit(2);
  }

  const current = resolveHintFlags({ envFile, anvilEnvFile }).global;
  const next = sub === 'toggle' ? !current : sub === 'on';
  setGlobalHintsEnabled({ value: next, anvilEnvFile });
  printToolHintStatus({ envFile, anvilEnvFile });
  return true;
}

module.exports = {
  parseEnvFile,
  resolveHintFlags,
  resolveDebugFlags,
  ensureAgentMemoryInstalled,
  appendSystemInjection,
  logDebugEvent,
  logTransparencyMessage,
  maybeHandleToolHintsSlashCommand,
};
