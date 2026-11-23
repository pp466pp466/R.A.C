'use strict';
/* rpg-story-view 6.2：行動建議不進本機暫存（game-store.js）。
 * 快照帶著 `suggestions`（錨點仍有效的建議，由玩家文字衍生，視為個資）；它只存在 game-session.js 的記憶體，
 * 本機暫存（記憶體後端與 IndexedDB 後端）的白名單不收，所以整個資料庫裡找不到它。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Store = require('../../app/static/js/game/game-store.js');
const { fakeIndexedDB, keyRange, dump } = require('./support/fake-indexeddb.js');

const SESSION = '99999999-9999-4999-8999-999999999999';
const MARKER = 'ZZSUGGEST_SECRET_ZZ';

const snapshotWithSuggestions = () => ({
  id: SESSION, title: '雨夜歸人', content_tier: 'safe', ended: false, completed_turns: 2, version: 3,
  opening: { state: 'ready', text: '雨落在石板路上。' },
  world: { elapsed_months: 4, location: '渡口', summary: '你剛到渡口。', goals: ['找到船夫'], places: ['渡口'], protagonist_age: 19 },
  suggestions: [`${MARKER}一`, `${MARKER}二`],
  last_turn: { client_msg_id: 'a', seq: 2, revision: 1, revisable: true, reason: null, free_left: 2, regenerate_cost: 0, edit_cost: 0 }
});

for (const [name, open] of [
  ['memory', () => Promise.resolve(Store.createStore(Store.createMemoryBackend()))],
  ['indexeddb', () => Store.openStore({ indexedDB: fakeIndexedDB(), name: 'game-cache-test', keyRange })]
]) {
  test(`the ${name} cache keeps the snapshot fields it always kept and nothing of the action suggestions`, async () => {
    const store = await open();
    const record = await store.putSession(SESSION, snapshotWithSuggestions());
    assert.deepEqual(Object.keys(record).sort(), ['completed_turns', 'content_tier', 'ended', 'id', 'opening', 'title', 'world']);
    assert.ok(!('suggestions' in record));
    assert.ok(!JSON.stringify(record).includes(MARKER));
    const read = await store.getSession(SESSION);
    assert.ok(!JSON.stringify(read).includes(MARKER));
  });
}

test('after a whole write sequence the IndexedDB holds no suggestion text anywhere, and the in-flight record has no suggestion field', async () => {
  const factory = fakeIndexedDB();
  const store = await Store.openStore({ indexedDB: factory, name: 'game-cache-test', keyRange });
  await store.putSession(SESSION, snapshotWithSuggestions());
  await store.putTurns(SESSION, [{ seq: 1, client_msg_id: 'a', turn_number: 1, status: 'complete', action: '行動', story: '劇情。', suggestions: [`${MARKER}三`] }]);
  await store.setInflight(SESSION, { client_msg_id: 'b', action: '在途的行動', created_at: 'x', suggestions: [`${MARKER}四`] });
  const rows = dump(factory);
  assert.ok(rows.length >= 3);
  assert.ok(!JSON.stringify(rows).includes(MARKER), '整個資料庫都不該有建議的文字');
  assert.ok(!JSON.stringify(rows).toLowerCase().includes('suggest'), '連欄位名稱都不該有');
});
