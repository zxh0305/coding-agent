import {createRequire} from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url), S = require('./skills_state.js');
const skill = (id, scope='once') => ({id, version:'v1', name:id, body:'old', scope});
test('send consumes one-time skills and retains session skills without mutating the queued snapshot', () => {
  const selected = [skill('review'), skill('polish', 'session')];
  const message = S.snapshot(selected);
  assert.deepEqual(S.afterSend(selected).map(s => s.id), ['polish']);
  const updated = S.update(selected, {...skill('review'), version:'v2', body:'new'});
  assert.equal(updated[0].version, 'v2');
  assert.equal(message[0].version, 'v1');
  assert.equal(selected[0].body, 'old');
});
test('applying a new version preserves the selected scope', () => {
  const result = S.update([skill('review', 'session')], {...skill('review'), version:'v2'});
  assert.equal(result[0].scope, 'session');
});
test('restoring legacy drafts defaults to one-time and removes invalid or duplicate entries', () => {
  assert.deepEqual(S.normalize(null), []);
  const items = S.normalize([null, {}, {id:'x',version:'v'}, {id:'x',version:'old'}]);
  assert.equal(items.length, 1); assert.equal(items[0].scope, 'once');
  assert.equal(S.normalize(Array.from({length:8},(_,i) => skill(String(i)))).length, 6);
});
test('auto-generated identifiers support Chinese names and avoid collisions', () => {
  assert.equal(S.autoId('论文润色', []), 'skill');
  assert.equal(S.autoId('论文润色', ['skill','skill-2']), 'skill-3');
  assert.equal(S.autoId('Code Review', []), 'code-review');
});
