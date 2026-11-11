// 遊戲頁本機暫存的單元測試（rpg-game-ui 2.1）：記憶體後端與假 IndexedDB 兩種後端、白名單、帳號隔離、開啟失敗與寫入失敗時退回記憶體。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Store = require('../../app/static/js/game/game-store.js');
const { fakeIndexedDB, keyRange, dump } = require('./support/fake-indexeddb.js');

const SESSION = '99999999-9999-4999-8999-999999999999';
const OTHER = '88888888-8888-4888-8888-888888888888';

// 把寫進記憶體後端的每一個值都記下來（記憶體後端沒有公開的傾印；用包裝的 put 取得，等同檢查「整個資料庫的內容」）。
function recordingMemory() {
  const backend = Store.createMemoryBackend();
  const written = [];
  const put = backend.put;
  backend.put = (collection, key, value) => { written.push({ collection, key, value: JSON.parse(JSON.stringify(value)) }); return put(collection, key, value); };
  return { store: Store.createStore(backend), written };
}

const backends = {
  memory: () => Promise.resolve(recordingMemory().store),
  indexeddb: () => Store.openStore({ indexedDB: fakeIndexedDB(), name: 'game-cache-test', keyRange })
};

function turn(seq, extra) {
  return Object.assign({ seq, client_msg_id: 'm' + seq, turn_number: seq, status: 'complete', action: '行動' + seq, story: '劇情' + seq }, extra || {});
}

function snapshot(extra) {
  return Object.assign({
    id: SESSION, title: '雨夜歸人', content_tier: 'standard', status: 'active', ended: false, completed_turns: 3, version: 7,
    opening: { state: 'ready', text: '雨落在石板路上。' },
    world: { elapsed_months: 4, location: '渡口', summary: '你剛到渡口。', goals: ['找到船夫'], places: ['渡口', '客棧'], protagonist_age: 19 },
    protagonist: { name: '阿青', persona: '主角的角色卡全文' },
    notebook: { text: '記事簿全文：不可進快取', version: 2, length: 12, capacity: 2000, reserve: 500, full: false }
  }, extra || {});
}

