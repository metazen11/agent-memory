#!/usr/bin/env node
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);

const packs = [
  path.join(ROOT, 'scripts', 'install-agent-memory-claude.js'),
  path.join(ROOT, 'scripts', 'install-agent-memory-codex.js'),
  path.join(ROOT, 'scripts', 'install-agent-memory-anvil.js'),
];

for (const pack of packs) {
  const res = spawnSync(process.execPath, [pack, ...args], {
    cwd: ROOT,
    stdio: 'inherit',
    env: process.env,
  });
  if ((res.status == null ? 1 : res.status) !== 0) process.exit(res.status == null ? 1 : res.status);
}
