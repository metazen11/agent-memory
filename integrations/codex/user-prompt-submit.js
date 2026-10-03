#!/usr/bin/env node
// Native Codex/Work prompt capture. Hook output is always contract-safe JSON.
const fs = require('fs');
const { normalizeProjectPath, requestJson, hintsEnabled, readJsonFile, LESSONS_FILE, formatLessons, lessonAppliesToProject, saveSpooledPromptPayload } = require('./common');

async function main() {
  const event = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (!event.session_id || typeof event.prompt !== 'string') return {};
  const project = normalizeProjectPath(event.cwd || process.cwd());
  const payload = { session_id: event.session_id, prompt: event.prompt, cwd: project, agent_name: 'codex-cli' };
  // Await persistence so the following tool call can link to this prompt.
  try {
    await requestJson('POST', '/api/prompts', payload, 1200);
  } catch (error) {
    saveSpooledPromptPayload(payload);
    console.error(`[agent-memory:prompt] spooled: ${error.message}`);
  }
  if (!hintsEnabled()) return {};
  let lessons;
  try {
    const { data } = await requestJson('GET', `/api/lessons?project=${encodeURIComponent(project)}&active=true&limit=100&strict_scope=true`, null, 1200);
    lessons = Array.isArray(data) ? data : [];
  } catch {
    const snapshot = readJsonFile(LESSONS_FILE, {});
    lessons = normalizeProjectPath(snapshot.project_path) === project ? snapshot.lessons || [] : [];
  }
  const critical = lessons.filter(l => l.active !== false && l.severity === 'critical' && lessonAppliesToProject(l, project)).slice(0, 5);
  const hint = `Agent Memory hints — ${project}\n${formatLessons(critical)}`.slice(0, 6000);
  return critical.length ? {
    systemMessage: hint,
    hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: hint },
  } : {};
}
main().then(out => console.log(JSON.stringify(out))).catch(error => {
  console.error(`[agent-memory:prompt] ${error.message}`);
  console.log('{}');
});
