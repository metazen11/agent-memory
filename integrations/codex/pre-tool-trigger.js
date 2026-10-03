#!/usr/bin/env node
const { requestJson, compileLessonMatchesFromSnapshot, preToolHintsEnabled } = require('./common');
const http = require('http');
const fs = require('fs');

function readStdinEvent() {
  if (process.stdin.isTTY) return null;
  try {
    const raw = fs.readFileSync(0, 'utf8').trim();
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function stringifyPreview(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value).slice(0, 1000); } catch { return String(value).slice(0, 1000); }
}

function parseArgs(argv) {
  const event = readStdinEvent();
  const args = {
    tool: event?.tool_name || '',
    input: stringifyPreview(event?.tool_input),
    toolInput: event?.tool_input || {},
    project: event?.cwd || process.cwd(),
    hookMode: !!event,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--tool') args.tool = argv[++i] || '';
    else if (a === '--input') args.input = argv[++i] || '';
    else if (a === '--project') args.project = argv[++i] || args.project;
  }
  return args;
}

function extractModifiedFiles(tool, toolInput, preview) {
  const files = [toolInput.file_path, toolInput.path, toolInput.notebook_path];
  for (const edit of toolInput.edits || []) files.push(edit?.file_path);
  if (tool === 'apply_patch') {
    const patch = typeof toolInput === 'string' ? toolInput : toolInput.command || toolInput.patch || preview;
    for (const match of patch.matchAll(/^\*\*\* (?:Update File|Add File|Delete File|Move to): (.+)$/gm)) files.push(match[1]);
  }
  if (tool === 'Bash') {
    const command = toolInput.command || toolInput.cmd || preview;
    files.push(...command.split(/[\s;|&<>()'"`]+/).filter(v => /\//.test(v) || /^[\w.~-]+\.[A-Za-z0-9]{1,8}$/.test(v)));
  }
  return [...new Set(files.filter(v => typeof v === 'string' && v))].slice(0, 40);
}

function trackTrigger(id) {
  const base = process.env.AGENT_MEMORY_SERVER || 'http://127.0.0.1:3377';
  const url = new URL(`/api/lessons/${id}/trigger`, base);
  const req = http.request({
    hostname: url.hostname,
    port: url.port,
    path: url.pathname,
    method: 'POST',
    headers: { 'X-Agent-Name': 'codex', 'Content-Type': 'application/json', 'Content-Length': 2 },
    timeout: 1000,
  }, () => {});
  req.on('socket', socket => socket.unref());
  req.on('error', () => {});
  req.on('timeout', () => req.destroy());
  req.write('{}');
  req.end();
}

async function main() {
  const { tool, input, toolInput, project, hookMode } = parseArgs(process.argv);
  const modifiedFiles = extractModifiedFiles(tool, toolInput, input);
  const lessonTools = tool === 'apply_patch' ? ['apply_patch', 'Edit', 'Write'] : [tool];
  if (!tool) {
    if (hookMode) {
      console.log(JSON.stringify({}));
      return;
    }
    console.error('Usage: node integrations/codex/pre-tool-trigger.js --tool <ToolName> --input <preview>');
    process.exit(2);
  }
  if (!preToolHintsEnabled()) {
    if (hookMode) {
      console.log(JSON.stringify({}));
      return;
    }
    console.log('Pre-tool lesson hints disabled (AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED=0).');
    return;
  }

  let matches = [];
  let source = 'api';
  try {
    const params = new URLSearchParams({ tool_name: tool, tool_input_preview: input || '', project, strict_scope: 'true' });
    const queries = ['input', ...(modifiedFiles.length ? ['file_scope'] : [])];
    const responses = await Promise.all(lessonTools.flatMap(toolName => queries.map(triggerOn => {
      const query = new URLSearchParams(params);
      query.set('tool_name', toolName);
      query.set('trigger_on', triggerOn);
      if (triggerOn === 'file_scope') query.set('modified_files', modifiedFiles.join(','));
      return requestJson('GET', `/api/lessons/match?${query}`, null, 1200);
    })));
    matches = [...new Map(responses.flatMap(r => Array.isArray(r.data) ? r.data : []).map(l => [l.id, l])).values()];
  } catch {
    matches = [...new Map(lessonTools.flatMap(toolName => compileLessonMatchesFromSnapshot({
      toolName,
      modifiedFiles,
      toolInputPreview: input || '',
      projectPath: project,
    })).map(lesson => [lesson.id, lesson])).values()];
    source = 'snapshot';
  }
  if (!matches.length) {
    if (hookMode) {
      console.log(JSON.stringify({}));
      return;
    }
    console.log(source === 'snapshot' ? 'No matching lessons (snapshot mode).' : 'No matching lessons.');
    return;
  }

  matches.sort((a, b) => (a.severity === 'critical' ? 0 : 1) - (b.severity === 'critical' ? 0 : 1));
  matches = matches.slice(0, 5);
  for (const lesson of matches) {
    trackTrigger(lesson.id);
  }

  const lines = matches.map((lesson) => `- [${lesson.severity}] ${lesson.rule}`);
  if (hookMode) {
    const hint = `Agent Memory hints — ${project}\n${lines.join('\n')}`.slice(0, 6000);
    console.log(JSON.stringify({ systemMessage: hint, hookSpecificOutput: { hookEventName: 'PreToolUse', additionalContext: hint } }));
  } else {
    console.log(source === 'snapshot' ? 'Active lessons (snapshot mode):' : 'Active lessons:');
    for (const line of lines) console.log(line);
  }
}

main().catch((e) => {
  if (!process.stdin.isTTY) {
    console.log(JSON.stringify({}));
    process.exit(0);
  }
  console.error(`agent-memory pre-tool trigger failed: ${e.message || String(e)}`);
  process.exit(1);
});
