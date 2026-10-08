#!/usr/bin/env node
/**
 * launchd-api-label.js — which launchd job (if any) supervises the API on :3377.
 *
 * Two labels exist in the wild: `com.metazen.agent-memory-api` (written by
 * scripts/install_recovery_service.py) and `com.metazen.agent-memory-server`
 * (hand-installed, runs agentMemory-live). ensure-services.js must defer to
 * whichever is installed instead of spawning an unmanaged uvicorn that steals
 * the port and leaves the launchd job crash-looping on EADDRINUSE.
 *
 * CLI: `node launchd-api-label.js [launchAgentsDir]` prints the label or nothing.
 */
const fs = require('fs');
const path = require('path');

const API_LABELS = ['com.metazen.agent-memory-api', 'com.metazen.agent-memory-server'];

function findManagedApiLabel(launchAgentsDir) {
  const dir = launchAgentsDir || path.join(require('os').homedir(), 'Library/LaunchAgents');
  return API_LABELS.find(l => fs.existsSync(path.join(dir, `${l}.plist`))) || '';
}

module.exports = { findManagedApiLabel, API_LABELS };

if (require.main === module) {
  process.stdout.write(findManagedApiLabel(process.argv[2]));
}
