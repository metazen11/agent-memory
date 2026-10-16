#!/usr/bin/env node
// Native Codex/Work prompt capture. Hook output is always contract-safe JSON.
const fs = require('fs');
const { randomUUID } = require('crypto');
const { normalizeProjectPath, requestJson, hintsEnabled, readJsonFile, LESSONS_FILE, formatLessons, lessonAppliesToProject, lessonIsGlobal, lessonContextSentForSession, reserveLessonContextDelivery, completeLessonContextDelivery, releaseLessonContextDelivery, saveSpooledPromptPayload } = require('./common');

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
    // Keep the scoped response canonical (full_path), then fetch explicit
    // globals separately. The non-strict response uses short names, which
    // cannot safely distinguish same-basename repositories.
    const scoped = await requestJson('GET', `/api/lessons?project=${encodeURIComponent(project)}&active=true&limit=100&strict_scope=true`, null, 1200);
    const globals = await requestJson('GET', '/api/lessons?active=true&limit=100', null, 1200);
    lessons = [
      ...(Array.isArray(scoped.data) ? scoped.data : []),
      ...(Array.isArray(globals.data) ? globals.data : []),
    ];
  } catch {
    const snapshot = readJsonFile(LESSONS_FILE, {});
    lessons = normalizeProjectPath(snapshot.project_path) === project ? snapshot.lessons || [] : [];
  }
  const scoped = lessons.filter(l => l.active !== false && (lessonIsGlobal(l) || lessonAppliesToProject(l, project)));
  const severity = { critical: 0, warning: 1, info: 2 };
  const overview = scoped
    .sort((a, b) => (severity[a.severity] ?? 3) - (severity[b.severity] ?? 3))
    .slice(0, 5);
  const hint = `Agent Memory lesson overview — ${project}\n${formatLessons(overview)}`.slice(0, 6000);
  if (!overview.length) return {};
  const deliveryToken = reserveLessonContextDelivery(event.session_id);
  if (!deliveryToken) return {};
  return {
    __lessonDelivery: { sessionId: event.session_id, token: deliveryToken },
    systemMessage: hint,
    hookSpecificOutput: { hookEventName: 'UserPromptSubmit', additionalContext: hint },
  };
}
main().then(out => {
  const delivery = out?.__lessonDelivery;
  if (delivery) delete out.__lessonDelivery;
  const serialized = JSON.stringify(out) + '\n';
  process.stdout.write(serialized, (error) => {
    if (!delivery) return;
    if (error) releaseLessonContextDelivery(delivery.sessionId, delivery.token);
    else completeLessonContextDelivery(delivery.sessionId, delivery.token);
  });
}).catch(error => {
  console.error(`[agent-memory:prompt] ${error.message}`);
  console.log('{}');
});
