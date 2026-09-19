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
  req.on('error', () => {});
  req.on('timeout', () => req.destroy());
  req.write('{}');
  req.end();
}

async function main() {
  const { tool, input, project, hookMode } = parseArgs(process.argv);
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
    const { data } = await requestJson(
      'GET',
      `/api/lessons/match?tool_name=${encodeURIComponent(tool)}&tool_input_preview=${encodeURIComponent(input || '')}&project=${encodeURIComponent(project)}`,
      null,
      2000
    );
    matches = Array.isArray(data) ? data : [];
  } catch {
    matches = compileLessonMatchesFromSnapshot({
      toolName: tool,
      toolInputPreview: input || '',
      projectPath: project,
    });
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

  for (const lesson of matches) {
    trackTrigger(lesson.id);
  }

  const lines = matches.map((lesson) => `- [${lesson.severity}] ${lesson.rule}`);
  if (hookMode) {
    console.log(JSON.stringify({ systemMessage: `## Active Lessons\n${lines.join('\n')}` }));
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
