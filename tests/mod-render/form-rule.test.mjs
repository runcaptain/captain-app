// The owner's rule for every form: "validation needs to be automatic, not after submitting". He typed "gpu cloud"
// as a name, pressed the button and got the registry's own sentence, "bad name 'gpu cloud': use lowercase letters...".
// For the forms on Data and Notebooks: a field is checked as it is typed, its line sits under it, the button is
// off with the reason while something is missing, any words are a name, and nothing raw reaches the screen.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/form-rule.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname);
const root = path.resolve(here, '../..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

// The checks are one file, in the kit. captain-forms reads the kit's copy like every other mod, and has none of its own.
assert.ok(!fs.existsSync(path.join(root, 'defaults/mods/captain-forms/checks.js')), 'captain-forms keeps no copy of its own');
assert.ok(read('defaults/mods/captain-forms/main.js').includes('from "./kit/checks.js"'), 'it reads the kit\'s');
const { idFrom, sayError, looksRaw } = await import('../../defaults/captain/kit/checks.js');

// ---- a new notebook's name ---------------------------------------------------------------------------------------
const { newNameCheck, importPathCheck, Notebooks } = await import('../../defaults/mods/captain-notebooks/notebooks.js');
// A notebook does not need a name to start: with nothing typed the button works at once and Captain names it.
assert.deepEqual(newNameCheck('', []), { id: 'untitled-notebook', text: null, off: null, untitled: true });
assert.equal(newNameCheck('  ', [{ name: 'untitled-notebook' }]).id, 'untitled-notebook-2', 'then the next free one');
assert.equal(newNameCheck('', [{ name: 'untitled-notebook' }, { name: 'untitled-notebook-2' }]).id, 'untitled-notebook-3');
let k = newNameCheck('GPU cloud notes', []);
assert.deepEqual([k.id, k.text, k.off], ['gpu-cloud-notes', 'Captain will call it gpu-cloud-notes.', null], 'any words are a name');
assert.equal(idFrom('GPU cloud notes', 'notebook'), k.id, 'by the one rule');
assert.equal(newNameCheck('weekly', []).text, null, 'already an id: nothing to say');
k = newNameCheck('Weekly', [{ name: 'weekly' }]);
assert.deepEqual([k.text, k.off], ['You already have a notebook called weekly. This opens it.', null], 'a name in use is said as it is typed, and is not an error');
assert.equal(newNameCheck('!!!', []).id, 'notebook', 'never empty, never refused');

// ---- a notebook to import ----------------------------------------------------------------------------------------
assert.equal(importPathCheck('', null).off, 'Type the path of a notebook file.');
assert.deepEqual(importPathCheck('~/x.ipynb', 'checking'), { text: 'Checking...', off: null }, 'it says it is looking, and does not make a fast typist wait');
k = importPathCheck('~/x.ipynb', false);
assert.ok(k.bad && k.off && k.text === "There's no file at ~/x.ipynb, or it's over 1 MB.", 'said under the field, before any button is pressed');
assert.deepEqual(importPathCheck('~/x.ipynb', true), { text: null, off: null });

// Drawn: the line under the field, the button off with its reason, and nothing happening on a click while off.
{
  const { palette } = await import('../../defaults/mods/captain-notebooks/kit/tokens.js');
  const all = (e, out = []) => { if (e && typeof e === 'object') { out.push(e); for (const x of e.kids ?? []) all(x, out); } return out; };
  const idOf = (e) => String(e.call?.('id')?.[0] ?? '');
  const text = (e) => (e?.kids ?? []).flatMap((x) => (typeof x === 'string' ? [x] : x && typeof x === 'object' ? text(x) : []));
  let went = 0;
  const nb = Object.assign(Object.create(Notebooks.prototype), { icon: () => ({ kind: 'icon' }) });
  const draw = (check) => all(nb.field(palette(false), 'nb-new', { value: () => '' }, 'plus', 'New', () => went++, check));
  // A check that says the button is off: its reason is one hover away, and a click does nothing.
  let tree = draw({ text: null, off: 'Type the path of a notebook file.' });
  assert.equal(tree.find((e) => idOf(e) === 'nb-new-go-wrap').call('tooltip')[0], 'Type the path of a notebook file.', 'the reason is one hover away');
  assert.equal(tree.find((e) => idOf(e) === 'nb-new-go').call('on_click'), undefined, 'an off button has nothing to press');
  // New, with no name typed: on.
  tree = draw(newNameCheck('', []));
  tree.find((e) => idOf(e) === 'nb-new-go').call('on_click')[0]({}, {});
  assert.equal(went, 1, 'New works with nothing typed');
  went = 0;
  tree = draw(newNameCheck('GPU cloud notes', []));
  assert.deepEqual(text(tree.find((e) => idOf(e) === 'nb-new-says')), ['Captain will call it gpu-cloud-notes.']);
  tree.find((e) => idOf(e) === 'nb-new-go').call('on_click')[0]({}, {});
  assert.equal(went, 1);
  // The submit path itself refuses an empty name and makes the id: an agent or a test can call it with no form shown.
  const opened = [];
  const sub = Object.assign(Object.create(Notebooks.prototype), { list: [], newName: { value: () => '  ', set_value() {} }, openNotebook: (_cx, n) => opened.push(n) });
  sub.newNotebook({ notify() {} });
  assert.deepEqual(opened, ['untitled-notebook'], 'no name: Captain names it');
  sub.newNotebook({ notify() {} });
  assert.deepEqual(opened, ['untitled-notebook', 'untitled-notebook-2'], 'and the next one gets the next name');
  opened.length = 0;
  sub.newName = { value: () => 'GPU cloud notes', set_value() {} };
  sub.newNotebook({ notify() {} });
  assert.deepEqual(opened, ['gpu-cloud-notes']);
}

// ---- moving a table to a new folder ------------------------------------------------------------------------------
{
  const src = read('defaults/mods/captain-data/dataset-page.js');
  const { DatasetPage } = await import('../../defaults/mods/captain-data/dataset-page.js').catch(() => ({}));
  const Page = DatasetPage ?? Object.values(await import('../../defaults/mods/captain-data/dataset-page.js')).find((v) => typeof v === 'function' && v.prototype?.folderCheck);
  const page = (typed, at = 'logs', folders = ['logs', 'Catalogs']) => Object.assign(Object.create(Page.prototype), { newFolder: { value: () => typed }, folder: () => at, folders });
  assert.deepEqual(page('').folderCheck(), { folder: '', text: null, off: "Type a folder's name." });
  assert.deepEqual(page(' /Shop / Q4/ ').folderCheck(), { folder: 'Shop/Q4', text: 'Captain will make the folder Shop/Q4 and move it there.', off: null }, 'any words, and a path keeps its levels');
  assert.equal(page('Catalogs').folderCheck().text, 'It will move to Catalogs, which is already there.');
  assert.deepEqual([page('logs').folderCheck().off, page('logs').folderCheck().text], ['It is already there.', 'It is already in that folder.']);
  assert.ok(src.includes('disabled: !!this.folderCheck().off') && src.includes('"ds-move-says"') && src.includes('this.newFolder.on("change"'), 'drawn under the field as it is typed, with the button off');
}

// ---- nothing raw reaches the screen ------------------------------------------------------------------------------
for (const raw of ["bad name 'gpu cloud': use lowercase letters, digits, . _ - ...", 'Traceback (most recent call last): ...', 'ValueError: nope', 'Request failed (status 500)']) {
  const said = sayError(raw).text;
  assert.ok(!looksRaw(said) && /[.!?]$/.test(said), `${raw} becomes a sentence: ${said}`);
}
// Every place these forms show what the services said goes through a function that makes a sentence of it.
for (const f of ['defaults/mods/captain-notebooks/notebooks.js', 'defaults/mods/captain-data/dataset-page.js']) {
  const lines = read(f).split('\n').filter((l) => /[nN]ote = \{ text: .*(r\.error|data\?\.error|data\?\.detail)/.test(l));
  assert.ok(lines.length > 0);
  for (const l of lines) assert.ok(/sayError\(|plainNote\(/.test(l), `${f} shows the services' words through a sentence maker: ${l.trim().slice(0, 120)}`);
  assert.ok(!/text: String\(r\./.test(read(f)), `${f}: no raw String(r...) on screen`);
}
// The import wizard's buttons are off until there is something to act on, with the reason beside them.
const imp = read('defaults/mods/captain-import/main.js');
assert.ok(imp.includes('disabled: this.busy || !this.path.value().trim()') && imp.includes('"file-go-why"') && imp.includes('disabled: this.busy || !path') && imp.includes('"input-use-local-why"'));
console.log('form rule: ok');
process.exit(0);
