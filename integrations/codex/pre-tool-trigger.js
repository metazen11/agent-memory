#!/usr/bin/env node
const { requestJson, compileLessonMatchesFromSnapshot, preToolHintsEnabled } = require('./common');
const http = require('http');

function parseArgs(argv) {
  const args = { tool: '', input: '', project: process.cwd() };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--tool') args.tool = argv[++i] || '';
    else if (a === '--input') args.input = argv[++i] || '';
    else if (a === '--project') args.project = argv[++i] || args.project;
  }
  return args;
}

function trackTrigger(id) {
  const base = process.env.AGENT_MEMORY_SERVER || 'http://localhost:3377';
  const url = new URL(`/api/lessons/${id}/trigger`, base);
  const req = http.request({
    hostname: url.hostname,
    port: url.port,
    path: url.pathname,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': 2 },
    timeout: 1000,
  }, () => {});
  req.on('error', () => {});
  req.on('timeout', () => req.destroy());
  req.write('{}');
  req.end();
}

async function main() {
  const { tool, input, project } = parseArgs(process.argv);
  if (!tool) {
    console.error('Usage: node integrations/codex/pre-tool-trigger.js --tool <ToolName> --input <preview>');
    process.exit(2);
  }
  if (!preToolHintsEnabled()) {
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
    console.log(source === 'snapshot' ? 'No matching lessons (snapshot mode).' : 'No matching lessons.');
    return;
  }

  for (const lesson of matches) {
    trackTrigger(lesson.id);
  }

  console.log(source === 'snapshot' ? 'Active lessons (snapshot mode):' : 'Active lessons:');
  for (const lesson of matches) {
    console.log(`- [${lesson.severity}] ${lesson.rule}`);
  }
}

main().catch((e) => {
  console.error(`agent-memory pre-tool trigger failed: ${e.message || String(e)}`);
  process.exit(1);
});
