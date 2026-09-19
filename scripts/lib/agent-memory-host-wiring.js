const fs = require('fs');
const path = require('path');

function readJson(file, fallback = {}) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function writeJson(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
}

function symlink(src, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  try { fs.unlinkSync(dest); } catch {}
  fs.symlinkSync(src, dest);
}

function removeSymlink(dest) {
  try {
    const stat = fs.lstatSync(dest);
    if (!stat.isSymbolicLink()) return false;
    fs.unlinkSync(dest);
    return true;
  } catch {
    return false;
  }
}

function hookEntry(command, timeout, matcher = null) {
  const entry = {
    hooks: [{ type: 'command', command, timeout }],
  };
  if (matcher) entry.matcher = matcher;
  return entry;
}

function createHostWiring({ root, home }) {
  const claudeHome = path.join(home, '.claude');
  const codexHome = path.join(home, '.codex');
  const codexHooksDir = path.join(codexHome, 'hooks');

  const claude = {
    name: 'claude',
    detect: () => fs.existsSync(claudeHome),
    hooksDir: path.join(claudeHome, 'hooks'),
    settingsFile: path.join(claudeHome, 'settings.json'),
    mcpFile: path.join(home, '.claude.json'),
    skillsDir: path.join(claudeHome, 'skills'),
    hookFiles: [
      ['hooks/user-prompt-submit.js', 'agent-memory-user-prompt-submit.js'],
      ['hooks/pre-tool-use.js', 'agent-memory-pre-tool-use.js'],
      ['hooks/post-tool-use.js', 'agent-memory-post-tool-use.js'],
      ['hooks/session-start.js', 'agent-memory-session-start.js'],
      ['hooks/session-end.js', 'agent-memory-session-end.js'],
    ],
    hookEntries: [
      { event: 'UserPromptSubmit', entry: hookEntry('node ~/.claude/hooks/agent-memory-user-prompt-submit.js', 5) },
      { event: 'PreToolUse', entry: hookEntry('node ~/.claude/hooks/agent-memory-pre-tool-use.js', 2, 'Edit|Write|Bash|NotebookEdit') },
      { event: 'PostToolUse', entry: hookEntry('node ~/.claude/hooks/agent-memory-post-tool-use.js', 5, 'Read|Edit|Write|Bash|Grep|Glob|NotebookEdit|WebFetch|WebSearch') },
      { event: 'SessionStart', entry: hookEntry('node ~/.claude/hooks/agent-memory-session-start.js', 60) },
      { event: 'Stop', entry: hookEntry('node ~/.claude/hooks/agent-memory-session-end.js', 10) },
    ],
  };

  const codex = {
    name: 'codex',
    detect: () => fs.existsSync(codexHome),
    hooksDir: codexHooksDir,
    settingsFile: path.join(codexHome, 'hooks.json'),
    configFile: path.join(codexHome, 'config.toml'),
    hookFiles: [
      ['integrations/codex/session-start.js', 'agent-memory-session-start.js'],
      ['integrations/codex/pre-tool-trigger.js', 'agent-memory-pre-tool-trigger.js'],
      ['integrations/codex/post-tool-hook.js', 'agent-memory-post-tool-use.js'],
      ['integrations/codex/session-end.js', 'agent-memory-session-end.js'],
    ],
    hookEntries: [
      { event: 'SessionStart', entry: hookEntry(`node '${path.join(codexHooksDir, 'agent-memory-session-start.js')}'`, 60) },
      { event: 'PreToolUse', entry: hookEntry(`node '${path.join(codexHooksDir, 'agent-memory-pre-tool-trigger.js')}'`, 2, 'Edit|Write|NotebookEdit|Bash') },
      { event: 'PostToolUse', entry: hookEntry(`node '${path.join(codexHooksDir, 'agent-memory-post-tool-use.js')}'`, 5, 'Read|Edit|Write|Bash|Grep|Glob|NotebookEdit|WebFetch|WebSearch') },
      { event: 'SessionEnd', entry: hookEntry(`node '${path.join(codexHooksDir, 'agent-memory-session-end.js')}'`, 10) },
    ],
  };

  return { claude, codex, root };
}

function installHookSymlinks(host, root) {
  for (const [srcRel, destName] of host.hookFiles) {
    symlink(path.join(root, srcRel), path.join(host.hooksDir, destName));
  }
}

function registerHookEntries(settingsFile, entries) {
  const settings = readJson(settingsFile, { hooks: {} });
  if (!settings.hooks) settings.hooks = {};
  let added = 0;
  for (const { event, entry } of entries) {
    if (!Array.isArray(settings.hooks[event])) settings.hooks[event] = [];
    const command = entry.hooks[0].command;
    const existing = settings.hooks[event].find((candidate) => (
      candidate.hooks && candidate.hooks.some((hook) => hook.command === command)
    ));
    if (existing) {
      if (entry.matcher && !existing.matcher) existing.matcher = entry.matcher;
      continue;
    }
    settings.hooks[event].push(entry);
    added += 1;
  }
  writeJson(settingsFile, settings);
  return added;
}

function unregisterHookEntries(settingsFile, entries) {
  const settings = readJson(settingsFile, {});
  let removed = 0;
  for (const { event, entry } of entries) {
    const arr = settings.hooks?.[event];
    if (!Array.isArray(arr)) continue;
    const command = entry.hooks[0].command;
    const before = arr.length;
    settings.hooks[event] = arr.filter(
      (candidate) => !(candidate.hooks && candidate.hooks.some((hook) => hook.command === command))
    );
    removed += before - settings.hooks[event].length;
    if (settings.hooks[event].length === 0) {
      delete settings.hooks[event];
      if (before === 0) removed += 1;
    }
  }
  if (settings.hooks) {
    for (const [event, entriesForEvent] of Object.entries(settings.hooks)) {
      if (Array.isArray(entriesForEvent) && entriesForEvent.length === 0) {
        delete settings.hooks[event];
        removed += 1;
      }
    }
  }
  if (removed > 0) writeJson(settingsFile, settings);
  return removed;
}

function repairCodexMcpPath({ configFile, python, server }) {
  if (!fs.existsSync(configFile)) return false;
  let text = fs.readFileSync(configFile, 'utf8');
  const before = text;
  text = text.replace(
    /command = "\/Users\/mz\/Dropbox\/_CODING\/agentMemory\/\.venv\/bin\/python"/g,
    `command = "${python}"`,
  );
  text = text.replace(
    /args = \["\/Users\/mz\/Dropbox\/_CODING\/agentMemory\/mcp_server\.py"\]/g,
    `args = ["${server}"]`,
  );
  if (text !== before) fs.writeFileSync(configFile, text, 'utf8');
  return text !== before;
}

module.exports = {
  createHostWiring,
  installHookSymlinks,
  readJson,
  registerHookEntries,
  removeSymlink,
  repairCodexMcpPath,
  symlink,
  unregisterHookEntries,
  writeJson,
};
