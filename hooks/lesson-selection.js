const path = require('path');
const { normalizeText } = require('./_path-normalize');

const severityRank = { critical: 3, warning: 2, info: 1 };

function normalizeProject(value) {
  if (!value) return '';
  const text = normalizeText(String(value))
    .replace(/^\/Users\/mz\/Dropbox\/_CODING(?=\/|$)/, '/Users/mz/_CODING')
    .replace(/\/$/, '');
  return path.normalize(text) || '/';
}

function ownerOf(lesson) {
  return normalizeProject(lesson['project_name'] || lesson.project || '');
}

function isGlobal(lesson) {
  return !ownerOf(lesson) && lesson.project_id == null;
}

function appliesToProject(lesson, project) {
  const owner = ownerOf(lesson);
  const target = normalizeProject(project);
  if (isGlobal(lesson)) return true;
  if (!owner || !target) return false;
  return target === owner || target.startsWith(`${owner}/`);
}

function policyOf(lesson) {
  const text = `${lesson.title || ''} ${lesson['rule'] || ''}`.toLowerCase();
  if (text.includes('never force-push this repository')) return 'never-repository';
  if (text.includes('always force-push after rebasing')) return 'always-rebase';
  return null;
}

function selectScopedLessons(lessons, project, limit = 5) {
  const target = normalizeProject(project);
  const scoped = (Array.isArray(lessons) ? lessons : [])
    .filter(lesson => lesson && lesson.active !== false && appliesToProject(lesson, target))
    .sort((a, b) => {
      const ownerA = ownerOf(a);
      const ownerB = ownerOf(b);
      const specificity = owner => !owner ? 0 : target === owner ? 3 : target.startsWith(`${owner}/`) ? 2 : -1;
      return specificity(ownerB) - specificity(ownerA)
        || (severityRank[String(b.severity || '').toLowerCase()] || 0)
          - (severityRank[String(a.severity || '').toLowerCase()] || 0)
        || Number(a.id || 0) - Number(b.id || 0);
    });

  const seen = new Set();
  const policies = new Map();
  const selected = [];
  for (const lesson of scoped) {
    // Same rule is a duplicate; same title with different rules is not.
    const key = String(lesson['rule'] || lesson.title || '');
    if (seen.has(key)) continue;
    const policy = policyOf(lesson);
    if (policy && policies.has('force-push') && policies.get('force-push') !== policy) continue;
    seen.add(key);
    if (policy) policies.set('force-push', policy);
    selected.push(lesson);
    if (selected.length >= limit) break;
  }
  return selected;
}

module.exports = { selectScopedLessons };
