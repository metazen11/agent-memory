/**
 * Path normalization at the hook write boundary.
 *
 * Mirror of app/path_normalize.py — keep the prefix table in sync.
 * Per CLAUDE.md, the source of truth is ~/_CODING/; ~/Dropbox/_CODING/
 * is a stale backup mirror. Normalizing at the hook prevents stale paths
 * from re-entering the DB via POST /api/prompts and POST /api/queue.
 *
 * Tiny, dependency-free, safe on hot paths (no FS access, no resolution).
 */
'use strict';

const PREFIXES = [
  ['/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/'],
  ['~/Dropbox/_CODING/',          '~/_CODING/'],
  ['%2FUsers%2Fmz%2FDropbox%2F_CODING%2F', '%2FUsers%2Fmz%2F_CODING%2F'],
];

// Probe: 99% of strings don't match, so check before doing the full replace.
const PROBE_RE = /(?:\/Users\/mz|~)\/Dropbox\/_CODING\//;

function normalizeText(s) {
  if (typeof s !== 'string' || !s || !PROBE_RE.test(s)) return s;
  let out = s;
  for (const [oldPrefix, newPrefix] of PREFIXES) {
    if (out.includes(oldPrefix)) out = out.split(oldPrefix).join(newPrefix);
  }
  return out;
}

function normalizeJson(obj) {
  if (obj == null) return obj;
  if (typeof obj === 'string') return normalizeText(obj);
  if (Array.isArray(obj)) {
    let changed = false;
    const out = new Array(obj.length);
    for (let i = 0; i < obj.length; i++) {
      const v = normalizeJson(obj[i]);
      if (v !== obj[i]) changed = true;
      out[i] = v;
    }
    return changed ? out : obj;
  }
  if (typeof obj === 'object') {
    let changed = false;
    const out = {};
    for (const k of Object.keys(obj)) {
      const v = normalizeJson(obj[k]);
      if (v !== obj[k]) changed = true;
      out[k] = v;
    }
    return changed ? out : obj;
  }
  return obj;
}

module.exports = { normalizeText, normalizeJson };
