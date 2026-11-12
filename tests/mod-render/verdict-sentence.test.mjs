// A model's verdict on the project page is one sentence. Two things are in it whenever they apply, because they
// change what the result means: where the evidence comes from, and that the model agrees with its own labeler.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/verdict-sentence.test.mjs
import assert from 'node:assert/strict';

const { verdictSentence } = await import('../../defaults/mods/captain-project/main.js');
const model = (evaluation, extra = {}) => ({ id: 'e1', pipeline: 'shop/ranker@v2', status: 'done', is_champion: false, evaluation, ...extra });
const EVIDENCE = { SIMULATED: 'Simulated events.', LLM_GRADED: 'LLM graded.', LOGGED_CLICKS: 'From logged clicks.', HUMAN_GRADED: 'Human graded.', REAL_TRAFFIC: 'Real traffic.' };

// The evidence word ends every judged verdict, whatever the verdict is.
for (const [evidence, words] of Object.entries(EVIDENCE)) {
  for (const comparison of ['win', 'loss', 'noise']) {
    const s = verdictSentence(model({ comparison, evidence, value: 0.4 }), 'keyword-baseline');
    assert.ok(s.endsWith(words), `${evidence} ${comparison}: ${s}`);
    assert.ok(s.includes('keyword-baseline'), `the other model by its own name: ${s}`);
  }
}
assert.equal(verdictSentence(model({ comparison: 'win', evidence: 'HUMAN_GRADED' }), 'keyword-baseline'), 'Ranks better than keyword-baseline. Human graded.');
assert.equal(verdictSentence(model({ comparison: 'loss', evidence: 'REAL_TRAFFIC' }), 'keyword-baseline'), 'Ranks worse than keyword-baseline. Real traffic.');
assert.equal(verdictSentence(model({ verdict: 'too few judged results to call', evidence: 'SIMULATED' }), 'keyword-baseline'),
  'Too few judged results to say whether it beats keyword-baseline. Simulated events.');

// Its own labeler: said whenever it applies, and never as a win, even when the comparison says "win".
for (const ev of [{ verdict: 'agrees with its own labeler', comparison: 'win', evidence: 'LLM_GRADED' }, { label_independence: { status: 'shared' }, comparison: 'win', evidence: 'SIMULATED' }]) {
  const s = verdictSentence(model(ev), 'keyword-baseline');
  assert.ok(/[Aa]grees with its own labeler/.test(s) && !s.includes('Ranks better'), `never a win: ${s}`);
  assert.ok(s.endsWith(EVIDENCE[ev.evidence]), `and the evidence is still said: ${s}`);
}
// Too few results and its own labeler at once: the labeler is not dropped.
const both = verdictSentence(model({ verdict: 'too few judged results to call', label_independence: { status: 'shared' }, evidence: 'SIMULATED' }), 'keyword-baseline');
assert.equal(both, 'Too few judged results to say whether it beats keyword-baseline. It also agrees with its own labeler. Simulated events.');

// Before there is a verdict it claims nothing.
assert.equal(verdictSentence(model(null), 'keyword-baseline'), 'Not compared with keyword-baseline yet.');
assert.equal(verdictSentence(model(null), null), 'Nothing to compare it with yet.');
assert.equal(verdictSentence(model(null, { status: 'backtesting' }), 'keyword-baseline'), 'Being compared with keyword-baseline now.');
assert.equal(verdictSentence(model(null, { status: 'failed' }), 'keyword-baseline'), "Comparing it didn't finish. Open Details to try again.");
assert.equal(verdictSentence(model({ evidence: 'SIMULATED' }, { is_champion: true }), null), 'The model the others are compared with. Simulated events.');
// A model that returns what the other returns is said so, in the same sentence.
assert.ok(verdictSentence(model({ comparison: 'noise', evidence: 'SIMULATED' }, { sameness: { mostly_same: true } }), 'keyword-baseline').includes('It mostly returns the same results as keyword-baseline.'));
// No sentence calls the other model "the baseline" or the search that runs today.
for (const ev of [{ comparison: 'win', evidence: 'SIMULATED' }, null]) assert.ok(!/the baseline|today's search/i.test(verdictSentence(model(ev), 'keyword-baseline')));
console.log('verdict sentence test: ok');
