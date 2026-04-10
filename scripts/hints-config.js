#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const ROOT = path.resolve(__dirname, '..');
const ENV_PATH = path.join(ROOT, '.env');
function loadEnvLines() {
  try {
    return fs.readFileSync(ENV_PATH, 'utf8').split('\n');
  } catch {
    return [];
  }
}

function parseEnvMap(lines) {
  const map = {};
  for (const line of lines) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const idx = t.indexOf('=');
    if (idx <= 0) continue;
    const key = t.slice(0, idx).trim();
    const value = t.slice(idx + 1).trim();
    map[key] = value;
  }
  return map;
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

function writeEnvLines(lines) {
  const text = `${lines.join('\n').replace(/\n*$/, '')}\n`;
  fs.writeFileSync(ENV_PATH, text, 'utf8');
}

function normBool(v) {
  const s = String(v || '').trim().toLowerCase();
  if (['1', 'true', 'on', 'yes'].includes(s)) return '1';
  if (['0', 'false', 'off', 'no'].includes(s)) return '0';
  return null;
}

function resolveCurrent() {
  const lines = loadEnvLines();
  const env = parseEnvMap(lines);
  return {
    global: env.AGENT_MEMORY_HINTS_ENABLED || '(default:1)',
    session: env.AGENT_MEMORY_SESSION_HINTS_ENABLED || '(inherits global)',
    pretool: env.AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED || '(inherits global)',
  };
}

function printStatus() {
  const s = resolveCurrent();
  console.log('Hint flag status (.env):');
  console.log(`- global : ${s.global}`);
  console.log(`- session: ${s.session}`);
  console.log(`- pretool: ${s.pretool}`);
}

function updateFlag(which, onOff) {
  const value = normBool(onOff);
  if (value == null) {
    console.error('Value must be on/off, 1/0, true/false.');
    process.exit(2);
  }
  const key = which === 'global'
    ? 'AGENT_MEMORY_HINTS_ENABLED'
    : which === 'session'
      ? 'AGENT_MEMORY_SESSION_HINTS_ENABLED'
      : which === 'pretool'
        ? 'AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED'
        : null;
  if (!key) {
    console.error('Target must be one of: global, session, pretool');
    process.exit(2);
  }

  let lines = loadEnvLines();
  lines = setEnvValue(lines, key, value);
  writeEnvLines(lines);
  console.log(`Updated ${key}=${value} in ${ENV_PATH}`);
  printStatus();
}

async function runTui() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = (q) => new Promise((resolve) => rl.question(q, resolve));

  printStatus();
  console.log('');
  console.log('Choose a target:');
  console.log('1) global');
  console.log('2) session');
  console.log('3) pretool');
  const targetChoice = (await ask('Select [1-3]: ')).trim();
  const target = targetChoice === '1' ? 'global' : targetChoice === '2' ? 'session' : targetChoice === '3' ? 'pretool' : '';
  if (!target) {
    rl.close();
    console.error('Invalid target choice.');
    process.exit(2);
  }

  console.log('Set mode:');
  console.log('1) on');
  console.log('2) off');
  const modeChoice = (await ask('Select [1-2]: ')).trim();
  const mode = modeChoice === '1' ? 'on' : modeChoice === '2' ? 'off' : '';
  rl.close();
  if (!mode) {
    console.error('Invalid mode choice.');
    process.exit(2);
  }

  updateFlag(target, mode);
}

function usage() {
  console.log('Usage:');
  console.log('  node scripts/hints-config.js status');
  console.log('  node scripts/hints-config.js set <global|session|pretool> <on|off>');
  console.log('  node scripts/hints-config.js tui');
}

async function main() {
  const cmd = process.argv[2] || 'status';
  if (cmd === 'status') {
    printStatus();
    return;
  }
  if (cmd === 'set') {
    updateFlag(process.argv[3], process.argv[4]);
    return;
  }
  if (cmd === 'tui') {
    await runTui();
    return;
  }
  usage();
  process.exit(2);
}

main().catch((err) => {
  console.error(err.message || String(err));
  process.exit(1);
});
