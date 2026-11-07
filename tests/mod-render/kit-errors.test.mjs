// The kit never shows people a storage key, a path, a bare HTTP status or a transport's words as an error.
//   node --import ./tests/mod-render/stubs/register.mjs tests/mod-render/kit-errors.test.mjs
import assert from 'node:assert/strict';
const { plainError } = await import('../../defaults/captain/kit/ui.js');
const fb = 'Something went wrong.';
assert.equal(plainError('backtest-runs/x/v1/MANIFEST.yaml'), fb);
assert.equal(plainError('models/generika/v3'), fb);
assert.equal(plainError('Not Found'), fb);
assert.equal(plainError('HTTP 500'), fb);
assert.equal(plainError('error sending request for url (http://127.0.0.1:4100/api/x)'), fb);
assert.equal(plainError('operation timed out'), 'This took too long.');
assert.equal(plainError(''), fb);
assert.equal(plainError("There's already a workspace called demo."), "There's already a workspace called demo.");
assert.equal(plainError('This login isn\'t allowed to write to s3://acme/captain.'), 'This login isn\'t allowed to write to s3://acme/captain.');
assert.equal(plainError('an engine is one of search, recommendation'), 'an engine is one of search, recommendation');
console.log('kit errors test: ok');
