#!/usr/bin/env node
/**
 * git-init-guard — PreToolUse Bash matcher
 *
 * Enforces the lesson "NEVER run git init in $HOME or any directory at
 * or above $HOME." A .git in $HOME makes every project below it look
 * untracked and breaks per-project git repos.
 *
 * This is the enforcement artifact for lesson id=52 (global). With this
 * hook in place, the lesson never needs to be injected into prompts —
 * the rule is mechanical, not advisory.
 *
 * Decision logic:
 *   - tool_name != "Bash"                    → allow
 *   - command does not match git init        → allow
 *   - resolved target dir is NOT $HOME or an ancestor → allow
 *   - resolved target dir IS $HOME or an ancestor → DENY with explanation
 *
 * "Resolved target dir" accounts for `git init <path>` arguments and
 * the cwd of the shell. We err on the side of allowing — only block
 * when we're confident the init target is at-or-above $HOME.
 *
 * Known limitations:
 *   - Compound commands like `cd ~ && git init` are NOT caught — we
 *     resolve against the hook's reported cwd, not the cwd at the point
 *     of `git init`. Catching this would require a real shell parser.
 *     The lesson text in the inject still warns about this pattern.
 *
 * Hook contract:
 *   stdin  = JSON { tool_name, tool_input: { command, ... }, cwd }
 *   stdout = JSON { hookSpecificOutput: { hookEventName, permissionDecision, permissionDecisionReason? } }
 *
 * Set GIT_INIT_GUARD_DEBUG=1 for verbose stderr logging.
 */

const fs = require('fs');
const path = require('path');
const os = require('os');

const DEBUG = process.env.GIT_INIT_GUARD_DEBUG === '1';

function debug(msg) {
  if (DEBUG) console.error(`[git-init-guard] ${msg}`);
}

const allow = () => JSON.stringify({
  hookSpecificOutput: {
    hookEventName: 'PreToolUse',
    permissionDecision: 'allow',
  },
});

const deny = (reason) => JSON.stringify({
  hookSpecificOutput: {
    hookEventName: 'PreToolUse',
    permissionDecision: 'deny',
    permissionDecisionReason: reason,
  },
});

function readStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch {
    return null;
  }
}

// Match `git init`, optionally with options and a path argument. Tolerates
// leading env-var assignments and `command`/`exec` wrappers. Does NOT match
// `git init-template` or other subcommands that start with init-.
const GIT_INIT_RE = /(?:^|[;&|\s(])(?:(?:[A-Z_][A-Za-z0-9_]*=\S+\s+)*(?:command|exec)?\s*)?git\s+init(?:\s|$)/;

function findInitTargetArg(command) {
  // Find the `git init` token, then parse the rest of that segment for the
  // first non-flag positional. Conservative — if we can't parse, return null
  // and fall back to cwd.
  const idx = command.search(/\bgit\s+init\b/);
  if (idx < 0) return null;
  const tail = command.slice(idx + 'git init'.length);
  // Stop at command separators
  const segment = tail.split(/[;&|]/)[0];
  const tokens = segment.trim().split(/\s+/);
  for (const tok of tokens) {
    if (!tok) continue;
    if (tok.startsWith('-')) continue;
    // Strip surrounding quotes
    return tok.replace(/^["']|["']$/g, '');
  }
  return null;
}

function resolveTarget(targetArg, cwd) {
  const base = cwd || process.cwd();
  if (!targetArg) return base;
  if (path.isAbsolute(targetArg)) return path.resolve(targetArg);
  // Expand `~` and `~/...`
  if (targetArg === '~') return os.homedir();
  if (targetArg.startsWith('~/')) return path.join(os.homedir(), targetArg.slice(2));
  return path.resolve(base, targetArg);
}

function isAtOrAboveHome(absPath) {
  const home = os.homedir();
  if (!absPath || !home) return false;
  const norm = path.resolve(absPath);
  // Exact match: $HOME itself
  if (norm === home) return true;
  // Filesystem root is always an ancestor.
  if (norm === path.sep) return true;
  // Ancestor of $HOME (e.g., /Users, /Users/mz/..)
  // i.e., $HOME starts with norm + path separator
  if (home.startsWith(norm + path.sep)) return true;
  return false;
}

// ── Main ────────────────────────────────────────────────────

const input = readStdin();
if (!input) {
  console.log(allow());
  process.exit(0);
}

if (input.tool_name !== 'Bash') {
  console.log(allow());
  process.exit(0);
}

const command = (input.tool_input && input.tool_input.command) || '';
if (!command || !GIT_INIT_RE.test(command)) {
  console.log(allow());
  process.exit(0);
}

const targetArg = findInitTargetArg(command);
const resolved = resolveTarget(targetArg, input.cwd);
debug(`command=${command.slice(0, 120)}`);
debug(`targetArg=${targetArg} resolved=${resolved} home=${os.homedir()}`);

if (isAtOrAboveHome(resolved)) {
  const reason = [
    `BLOCKED: \`git init\` would create a repo at or above $HOME.`,
    ``,
    `  Target: ${resolved}`,
    `  $HOME:  ${os.homedir()}`,
    ``,
    `A .git in $HOME or its parents makes every folder below it look`,
    `untracked and breaks per-project git repos. Run \`git init\` only`,
    `inside the specific project directory you want to make a repo.`,
    ``,
    `If you intended to init a subdirectory, pass an explicit absolute`,
    `path: \`git init /path/to/project\`.`,
  ].join('\n');
  console.log(deny(reason));
  process.exit(0);
}

console.log(allow());
process.exit(0);
