// Comparisons' Profile tab without a backend: a canned profile with a budget breach, shares, memory, a skipped arm, and
// the slowest queries. Checks the words on screen.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/profile.test.mjs
import assert from 'node:assert/strict';
import { Profiler } from '../../defaults/mods/captain-compare/profile.js';
import { palette } from '../../defaults/captain/kit/tokens.js';

const stage = (s, p99, share) => ({ stage: s, kind: s, p50_ms: p99 / 2, p95_ms: p99 * 0.9, p99_ms: p99, mean_ms: p99 / 2, share });
const arm = (name, stages, budget) => ({
  pipeline: name, engine: 'local-bm25', queries: 24, explained: true,
  total: { p50_ms: 2.1, p95_ms: 3.0, p99_ms: stages.reduce((n, s) => n + s.p99_ms, 0), mean_ms: 2.2 },
  stages, memory: { peak_python_bytes_p50: 109339, peak_python_bytes_max: 1250000, what: 'Python allocations' },
  slowest: [{ query_id: 'q7', query: 'waterproof hiking boots', total_ms: 9.25, dominant_stage: 'boosts', stages_ms: { boosts: 6.5 }, candidates: 1200 }],
  budget, measured_on: {},
});
const result = {
  run: 'r@v1', queries: 24, summary: ['demo-tuned@v1 adds 4.0 ms at p99 against demo-bm25@v1, most of it in boosts (+3.8 ms)'],
  arms: [arm('demo-bm25@v1', [stage('retrieve', 2.0, 0.8), stage('rules', 0.5, 0.2)], null),
    arm('demo-tuned@v1', [stage('retrieve', 2.2, 0.35), stage('boosts', 3.8, 0.6), stage('rules', 0.4, 0.05)], { ok: false, breaches: ['p99 is 6.4 ms, over the 5 ms budget'] })],
  skipped: [{ arm: 'vendor-x', reason: 'from imported rankings, so it can\'t be replayed to time it' }],
  measured_on: { machine: 'Darwin arm64', python: '3.14.0', concurrency: 1 },
};
const host = { runRef: () => 'r@v1' };
const p = new Profiler(host);
p.byRun.set('r@v1', { job: null, result });
const c = palette(true);
const texts = (e, out = []) => { if (typeof e === 'string') out.push(e); else if (e && typeof e === 'object') for (const k of e.kids ?? []) texts(k, out); return out; };
const t = texts(p.render(c, 1100)).join(' | ');
assert.ok(t.includes('Demo-tuned@v1 adds 4.0 ms at p99 against demo-bm25@v1, most of it in boosts (+3.8 ms).'), 'the summary, first');
assert.ok(t.includes('Over budget') && t.includes('Demo-tuned@v1: p99 is 6.4 ms, over the 5 ms budget.'), 'a breach, in words');
assert.ok(t.includes('Vendor-x isn\'t timed: from imported rankings'), 'a skipped arm, with its reason');
assert.ok(t.includes('boosts') && t.includes('3.8 ms, 60%'), 'a stage with its p99 and share');
assert.ok(t.includes('Memory, Python allocations, not process memory: 109 kB per query at the median, 1.3 MB at most.'), 'memory, labelled');
assert.ok(t.includes('waterproof hiking boots') && t.includes('boosts, 6.5 ms') && t.includes('1,200'), 'the slowest query');
assert.ok(t.includes('Measured offline on this computer, not production.') && t.includes('Darwin arm64, Python 3.14.0'), 'where it was measured');
// Nothing run yet: what it does, and the label anyway.
const empty = texts(new Profiler({ runRef: () => 'other@v1' }).render(c, 1100)).join(' | ');
assert.ok(empty.includes('Profile this run') && empty.includes('Measured offline on this computer, not production.'), 'before a profile');
console.log('profile test: ok');
