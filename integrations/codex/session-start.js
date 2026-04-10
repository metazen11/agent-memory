#!/usr/bin/env node
const {
  CONTEXT_FILE,
  generateSessionId,
  projectNameFromCwd,
  requestJson,
  writeContext,
  writeSessionState,
  runEnsureServices,
  refreshSnapshots,
  readJsonFile,
  RECENT_FILE,
  LESSONS_FILE,
  formatRecentObservations,
  formatLessons,
  sessionHintsEnabled,
} = require('./common');

async function main() {
  const cwd = process.cwd();
  const project = projectNameFromCwd(cwd);
  const sessionId = process.env.AGENT_MEMORY_SESSION_ID || generateSessionId();
  const hintsOn = sessionHintsEnabled();

  let online = true;
  let notices = [];
  const allowHostRecovery = process.env.AGENT_MEMORY_CODEX_HOST_RECOVERY === '1';
  const startSession = async () => {
    try {
      await requestJson('POST', '/api/sessions', {
        session_id: sessionId,
        project,
        project_path: cwd,
        agent_type: 'codex-cli',
      }, 4000);
    } catch (e) {
      if (e.status !== 409) throw e;
    }
  };

  let recent = { observations: [] };
  let lessons = [];
  try {
    await startSession();
    ({ recent, lessons } = await refreshSnapshots({ projectPath: cwd, projectName: project, includeLessons: hintsOn }));
  } catch (e) {
    if (allowHostRecovery) {
      const recovery = runEnsureServices();
      notices.push('agent-memory was unavailable; attempted restart');
      if (recovery.stdout) notices.push(recovery.stdout.replace(/\n/g, ' | '));
      try {
        await startSession();
        ({ recent, lessons } = await refreshSnapshots({ projectPath: cwd, projectName: project, includeLessons: hintsOn }));
      } catch {
        online = false;
        notices.push('still offline; using local snapshot/spool mode');
        recent = readJsonFile(RECENT_FILE, { observations: [] }) || { observations: [] };
        if (hintsOn) {
          lessons = (readJsonFile(LESSONS_FILE, { lessons: [] }) || { lessons: [] }).lessons || [];
        }
      }
    } else {
      online = false;
      notices.push('agent-memory unavailable; using local snapshot/spool mode');
      recent = readJsonFile(RECENT_FILE, { observations: [] }) || { observations: [] };
      if (hintsOn) {
        lessons = (readJsonFile(LESSONS_FILE, { lessons: [] }) || { lessons: [] }).lessons || [];
      }
    }
  }

  const context = [
    '# Agent Memory (Codex)',
    '',
    `Session ID: \`${sessionId}\``,
    `Project: \`${cwd}\``,
    '',
    'Use MCP server `agent-memory` for memory lookup (`search`, `timeline`, `get_observations`, `save_memory`).',
    '',
    notices.length ? `Status: ${notices.join(' ; ')}` : 'Status: online',
    '',
    hintsOn
      ? formatLessons(lessons)
      : '## Active Lessons\n\nSession-start hints disabled (`AGENT_MEMORY_SESSION_HINTS_ENABLED=0`).\n',
    formatRecentObservations(recent),
    '## Workflow Notes',
    '',
    hintsOn
      ? '- Before risky Bash/Edit/Write actions, run `node integrations/codex/pre-tool-trigger.js --tool <Tool> --input <preview>` (falls back to local lesson snapshot offline).'
      : '- Session-start hint injection is disabled (`AGENT_MEMORY_SESSION_HINTS_ENABLED=0`).',
    '- After meaningful tool actions, run `node integrations/codex/post-tool-hook.js --tool <Tool> --input <json> --output <preview>` (spools locally if offline).',
    '- End the session with `node integrations/codex/session-end.js` (done automatically by `scripts/codex-agent-memory.sh`).',
    '',
  ].join('\n');

  writeSessionState({ session_id: sessionId, project, project_path: cwd, agent_type: 'codex-cli' });
  writeContext(context);

  console.log(JSON.stringify({
    ok: true,
    online,
    session_id: sessionId,
    context_file: CONTEXT_FILE,
  }));
}

main().catch((e) => {
  console.error(JSON.stringify({
    ok: false,
    error: e.message || String(e),
  }));
  process.exit(1);
});
