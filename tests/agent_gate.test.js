const test = require('node:test');
const assert = require('node:assert');
const { evaluate } = require('../scripts/agent_gate.js');

const SHA = 'a'.repeat(40);
const OLD = 'b'.repeat(40);
const c = (body, assoc = 'OWNER') => ({ body, author_association: assoc });
const both = (sha) => c(`review: APPROVE ${sha}\naudit: PASS ${sha}`);

test('both lines for the current head pass', () => {
  assert.strictEqual(evaluate([both(SHA)], SHA).pass, true);
});

test('lines split across two comments pass', () => {
  const r = evaluate([c(`review: APPROVE ${SHA}`), c(`audit: PASS ${SHA}`, 'MEMBER')], SHA);
  assert.strictEqual(r.pass, true);
});

test('evidence for an older sha fails', () => {
  const r = evaluate([both(OLD)], SHA);
  assert.strictEqual(r.pass, false);
  assert.strictEqual(r.reasons.length, 2);
});

test('quoted line does not count', () => {
  const r = evaluate([c(`> review: APPROVE ${SHA}\n> audit: PASS ${SHA}`)], SHA);
  assert.strictEqual(r.pass, false);
});

test('lines inside a fenced code block do not count', () => {
  const body = `example:\n\`\`\`\nreview: APPROVE ${SHA}\naudit: PASS ${SHA}\n\`\`\``;
  assert.strictEqual(evaluate([c(body)], SHA).pass, false);
});

test('non-member author does not count', () => {
  assert.strictEqual(evaluate([c(`review: APPROVE ${SHA}\naudit: PASS ${SHA}`, 'NONE')], SHA).pass, false);
});

test('missing one line fails and names it', () => {
  const r = evaluate([c(`review: APPROVE ${SHA}`)], SHA);
  assert.strictEqual(r.pass, false);
  assert.deepStrictEqual(r.reasons, [`audit: PASS ${SHA}`]);
});

test('fails closed on bad input', () => {
  assert.strictEqual(evaluate(null, SHA).pass, false);
  assert.strictEqual(evaluate([both(SHA)], '').pass, false);
  assert.strictEqual(evaluate([{ body: null }], SHA).pass, false);
});
