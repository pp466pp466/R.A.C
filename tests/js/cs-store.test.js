// 客服本機暫存的單元測試：記憶體後端、一個最小的假 IndexedDB，以及開啟失敗時退回記憶體。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Store = require('../../app/static/js/cs/cs-store.js');

// ---- 最小的假 IndexedDB：只實作 cs-store.js 用到的部分（非同步回呼、依 key 排序的 getAll、交易完成事件）-----

const keyRange = { bound: (lower, upper) => ({ lower, upper }) };

function fakeIndexedDB(behaviour) {
  behaviour = behaviour || {};
  const databases = new Map();
  return {
    databases,
    open(name) {
      const request = {};
      setTimeout(() => {
        if (behaviour.neverOpens) { return; }
        if (behaviour.errorOnOpen) { request.error = new Error('denied'); request.onerror && request.onerror(); return; }
        if (behaviour.blocked) { request.onblocked && request.onblocked(); return; }
        let record = databases.get(name);
        const isNew = !record;
        if (isNew) { record = { stores: new Map() }; databases.set(name, record); }
        const db = {
          closed: false,
          objectStoreNames: { contains: (store) => record.stores.has(store) },
          createObjectStore(store) { record.stores.set(store, new Map()); },
          close() { db.closed = true; },
          transaction(storeName) {
            const transaction = {};
            let scheduled = false;
            const finish = () => {
              if (scheduled) { return; }
              scheduled = true;
              setTimeout(() => {
                if (behaviour.failWrites && transaction.wrote) {
                  transaction.error = new Error('QuotaExceededError');
                  transaction.onabort && transaction.onabort();
                } else {
                  transaction.oncomplete && transaction.oncomplete();
                }
              }, 0);
            };
            const map = record.stores.get(storeName);
            const reply = (value) => {
              const req = {};
              setTimeout(() => { req.result = value; req.onsuccess && req.onsuccess(); }, 0);
              return req;
            };
            transaction.objectStore = () => ({
              get: (key) => { finish(); return reply(map.get(key)); },
              put: (value, key) => { transaction.wrote = true; if (!behaviour.failWrites) { map.set(key, JSON.parse(JSON.stringify(value))); } finish(); return reply(undefined); },
              delete: (key) => { transaction.wrote = true; map.delete(key); finish(); return reply(undefined); },
              clear: () => { transaction.wrote = true; map.clear(); finish(); return reply(undefined); },
              getAll: (range) => {
                finish();
                const keys = [...map.keys()].filter((key) => !range || (key >= range.lower && key <= range.upper)).sort();
                return reply(keys.map((key) => JSON.parse(JSON.stringify(map.get(key)))));
              }
            });
            return transaction;
          }
        };
        request.result = db;
        if (isNew) { request.onupgradeneeded && request.onupgradeneeded(); }
        request.onsuccess && request.onsuccess();
      }, 0);
      return request;
    }
  };
}

const backends = {
  memory: () => Promise.resolve(Store.createStore(Store.createMemoryBackend())),
  indexeddb: () => Store.openStore({ indexedDB: fakeIndexedDB(), name: 'cs-test', keyRange })
};

function conversation(key, updatedAt, extra) {
  return Object.assign({ key, id: key, title: '對話 ' + key, state_version: 1, updated_at: updatedAt, created_at: updatedAt }, extra || {});
}

function message(seq, extra) {
  return Object.assign({ seq, client_msg_id: 'm' + seq, role: seq % 2 ? 'user' : 'assistant', content: '內容' + seq, msg_status: 'ok', suggested_actions: [], created_at: '2026-10-29T00:00:00.000+00:00' }, extra || {});
}