for (const [name, open] of Object.entries(backends)) {
  test(`[${name}] the store reports which backend it uses`, async () => {
    const store = await open();
    assert.equal(store.mode, name);
  });

  test(`[${name}] a session snapshot is cached with exactly the whitelisted fields`, async () => {
    const store = await open();
    const record = await store.putSession(SESSION, snapshot());
    assert.deepEqual(Object.keys(record).sort(), ['completed_turns', 'content_tier', 'ended', 'id', 'opening', 'title', 'world']);
    const read = await store.getSession(SESSION);
    assert.deepEqual(read, {
      id: SESSION, title: '雨夜歸人', content_tier: 'standard', ended: false, completed_turns: 3,
      opening: { state: 'ready', text: '雨落在石板路上。' },
      world: { elapsed_months: 4, location: '渡口', summary: '你剛到渡口。', goals: ['找到船夫'], places: ['渡口', '客棧'], protagonist_age: 19 }
    });
    assert.equal(await store.getSession(OTHER), null);
  });

  test(`[${name}] an opening that is not written yet is cached as a state without text, and a malformed snapshot becomes safe defaults`, async () => {
    const store = await open();
    await store.putSession(SESSION, snapshot({ opening: { state: 'generating', text: null }, ended: undefined }));
    assert.deepEqual((await store.getSession(SESSION)).opening, { state: 'generating', text: '' });
    assert.equal((await store.getSession(SESSION)).ended, false);
    await store.putSession(OTHER, 'not a snapshot');
    assert.deepEqual(await store.getSession(OTHER), {
      id: OTHER, title: '', content_tier: '', ended: false, completed_turns: 0, opening: { state: null, text: '' },
      world: { elapsed_months: 0, location: '', summary: '', goals: [], places: [], protagonist_age: null }
    });
  });

  test(`[${name}] a session overwrite replaces the previous snapshot`, async () => {
    const store = await open();
    await store.putSession(SESSION, snapshot({ title: '舊標題', ended: false }));
    await store.putSession(SESSION, snapshot({ title: '新標題', ended: true }));
    const read = await store.getSession(SESSION);
    assert.deepEqual({ title: read.title, ended: read.ended }, { title: '新標題', ended: true });
  });

  test(`[${name}] turns come back ordered by numeric sequence, not by text`, async () => {
    const store = await open();
    await store.putTurns(SESSION, [turn(10), turn(2), turn(1), turn(100), turn(3)]);
    assert.deepEqual((await store.turnsOf(SESSION)).map((t) => t.seq), [1, 2, 3, 10, 100]);
    assert.equal(await store.maxSeq(SESSION), 100);
  });

  test(`[${name}] maxSeq is 0 for a session with no cached turns`, async () => {
    const store = await open();
    assert.equal(await store.maxSeq(SESSION), 0);
    assert.deepEqual(await store.turnsOf(SESSION), []);
  });

  test(`[${name}] the backend wins: writing a turn with the same sequence overwrites the cached one`, async () => {
    const store = await open();
    await store.putTurns(SESSION, [turn(1, { story: '本機舊的' }), turn(2)]);
    assert.equal(await store.putTurns(SESSION, [turn(1, { story: '後端的' })]), 1);
    assert.deepEqual((await store.turnsOf(SESSION)).map((t) => t.story), ['後端的', '劇情2']);
  });

  test(`[${name}] a blocked or interrupted turn keeps only its sequence and status, never any text`, async () => {
    const store = await open();
    await store.putTurns(SESSION, [
      turn(1),
      turn(2, { status: 'blocked', action: '被擋的行動', story: '被擋的劇情' }),
      turn(3, { status: 'interrupted', action: '中斷的行動', story: '中斷前的片段' })
    ]);
    const cached = await store.turnsOf(SESSION);
    assert.deepEqual(cached[1], { seq: 2, client_msg_id: 'm2', turn_number: 2, status: 'blocked', action: '', story: '' });
    assert.deepEqual(cached[2], { seq: 3, client_msg_id: 'm3', turn_number: 3, status: 'interrupted', action: '', story: '' });
    assert.ok(!JSON.stringify(cached).includes('被擋') && !JSON.stringify(cached).includes('中斷前'));
  });

  test(`[${name}] a turn that is still streaming is not cached, so a later catch-up from the max sequence cannot skip its final content`, async () => {
    const store = await open();
    assert.equal(await store.putTurns(SESSION, [turn(1), turn(2, { status: 'streaming', story: null, action: null })]), 1);
    assert.equal(await store.maxSeq(SESSION), 1);
  });

  test(`[${name}] entries without an integer sequence are ignored and the written count is reported`, async () => {
    const store = await open();
    const written = await store.putTurns(SESSION, [turn(1), { status: 'complete', story: '沒有序號' }, null, 'x', { seq: 1.5 }, { seq: -1 }, turn(2)]);
    assert.equal(written, 2);
    assert.deepEqual((await store.turnsOf(SESSION)).map((t) => t.seq), [1, 2]);
    assert.equal(await store.putTurns(SESSION, 'not a list'), 0);
  });

  test(`[${name}] turns of one session never leak into another whose id is a prefix`, async () => {
    const store = await open();
    await store.putTurns('abc', [turn(1)]);
    await store.putTurns('abcd', [turn(1, { story: '別的存檔' })]);
    assert.deepEqual((await store.turnsOf('abc')).map((t) => t.story), ['劇情1']);
    assert.deepEqual((await store.turnsOf('abcd')).map((t) => t.story), ['別的存檔']);
  });

  test(`[${name}] ids that could confuse the key prefix are rejected without reading or writing`, async () => {
    const store = await open();
    for (const bad of ['', 'a|b', undefined, null, 5, 'x'.repeat(101)]) {
      assert.equal(await store.putSession(bad, snapshot()), null);
      assert.equal(await store.getSession(bad), null);
      assert.equal(await store.putTurns(bad, [turn(1)]), 0);
      assert.deepEqual(await store.turnsOf(bad), []);
      assert.equal(await store.setInflight(bad, { client_msg_id: 'a', action: 'b' }), null);
      assert.equal(await store.getInflight(bad), null);
      await assert.doesNotReject(store.clearInflight(bad));
      await assert.doesNotReject(store.clearSession(bad));
    }
  });

  test(`[${name}] there is at most one in-flight turn per session and a second one replaces the first`, async () => {
    const store = await open();
    assert.equal(await store.getInflight(SESSION), null);
    const first = await store.setInflight(SESSION, { client_msg_id: 'id-1', action: '推開門', created_at: '2026-11-11T10:00:00.000Z' });
    assert.deepEqual(first, { client_msg_id: 'id-1', action: '推開門', created_at: '2026-11-11T10:00:00.000Z' });
    assert.deepEqual(await store.getInflight(SESSION), first);
    await store.setInflight(SESSION, { client_msg_id: 'id-2', action: '走進去' });
    assert.deepEqual(await store.getInflight(SESSION), { client_msg_id: 'id-2', action: '走進去', created_at: '' });
    await store.clearInflight(SESSION);
    assert.equal(await store.getInflight(SESSION), null);
    await assert.doesNotReject(store.clearInflight(SESSION));
  });

  test(`[${name}] in-flight turns of two sessions are independent`, async () => {
    const store = await open();
    await store.setInflight(SESSION, { client_msg_id: 'a', action: '甲' });
    await store.setInflight(OTHER, { client_msg_id: 'b', action: '乙' });
    await store.clearInflight(SESSION);
    assert.equal(await store.getInflight(SESSION), null);
    assert.equal((await store.getInflight(OTHER)).client_msg_id, 'b');
  });

  test(`[${name}] an in-flight entry without a usable client_msg_id is not stored, and extra fields are dropped`, async () => {
    const store = await open();
    for (const bad of [null, 'x', {}, { client_msg_id: '' }, { client_msg_id: 5 }, { client_msg_id: 'x'.repeat(201) }]) {
      assert.equal(await store.setInflight(SESSION, bad), null);
    }
    assert.equal(await store.getInflight(SESSION), null);
    const record = await store.setInflight(SESSION, { client_msg_id: 'id-1', action: '行動', created_at: 't', notebook: '記事簿', email: 'a@b.c' });
    assert.deepEqual(Object.keys(record).sort(), ['action', 'client_msg_id', 'created_at']);
  });

  test(`[${name}] clearing a session removes its snapshot, turns and in-flight turn and nothing else`, async () => {
    const store = await open();
    await store.putSession(SESSION, snapshot());
    await store.putSession(OTHER, snapshot({ title: '別的存檔' }));
    await store.putTurns(SESSION, [turn(1), turn(2)]);
    await store.putTurns(OTHER, [turn(1, { story: '別的劇情' })]);
    await store.setInflight(SESSION, { client_msg_id: 'a', action: 'x' });
    await store.setInflight(OTHER, { client_msg_id: 'b', action: 'y' });
    await store.clearSession(SESSION);
    assert.equal(await store.getSession(SESSION), null);
    assert.deepEqual(await store.turnsOf(SESSION), []);
    assert.equal(await store.getInflight(SESSION), null);
    assert.equal((await store.getSession(OTHER)).title, '別的存檔');
    assert.deepEqual((await store.turnsOf(OTHER)).map((t) => t.story), ['別的劇情']);
    assert.equal((await store.getInflight(OTHER)).client_msg_id, 'b');
  });

  test(`[${name}] clearing a session that was never cached does not fail`, async () => {
    const store = await open();
    await assert.doesNotReject(store.clearSession(SESSION));
  });
}

