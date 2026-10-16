#!/usr/bin/env node
// Native Codex/Work prompt capture. Hook output is always contract-safe JSON.
const fs = require('fs');
const { randomUUID } = require('crypto');
const { normalizeProjectPath, requestJson, hintsEnabled, formatLessons, selectScopedLessons, lessonContextSentForSession, reserveLessonContextDelivery, completeLessonContextDelivery, releaseLessonContextDelivery, saveSpooledPromptPayload } = require('./common');

function isLessonRecord(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Number.isInteger(value.id)
    && typeof value.title === 'string'
    && typeof value['rule'] === 'string'
    && typeof value.severity === 'string';
}

function isLessonResponse(value) {
  return Array.isArray(value) && value.every(isLessonRecord);
}

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
    if (!isLessonResponse(scoped.data) || !isLessonResponse(globals.data)) {
      throw new Error('invalid lesson response shape');
    }
    lessons = [
      ...scoped.data,
      ...globals.data,
    ];
  } catch {
    // Retrieval errors must remain retryable; a stale snapshot must not mark
    // this session delivered or turn a transient outage into a silent miss.
    return {};
  }
  const deliveryToken = reserveLessonContextDelivery(event.session_id);
  if (!deliveryToken) return {};
  const overview = selectScopedLessons(lessons, project, 5);
  const hint = `Agent Memory lesson overview — ${project}\n${formatLessons(overview)}`.slice(0, 6000);
  if (!overview.length) return { __lessonDelivery: { sessionId: event.session_id, token: deliveryToken } };
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
  let settled = false;
  const finish = (error) => {
    if (settled) return;
    settled = true;
    if (!delivery) return;
    if (error) releaseLessonContextDelivery(delivery.sessionId, delivery.token);
    else completeLessonContextDelivery(delivery.sessionId, delivery.token);
  };
  process.stdout.once('error', finish);
  process.stdout.write(serialized, finish);
}).catch(error => {
  console.error(`[agent-memory:prompt] ${error.message}`);
  console.log('{}');
});