for (const [name, open] of Object.entries(backends)) {
  test(`[${name}] the store reports which backend it uses`, async () => {
    const store = await open();
    assert.equal(store.mode, name);
  });

  test(`[${name}] conversations are saved, read back and listed newest activity first`, async () => {
    const store = await open();
    await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
    await store.saveConversation(conversation('b', '2026-10-29T12:00:00.000+00:00'));
    await store.saveConversation(conversation('c', '2026-10-29T11:00:00.000+00:00'));
    assert.deepEqual((await store.listConversations()).map((c) => c.key), ['b', 'c', 'a']);
    assert.equal((await store.getConversation('c')).title, '對話 c');
    assert.equal(await store.getConversation('missing'), undefined);
  });

  test(`[${name}] messages come back ordered by numeric sequence, not by text`, async () => {
    const store = await open();
    await store.upsertMessages('a', [message(10), message(2), message(1), message(100), message(3)]);
    assert.deepEqual((await store.getMessages('a')).map((m) => m.seq), [1, 2, 3, 10, 100]);
  });

  test(`[${name}] messages of one conversation never leak into another whose key is a prefix`, async () => {
    const store = await open();
    await store.upsertMessages('abc', [message(1)]);
    await store.upsertMessages('abcd', [message(1, { content: '別的對話' })]);
    assert.deepEqual((await store.getMessages('abc')).map((m) => m.content), ['內容1']);
    assert.deepEqual((await store.getMessages('abcd')).map((m) => m.content), ['別的對話']);
  });

  test(`[${name}] the backend wins: an upsert overwrites a cached message with the same sequence`, async () => {
    const store = await open();
    await store.upsertMessages('a', [message(1, { content: '本機舊的' }), message(2)]);
    await store.upsertMessages('a', [message(1, { content: '後端的' })]);
    const cached = await store.getMessages('a');
    assert.deepEqual(cached.map((m) => m.content), ['後端的', '內容2']);
  });

  test(`[${name}] replacing messages drops cached ones the backend no longer has`, async () => {
    const store = await open();
    await store.upsertMessages('a', [message(1), message(2), message(3)]);
    await store.replaceMessages('a', [message(1), message(2)]);
    assert.deepEqual((await store.getMessages('a')).map((m) => m.seq), [1, 2]);
  });

  test(`[${name}] pending messages are kept in creation order and carry their original ids`, async () => {
    const store = await open();
    await store.addPending({ client_msg_id: 'id-1', conversation_key: 'a', content: '第一則' });
    await store.addPending({ client_msg_id: 'id-2', conversation_key: 'b', content: '另一個對話' });
    await store.addPending({ client_msg_id: 'id-3', conversation_key: 'a', content: '第二則' });
    assert.deepEqual((await store.listPending()).map((p) => p.client_msg_id), ['id-1', 'id-2', 'id-3']);
    assert.deepEqual((await store.listPending('a')).map((p) => p.content), ['第一則', '第二則']);
    const first = await store.findPending('id-1');
    assert.deepEqual({ status: first.status, attempts: first.attempts, error: first.error }, { status: 'queued', attempts: 0, error: null });
  });

  test(`[${name}] a pending message can be updated and removed`, async () => {
    const store = await open();
    await store.addPending({ client_msg_id: 'id-1', conversation_key: 'a', content: '內容' });
    const updated = await store.updatePending('id-1', { status: 'failed', attempts: 2, error: 'conversation_busy' });
    assert.deepEqual({ status: updated.status, attempts: updated.attempts, error: updated.error }, { status: 'failed', attempts: 2, error: 'conversation_busy' });
    assert.equal((await store.findPending('id-1')).status, 'failed');
    assert.equal(await store.updatePending('nope', { status: 'x' }), null);
    await store.removePending('id-1');
    assert.deepEqual(await store.listPending(), []);
    await assert.doesNotReject(store.removePending('id-1'));
  });

  test(`[${name}] removing a conversation removes its messages and pending messages and nothing else`, async () => {
    const store = await open();
    await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
    await store.saveConversation(conversation('b', '2026-10-29T11:00:00.000+00:00'));
    await store.upsertMessages('a', [message(1), message(2)]);
    await store.upsertMessages('b', [message(1)]);
    await store.addPending({ client_msg_id: 'pa', conversation_key: 'a', content: 'x' });
    await store.addPending({ client_msg_id: 'pb', conversation_key: 'b', content: 'y' });
    await store.removeConversation('a');
    assert.deepEqual((await store.listConversations()).map((c) => c.key), ['b']);
    assert.deepEqual(await store.getMessages('a'), []);
    assert.equal((await store.getMessages('b')).length, 1);
    assert.deepEqual((await store.listPending()).map((p) => p.client_msg_id), ['pb']);
  });

  test(`[${name}] merging the server list overwrites synced conversations, drops vanished ones and keeps offline-created ones`, async () => {
    const store = await open();
    await store.saveConversation(conversation('keep', '2026-10-29T10:00:00.000+00:00', { title: '本機舊標題' }));
    await store.saveConversation(conversation('vanished', '2026-10-29T10:00:00.000+00:00'));
    await store.saveConversation(conversation(Store.localConversationKey('c1'), '2026-10-29T09:00:00.000+00:00', { id: null, client_id: 'c1' }));
    await store.upsertMessages('vanished', [message(1)]);
    const removed = await store.mergeServerConversations([
      conversation('keep', '2026-10-29T12:00:00.000+00:00', { title: '後端新標題', state_version: 3 }),
      conversation('fresh', '2026-10-29T11:00:00.000+00:00')
    ]);
    assert.deepEqual(removed, ['vanished']);
    const keys = (await store.listConversations()).map((c) => c.key);
    assert.deepEqual(keys, ['keep', 'fresh', 'new:c1']);
    const kept = await store.getConversation('keep');
    assert.deepEqual({ title: kept.title, version: kept.state_version }, { title: '後端新標題', version: 3 });
    assert.deepEqual(await store.getMessages('vanished'), []);
  });

  test(`[${name}] a conversation created offline is re-keyed once the server created it, taking its pending messages along`, async () => {
    const store = await open();
    const localKey = Store.localConversationKey('c1');
    await store.saveConversation(conversation(localKey, '2026-10-29T09:00:00.000+00:00', { id: null, client_id: 'c1' }));
    await store.addPending({ client_msg_id: 'p1', conversation_key: localKey, content: '離線時輸入' });
    await store.rekeyConversation(localKey, conversation('server-id', '2026-10-29T09:05:00.000+00:00', { client_id: 'c1' }));
    assert.deepEqual((await store.listConversations()).map((c) => c.key), ['server-id']);
    assert.deepEqual((await store.listPending('server-id')).map((p) => p.client_msg_id), ['p1']);
    assert.deepEqual(await store.listPending(localKey), []);
  });

  test(`[${name}] clearAll empties everything`, async () => {
    const store = await open();
    await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
    await store.upsertMessages('a', [message(1)]);
    await store.addPending({ client_msg_id: 'p', conversation_key: 'a', content: 'x' });
    await store.clearAll();
    assert.deepEqual([(await store.listConversations()).length, (await store.getMessages('a')).length, (await store.listPending()).length], [0, 0, 0]);
  });
}

