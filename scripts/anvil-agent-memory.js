#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const ENV_FILE = path.join(ROOT, '.env');
const ANVIL_ENV_FILE = path.join(ROOT, '.anvil', 'agent-memory.env');

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

function usage() {
  console.log('Usage:');
  console.log('  node scripts/anvil-agent-memory.js <anvil-command> [args...]');
  console.log('Example:');
  console.log('  node scripts/anvil-agent-memory.js anvil');
}

function main() {
  const argv = process.argv.slice(2);
  if (argv.length < 1) {
    usage();
    process.exit(2);
  }

  const cmd = argv[0];
  const args = argv.slice(1);

  const mergedEnv = {
    ...process.env,
    ...parseEnvFile(ENV_FILE),
    ...parseEnvFile(ANVIL_ENV_FILE),
  };

  const child = spawn(cmd, args, {
    cwd: process.cwd(),
    env: mergedEnv,
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });

  child.on('error', (err) => {
    console.error(err.message || String(err));
    process.exit(1);
  });

  child.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    process.exit(code == null ? 1 : code);
  });
}

main();
