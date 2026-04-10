#!/usr/bin/env node
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const res = spawnSync(process.execPath, [path.join(ROOT, 'install-codex.js'), ...args], {
  cwd: ROOT,
  stdio: 'inherit',
  env: process.env,
});
process.exit(res.status == null ? 1 : res.status);