test('data written through IndexedDB survives closing and reopening the same database', async () => {
  const factory = fakeIndexedDB();
  const first = await Store.openStore({ indexedDB: factory, name: 'cs-user-1', keyRange });
  await first.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
  await first.upsertMessages('a', [message(1), message(2)]);
  await first.addPending({ client_msg_id: 'p', conversation_key: 'a', content: '還沒送出' });
  first.close();
  const second = await Store.openStore({ indexedDB: factory, name: 'cs-user-1', keyRange });
  assert.equal(second.mode, 'indexeddb');
  assert.equal((await second.getMessages('a')).length, 2);
  assert.deepEqual((await second.listPending()).map((p) => p.content), ['還沒送出']);
});

test('the pending counter keeps increasing after a reopen so the order stays right', async () => {
  const factory = fakeIndexedDB();
  const first = await Store.openStore({ indexedDB: factory, name: 'cs-user-1', keyRange });
  await first.addPending({ client_msg_id: 'p1', conversation_key: 'a', content: '1' });
  first.close();
  const second = await Store.openStore({ indexedDB: factory, name: 'cs-user-1', keyRange });
  await second.addPending({ client_msg_id: 'p2', conversation_key: 'a', content: '2' });
  assert.deepEqual((await second.listPending()).map((p) => p.client_msg_id), ['p1', 'p2']);
});

