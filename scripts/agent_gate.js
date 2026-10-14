'use strict';
// Verdict logic for the agent-gate workflow. Runs from main (pull_request_target),
// so a PR cannot edit its own gate. Fails closed on any malformed input.
const TRUSTED = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);

// Lines of a comment that are not inside a fenced code block, trimmed.
function liveLines(body) {
  const out = [];
  let fenced = false;
  for (const raw of body.split(/\r?\n/)) {
    const line = raw.trim();
    if (/^(```|~~~)/.test(line)) { fenced = !fenced; continue; }
    if (!fenced) out.push(line);
  }
  return out;
}

function evaluate(comments, headSha) {
  try {
    if (!Array.isArray(comments) || !/^[0-9a-f]{40}$/.test(headSha || '')) {
      return { pass: false, reasons: ['invalid input (comments array / 40-hex head sha required)'] };
    }
    const lines = new Set();
    for (const c of comments) {
      if (!c || typeof c.body !== 'string' || !TRUSTED.has(c.author_association)) continue;
      for (const l of liveLines(c.body)) lines.add(l);
    }
    const required = [`review: APPROVE ${headSha}`, `audit: PASS ${headSha}`];
    const reasons = required.filter((l) => !lines.has(l));
    return { pass: reasons.length === 0, reasons };
  } catch (e) {
    return { pass: false, reasons: [`error: ${e.message}`] };
  }
}

module.exports = { evaluate };