// ---- 整個資料庫的內容 ----------------------------------------------------------------------------------

const FORBIDDEN = ['記事簿全文', '主角的角色卡全文', '被擋的劇情', '中斷前的片段', 'secret@example.com', '2001-01-01', '18_24'];

function realisticFlow(store) {
  return store.putSession(SESSION, snapshot({ email: 'secret@example.com', birth_date: '2001-01-01', age_range: '18_24', user: { email: 'secret@example.com' } }))
    .then(() => store.putTurns(SESSION, [
      turn(1), turn(2, { status: 'blocked', story: '被擋的劇情' }), turn(3, { status: 'interrupted', story: '中斷前的片段' }),
      turn(4, { status: 'streaming' }), turn(5, { notebook: '記事簿全文', email: 'secret@example.com' })
    ]))
    .then(() => store.setInflight(SESSION, { client_msg_id: 'id-6', action: '推開門', notebook: '記事簿全文', email: 'secret@example.com' }));
}

test('[indexeddb] the whole database holds only the three collections and none of the notebook, character sheet, email, birth date or age range', async () => {
  const factory = fakeIndexedDB();
  const store = await Store.openStore({ indexedDB: factory, name: Store.databaseName('user-1'), keyRange });
  await realisticFlow(store);
  const rows = dump(factory);
  assert.ok(rows.length >= 6);
  assert.deepEqual([...new Set(rows.map((row) => row.collection))].sort(), ['inflight', 'sessions', 'turns']);
  const text = JSON.stringify(rows);
  for (const marker of FORBIDDEN) { assert.ok(!text.includes(marker), marker); }
  for (const forbiddenKey of ['notebook', 'protagonist', 'email', 'birth_date', 'age_range', 'version']) {
    assert.ok(!text.includes('"' + forbiddenKey + '"'), forbiddenKey);
  }
});

