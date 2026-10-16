#!/usr/bin/env node
// Native Codex/Work prompt capture. Hook output is always contract-safe JSON.
const fs = require('fs');
const { randomUUID } = require('crypto');
const { normalizeProjectPath, requestJson, hintsEnabled, readJsonFile, LESSONS_FILE, formatLessons, lessonAppliesToProject, lessonIsGlobal, lessonContextSentForSession, markLessonContextSent, saveSpooledPromptPayload } = require('./common');

async function main() {
  const event = JSON.parse(fs.readFileSync(0, 'utf8'));
  if (!event.session_id || typeof event.prompt !== 'string') return {};
  const project = normalizeProjectPath(event.cwd || process.cwd());
  const payload = { ingest_id: randomUUID(), session_id: event.session_id, prompt: event.prompt, cwd: project, agent_name: 'codex-cli' };
  // Await persistence so the following tool call can link to this prompt.
  try {
    await requestJson('POST', '/api/prompts', payload, 1200);
  } catch (error) {
    saveSpooledPromptPayload(payload);
    console.error(`[agent-memory:prompt] spooled: ${error.message}`);
  }
  if (!hintsEnabled()) return {};
  if (lessonContextSentForSession(event.session_id)) return {};
  let lessons;
  try {
    // Fetch the bounded overview once per session.  The client applies the
    // exact project/global filter below; strict PreToolUse matching remains
    // the only path for tool-specific reminders.
    const { data } = await requestJson('GET', `/api/lessons?project=${encodeURIComponent(project)}&active=true&limit=100`, null, 1200);
    lessons = Array.isArray(data) ? data : [];
  } catch {
    const snapshot = readJsonFile(LESSONS_FILE, {});
    lessons = normalizeProjectPath(snapshot.project_path) === project ? snapshot.lessons || [] : [];
  }
  const scoped = lessons.filter(l => l.active !== false && (lessonIsGlobal(l) || lessonAppliesToProject(l, project)));
  const overview = scoped
    .sort((a, b) => (a.severity === 'critical' ? 0 : b.severity === 'critical' ? 1 : 0))
    .slice(0, 5);
  markLessonContextSent(event.session_id);
  const hint = `Agent Memory lesson overview — ${project}\n${formatLessons(overview)}`.slice(0, 6000);
  return overview.length ? {
    systemMessage: hint,
    hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: hint },
  } : {};
}
main().then(out => console.log(JSON.stringify(out))).catch(error => {
  console.error(`[agent-memory:prompt] ${error.message}`);
  console.log('{}');
});
