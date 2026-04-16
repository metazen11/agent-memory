#!/usr/bin/env node
const path = require('path');
const { spawn } = require('child_process');
const {
  parseEnvFile,
  maybeHandleToolHintsSlashCommand,
} = require('../integrations/anvil/middleware');

const ROOT = path.resolve(__dirname, '..');
const ENV_FILE = path.join(ROOT, '.env');
const ANVIL_ENV_FILE = path.join(ROOT, '.anvil', 'agent-memory.env');

function usage() {
  console.log('Usage:');
  console.log('  node scripts/anvil-agent-memory.js <anvil-command> [args...]');
  console.log('  node scripts/anvil-agent-memory.js /tool-hints <status|on|off|toggle>');
  console.log('  node scripts/anvil-agent-memory.js /tool-hints debug <status|on|off|toggle>');
  console.log('Example:');
  console.log('  node scripts/anvil-agent-memory.js anvil');
  console.log('  node scripts/anvil-agent-memory.js /tool-hints toggle');
}

function main() {
  const argv = process.argv.slice(2);
  if (maybeHandleToolHintsSlashCommand({
    argv,
    rootDir: ROOT,
    envFile: ENV_FILE,
    anvilEnvFile: ANVIL_ENV_FILE,
  })) {
    return;
  }
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