test('different users (different database names) never see each others cache', async () => {
  const factory = fakeIndexedDB();
  const alice = await Store.openStore({ indexedDB: factory, name: 'cs-alice', keyRange });
  const bob = await Store.openStore({ indexedDB: factory, name: 'cs-bob', keyRange });
  await alice.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
  await alice.addPending({ client_msg_id: 'p', conversation_key: 'a', content: '私人' });
  assert.deepEqual(await bob.listConversations(), []);
  assert.deepEqual(await bob.listPending(), []);
});

const failures = {
  'there is no indexedDB at all': { indexedDB: null },
  'indexedDB is undefined': { indexedDB: undefined, global: true },
  'open() throws': { indexedDB: { open() { throw new Error('SecurityError'); } } },
  'the open request errors (private browsing)': { indexedDB: fakeIndexedDB({ errorOnOpen: true }) },
  'the open request is blocked': { indexedDB: fakeIndexedDB({ blocked: true }) },
  'the open request never answers': { indexedDB: fakeIndexedDB({ neverOpens: true }), openTimeoutMs: 20 }
};

for (const [label, options] of Object.entries(failures)) {
  test(`when ${label} the store falls back to memory and still works`, async () => {
    const reasons = [];
    const store = await Store.openStore({ name: 'x', keyRange, onFallback: (error) => reasons.push(error), ...options });
    assert.equal(store.mode, 'memory');
    assert.equal(reasons.length, 1);
    await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
    await store.addPending({ client_msg_id: 'p', conversation_key: 'a', content: '仍可使用' });
    assert.equal((await store.listConversations()).length, 1);
    assert.equal((await store.listPending()).length, 1);
  });
}

test('when a write fails after opening (quota), the store switches to memory and the operation still succeeds', async () => {
  const reasons = [];
  const store = await Store.openStore({ indexedDB: fakeIndexedDB({ failWrites: true }), name: 'q', keyRange, onFallback: (error) => reasons.push(error) });
  assert.equal(store.mode, 'indexeddb');
  await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));     // 這次寫入失敗 → 退回記憶體後重做
  assert.equal(store.mode, 'memory');
  assert.equal(reasons.length, 1);
  assert.equal((await store.getConversation('a')).key, 'a');
});

test('without IDBKeyRange a prefix read cannot be answered by IndexedDB, so it falls back instead of mixing conversations', async () => {
  const store = await Store.openStore({ indexedDB: fakeIndexedDB(), name: 'k', keyRange: null });
  await store.upsertMessages('a', [message(1)]);
  await store.upsertMessages('abcd', [message(1, { content: '別的對話' })]);
  const read = await store.getMessages('a');
  assert.equal(store.mode, 'memory');                               // 讀取失敗 → 本次改用記憶體（快取不搬移，所以是空的）
  assert.ok(read.every((m) => m.content !== '別的對話'));            // 無論如何都不會混入別的對話
});

test('a browser that throws when the indexedDB global is touched still gets a working memory store', async () => {
  Object.defineProperty(globalThis, 'indexedDB', { configurable: true, get() { throw new Error('SecurityError'); } });
  try {
    const store = await Store.openStore({ name: 'blocked', keyRange });
    assert.equal(store.mode, 'memory');
    await store.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
    assert.equal((await store.listConversations()).length, 1);
  } finally {
    delete globalThis.indexedDB;
  }
});

test('the memory backend is isolated per store instance', async () => {
  const one = Store.createStore(Store.createMemoryBackend());
  const two = Store.createStore(Store.createMemoryBackend());
  await one.saveConversation(conversation('a', '2026-10-29T10:00:00.000+00:00'));
  assert.deepEqual(await two.listConversations(), []);
});

test('keys sort so that sequence order equals text order', () => {
  const keys = [1, 2, 9, 10, 11, 100].map((seq) => Store.messageKey('c', seq));
  assert.deepEqual([...keys].sort(), keys);
  assert.equal(Store.isLocalConversationKey(Store.localConversationKey('x')), true);
  assert.equal(Store.isLocalConversationKey('3f2a'), false);
});