test('[memory] everything written to the memory backend holds none of the notebook, character sheet, email, birth date or age range', async () => {
  const { store, written } = recordingMemory();
  await realisticFlow(store);
  assert.ok(written.length >= 6);
  assert.deepEqual([...new Set(written.map((row) => row.collection))].sort(), ['inflight', 'sessions', 'turns']);
  const text = JSON.stringify(written);
  for (const marker of FORBIDDEN) { assert.ok(!text.includes(marker), marker); }
  for (const forbiddenKey of ['notebook', 'protagonist', 'email', 'birth_date', 'age_range', 'version']) {
    assert.ok(!text.includes('"' + forbiddenKey + '"'), forbiddenKey);
  }
});

// ---- 持久化與帳號隔離 ------------------------------------------------------------------------------------

test('data written through IndexedDB survives closing and reopening the same database', async () => {
  const factory = fakeIndexedDB();
  const name = Store.databaseName('user-1');
  const first = await Store.openStore({ indexedDB: factory, name, keyRange });
  await first.putSession(SESSION, snapshot());
  await first.putTurns(SESSION, [turn(1), turn(2)]);
  await first.setInflight(SESSION, { client_msg_id: 'id-3', action: '還沒送完' });
  first.close();
  const second = await Store.openStore({ indexedDB: factory, name, keyRange });
  assert.equal(second.mode, 'indexeddb');
  assert.equal((await second.getSession(SESSION)).title, '雨夜歸人');
  assert.equal(await second.maxSeq(SESSION), 2);
  assert.equal((await second.getInflight(SESSION)).action, '還沒送完');
});

test('different users (different database names) never see each others cache', async () => {
  const factory = fakeIndexedDB();
  const alice = await Store.openStore({ indexedDB: factory, name: Store.databaseName('alice'), keyRange });
  const bob = await Store.openStore({ indexedDB: factory, name: Store.databaseName('bob'), keyRange });
  await alice.putSession(SESSION, snapshot());
  await alice.putTurns(SESSION, [turn(1)]);
  await alice.setInflight(SESSION, { client_msg_id: 'a', action: '私人' });
  assert.equal(await bob.getSession(SESSION), null);
  assert.deepEqual(await bob.turnsOf(SESSION), []);
  assert.equal(await bob.getInflight(SESSION), null);
  assert.deepEqual([...factory.databases.keys()].sort(), ['game-cache-alice', 'game-cache-bob']);
});

test('the database name carries the public id only when it is a plain identifier', () => {
  assert.equal(Store.databaseName('9c2f6d1e-0000-4000-8000-000000000001'), 'game-cache-9c2f6d1e-0000-4000-8000-000000000001');
  for (const bad of ['', undefined, null, 5, 'a b', 'a/b', 'a|b', 'x'.repeat(65), '使用者']) { assert.equal(Store.databaseName(bad), null, String(bad)); }
});

test('without a database name the store uses memory only and never opens a shared database', async () => {
  const factory = fakeIndexedDB();
  const reasons = [];
  const store = await Store.openStore({ indexedDB: factory, keyRange, onFallback: (error) => reasons.push(error) });
  assert.equal(store.mode, 'memory');
  assert.equal(reasons.length, 1);
  assert.equal(factory.databases.size, 0);
  await store.putTurns(SESSION, [turn(1)]);
  assert.equal(await store.maxSeq(SESSION), 1);
});

// ---- 暫存不可用 ----------------------------------------------------------------------------------------

const failures = {
  'there is no indexedDB at all': { indexedDB: null },
  'indexedDB is undefined': { indexedDB: undefined },
  'open() throws': { indexedDB: { open() { throw new Error('SecurityError'); } } },
  'the open request errors (private browsing)': { indexedDB: fakeIndexedDB({ errorOnOpen: true }) },
  'the open request is blocked': { indexedDB: fakeIndexedDB({ blocked: true }) },
  'the open request never answers': { indexedDB: fakeIndexedDB({ neverOpens: true }), openTimeoutMs: 20 }
};

