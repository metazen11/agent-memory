#!/usr/bin/env node
// Native Codex/Work prompt capture. Hook output is always contract-safe JSON.
const fs = require('fs');
const { requestJson, hintsEnabled, readJsonFile, LESSONS_FILE, formatLessons } = require('./common');

async function main() {
  const event = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (!event.session_id || typeof event.prompt !== 'string') return {};
  const project = event.cwd || process.cwd();
  // Await persistence so the following tool call can link to this prompt.
  try {
    await requestJson('POST', '/api/prompts', {
      session_id: event.session_id, prompt: event.prompt, cwd: project, agent_name: 'codex-cli',
    }, 1200);
  } catch (error) {
    console.error(`[agent-memory:prompt] ${error.message}`);
  }
  if (!hintsEnabled()) return {};
  let lessons;
  try {
    const { data } = await requestJson('GET', `/api/lessons?project=${encodeURIComponent(project)}&active=true&limit=100`, null, 1200);
    lessons = Array.isArray(data) ? data : [];
  } catch {
    const snapshot = readJsonFile(LESSONS_FILE, {});
    lessons = snapshot.project_path === project ? snapshot.lessons || [] : [];
  }
  const critical = lessons.filter(l => l.active !== false && l.severity === 'critical').slice(0, 5);
  return critical.length ? {
    hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: formatLessons(critical).slice(0, 6000) },
  } : {};
}
main().then(out => console.log(JSON.stringify(out))).catch(error => {
  console.error(`[agent-memory:prompt] ${error.message}`);
  console.log('{}');
});
