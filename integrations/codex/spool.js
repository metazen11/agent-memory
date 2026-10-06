// Durable replay shared by local drains and the host-wide recovery job.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = file + '.' + crypto.randomUUID() + '.tmp';
  const fd = fs.openSync(temporary, 'wx', 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value) + '\n'); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  fs.renameSync(temporary, file);
  if (process.platform !== 'win32') {
    const dir = fs.openSync(path.dirname(file), 'r');
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
  }
}

function save(directory, payload) {
  const file = path.join(directory, `${Date.now()}-${crypto.randomUUID()}.json`);
  atomicJson(file, payload);
  return file;
}

function files(directory) {
  try { return fs.readdirSync(directory).filter(f => f.endsWith('.json')).sort().map(f => path.join(directory, f)); }
  catch { return []; }
}

function claim(file) {
  const lock = file + '.lock';
  try { fs.mkdirSync(lock); }
  catch (error) {
    if (error.code !== 'EEXIST') return null;
    try {
      const owner = JSON.parse(fs.readFileSync(path.join(lock, 'owner.json')));
      try { process.kill(owner.pid, 0); return null; }
      catch (error) { if (error.code !== 'ESRCH') return null; }
      fs.rmSync(lock, { recursive: true });
      return claim(file);
    } catch {
      // A writer may be between mkdir and recording ownership.
      if (Date.now() - fs.statSync(lock).mtimeMs < 300000) return null;
      fs.rmSync(lock, { recursive: true });
      return claim(file);
    }
  }
  fs.writeFileSync(path.join(lock, 'owner.json'), JSON.stringify({ pid: process.pid }));
  return () => { try { fs.rmSync(lock, { recursive: true }); } catch {} };
}

function quarantine(file, reason) {
  const target = path.join(path.dirname(path.dirname(file)), 'quarantine', path.basename(file));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const destination = target + '.' + crypto.randomUUID();
  fs.renameSync(file, destination);
  atomicJson(destination + '.reason.json', { reason, at: new Date().toISOString() });
}

async function drain(allFiles, send, { limit = Infinity, delayMs = 750 } = {}) {
  const result = { drained: 0, quarantined: 0, skipped: 0, error: null };
  for (const file of allFiles) {
    if (result.drained + result.quarantined >= limit) break;
    const release = claim(file);
    if (!release) { result.skipped++; continue; }
    try {
      let raw, event;
      try { raw = fs.readFileSync(file, 'utf8'); event = JSON.parse(raw); }
      catch (error) {
        if (error.code === 'ENOENT') continue;
        if (!(error instanceof SyntaxError)) { result.error = error.code || 'file_read_failed'; result.skipped++; continue; }
        quarantine(file, 'invalid_json'); result.quarantined++; continue;
      }
      const route = event?.route || '/api/queue';
      const payload = event?.route ? event.payload : event;
      if (!['/api/queue', '/api/prompts'].includes(route) || !payload || typeof payload !== 'object' || !payload.session_id) {
        quarantine(file, 'invalid_envelope'); result.quarantined++; continue;
      }
      // Same legacy file copied into a worktree must retain the same identity.
      const ingest_id = payload.ingest_id || crypto.createHash('sha256').update(path.basename(file) + '\0' + raw).digest('hex');
      try {
        await send(route, { ...payload, ingest_id });
        fs.unlinkSync(file);
        result.drained++;
      } catch (error) {
        if ([400, 404, 413, 422].includes(error.status)) {
          quarantine(file, `http_${error.status}`); result.quarantined++;
        } else { result.error = error.status ? `http_${error.status}` : 'unreachable'; break; }
      }
      if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs));
    } finally { release(); }
  }
  return result;
}

function discover(root) {
  const directories = [];
  const skip = new Set(['.git', 'node_modules', '.venv', 'venv', '.venv-finetune', '.next', 'models', '__pycache__', 'Dropbox']);
  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (!entry.isDirectory() || skip.has(entry.name)) continue; // Never follow symlinks.
      const full = path.join(dir, entry.name);
      if (entry.name === '.agent-memory-codex') {
        if (fs.existsSync(path.join(full, 'spool'))) directories.push(path.join(full, 'spool'));
      } else walk(full);
    }
  }
  walk(root);
  return directories;
}

module.exports = { atomicJson, save, files, drain, discover };