for (const [label, options] of Object.entries(failures)) {
  test(`when ${label} the store falls back to memory and still works`, async () => {
    const reasons = [];
    const store = await Store.openStore({ name: 'game-cache-x', keyRange, onFallback: (error) => reasons.push(error), ...options });
    assert.equal(store.mode, 'memory');
    assert.equal(reasons.length, 1);
    await store.putSession(SESSION, snapshot());
    await store.putTurns(SESSION, [turn(1)]);
    await store.setInflight(SESSION, { client_msg_id: 'p', action: '仍可使用' });
    assert.equal((await store.getSession(SESSION)).title, '雨夜歸人');
    assert.equal(await store.maxSeq(SESSION), 1);
    assert.equal((await store.getInflight(SESSION)).action, '仍可使用');
  });
}

test('when a write fails after opening (quota), the store switches to memory and the operation still succeeds without an error', async () => {
  const reasons = [];
  const store = await Store.openStore({ indexedDB: fakeIndexedDB({ failWrites: true }), name: 'game-cache-q', keyRange, onFallback: (error) => reasons.push(error) });
  assert.equal(store.mode, 'indexeddb');
  await store.putSession(SESSION, snapshot());      // 這次寫入失敗 → 退回記憶體後重做
  assert.equal(store.mode, 'memory');
  assert.equal(reasons.length, 1);
  assert.equal((await store.getSession(SESSION)).title, '雨夜歸人');
});

test('when the connection throws synchronously on every transaction (closed database), the store still falls back instead of throwing', async () => {
  const reasons = [];
  const store = await Store.openStore({ indexedDB: fakeIndexedDB({ transactionThrows: true }), name: 'game-cache-t', keyRange, onFallback: (error) => reasons.push(error) });
  assert.equal(store.mode, 'indexeddb');
  await assert.doesNotReject(store.putTurns(SESSION, [turn(1)]));
  assert.equal(store.mode, 'memory');
  assert.equal(await store.maxSeq(SESSION), 1);
  assert.equal(reasons.length, 1);
});

test('a failing fallback callback never breaks the store', async () => {
  const store = await Store.openStore({ indexedDB: null, name: 'game-cache-x', onFallback: () => { throw new Error('回報失敗'); } });
  assert.equal(store.mode, 'memory');
  await store.putTurns(SESSION, [turn(1)]);
  assert.equal(await store.maxSeq(SESSION), 1);
});

test('without IDBKeyRange a prefix read cannot be answered by IndexedDB, so it falls back instead of mixing sessions', async () => {
  const store = await Store.openStore({ indexedDB: fakeIndexedDB(), name: 'game-cache-k', keyRange: null });
  await store.putTurns('abc', [turn(1)]);
  await store.putTurns('abcd', [turn(1, { story: '別的存檔' })]);
  const read = await store.turnsOf('abc');
  assert.equal(store.mode, 'memory');                               // 讀取失敗 → 本次改用記憶體（快取不搬移，所以是空的）
  assert.ok(read.every((t) => t.story !== '別的存檔'));              // 無論如何都不會混入別的存檔
});

test('a browser that throws when the indexedDB global is touched still gets a working memory store', async () => {
  Object.defineProperty(globalThis, 'indexedDB', { configurable: true, get() { throw new Error('SecurityError'); } });
  try {
    const store = await Store.openStore({ name: 'game-cache-blocked', keyRange });
    assert.equal(store.mode, 'memory');
    await store.putTurns(SESSION, [turn(1)]);
    assert.equal(await store.maxSeq(SESSION), 1);
  } finally {
    delete globalThis.indexedDB;
  }
});

test('the memory backend is isolated per store instance', async () => {
  const one = Store.createStore(Store.createMemoryBackend());
  const two = Store.createStore(Store.createMemoryBackend());
  await one.putTurns(SESSION, [turn(1)]);
  assert.deepEqual(await two.turnsOf(SESSION), []);
});

test('keys sort so that sequence order equals text order', () => {
  const keys = [1, 2, 9, 10, 11, 100].map((seq) => Store.turnKey('c', seq));
  assert.deepEqual([...keys].sort(), keys);
});
