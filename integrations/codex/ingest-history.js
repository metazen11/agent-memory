#!/usr/bin/env node
/**
 * Ingest Codex user prompts into agent-memory.
 *
 * Codex exposes no UserPromptSubmit-style hook, so prompts never reached
 * mem_user_prompts the way Claude's user-prompt-submit.js delivers them.
 * Without them, mem_observations.prompt_number stays NULL for every Codex row
 * and the tool_calls⋈user_prompts join used by the fine-tune dataset builder
 * yields no Codex training data.
 *
 * Codex does maintain ~/.codex/history.jsonl ({session_id, text, ts}), which is
 * the same information after the fact. This tails that file and POSTs new
 * entries to /api/prompts. The server hashes prompt content for idempotency,
 * so re-running is safe; we additionally persist a cursor to avoid re-POSTing
 * the whole file on every invocation.
 *
 * Wired to SessionStart (drains the previous session's prompts) and SessionEnd.
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { SERVER_BASE, STATE_DIR, ensureStateDir, requestJson } = require('./common.js');

const HISTORY_FILE = process.env.AGENT_MEMORY_CODEX_HISTORY
  || path.join(os.homedir(), '.codex', 'history.jsonl');
const CURSOR_FILE = path.join(STATE_DIR, 'history.cursor.json');
const MAX_BATCH = Number(process.env.AGENT_MEMORY_CODEX_HISTORY_BATCH || 500);

function readCursor() {
  try {
    return JSON.parse(fs.readFileSync(CURSOR_FILE, 'utf8'));
  } catch {
    return { offset: 0, inode: null };
  }
}

function writeCursor(cursor) {
  try {
    ensureStateDir();
    fs.writeFileSync(CURSOR_FILE, JSON.stringify(cursor, null, 2));
  } catch { /* cursor is an optimization; content-hash dedupe is the guarantee */ }
}

async function main() {
  if (!fs.existsSync(HISTORY_FILE)) {
    console.log(JSON.stringify({ ok: true, skipped: 'no_history_file' }));
    return;
  }

  const stat = fs.statSync(HISTORY_FILE);
  let cursor = readCursor();

  // Truncated or rotated file → restart from the beginning.
  if (cursor.inode !== stat.ino || cursor.offset > stat.size) {
    cursor = { offset: 0, inode: stat.ino };
  }
  if (cursor.offset === stat.size) {
    console.log(JSON.stringify({ ok: true, ingested: 0, reason: 'up_to_date' }));
    return;
  }

  const fd = fs.openSync(HISTORY_FILE, 'r');
  const len = stat.size - cursor.offset;
  const buf = Buffer.alloc(len);
  fs.readSync(fd, buf, 0, len, cursor.offset);
  fs.closeSync(fd);

  const text = buf.toString('utf8');
  // A trailing partial line must not be consumed; leave it for the next run.
  const lastNl = text.lastIndexOf('\n');
  if (lastNl === -1) {
    console.log(JSON.stringify({ ok: true, ingested: 0, reason: 'no_complete_line' }));
    return;
  }
  const complete = text.slice(0, lastNl);
  const consumed = Buffer.byteLength(complete, 'utf8') + 1;

  const rows = [];
  for (const line of complete.split('\n')) {
    if (!line.trim()) continue;
    try {
      const d = JSON.parse(line);
      if (d && typeof d.text === 'string' && d.text.trim() && d.session_id) rows.push(d);
    } catch { /* skip malformed line, keep draining */ }
  }

  // The API enforces a 100-writes/min token bucket (app/config.py:46). Firing
  // the whole batch at once gets most of it rejected, so pace requests just
  // under that ceiling and back off once on a rejection rather than dropping
  // the prompt.
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const MIN_INTERVAL_MS = Number(process.env.AGENT_MEMORY_CODEX_HISTORY_INTERVAL_MS || 650);

  let ingested = 0;
  let failed = 0;
  for (const row of rows.slice(0, MAX_BATCH)) {
    const payload = {
      session_id: String(row.session_id),
      prompt: row.text,
      cwd: row.cwd || process.cwd(),
      agent_name: 'codex-cli',
    };
    let ok = false;
    for (let attempt = 0; attempt < 2 && !ok; attempt += 1) {
      try {
        await requestJson('POST', '/api/prompts', payload);
        ok = true;
      } catch {
        if (attempt === 0) await sleep(2000); // likely rate-limited; back off once
      }
    }
    if (ok) ingested += 1; else failed += 1;
    await sleep(MIN_INTERVAL_MS);
  }

  // Only advance the cursor if nothing failed, so a transient outage retries.
  if (failed === 0) writeCursor({ offset: cursor.offset + consumed, inode: stat.ino });

  console.log(JSON.stringify({ ok: true, ingested, failed, candidates: rows.length }));
}

main().catch((e) => {
  console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
});
