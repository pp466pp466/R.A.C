// 遊戲頁同步引擎的單元測試（rpg-game-ui 2.2）：以替身 API 與兩種儲存後端（記憶體、假 IndexedDB）驗證 catchUp、resolveInflight、remember。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Store = require('../../app/static/js/game/game-store.js');
const Sync = require('../../app/static/js/game/game-sync.js');
const { fakeIndexedDB, keyRange } = require('./support/fake-indexeddb.js');

const SESSION = '99999999-9999-4999-8999-999999999999';
const OTHER = '88888888-8888-4888-8888-888888888888';

const backends = {
  memory: () => Promise.resolve(Store.createStore(Store.createMemoryBackend())),
  indexeddb: () => Store.openStore({ indexedDB: fakeIndexedDB(), name: 'game-cache-test', keyRange })
};

function turn(seq, extra) {
  return Object.assign({ seq, client_msg_id: 'm' + seq, turn_number: seq, status: 'complete', action: '行動' + seq, story: '劇情' + seq }, extra || {});
}

const clone = (value) => JSON.parse(JSON.stringify(value));

/* 替身 API：只有 `turns`。任何其他成員被存取都會丟出例外——證明同步引擎從不碰串流或其他端點。
   server：後端目前的回合（可在測試中改）；pageSize：每頁筆數；ignoreSince：忽略 since_seq（模擬後端回覆較舊的序號）；
   fail：第 n 次呼叫（從 1 起算）失敗時回傳的錯誤，`(n) => error | null`。 */
function fakeApi(server, opts) {
  opts = opts || {};
  const calls = [];
  const real = {
    turns(id, sinceSeq, options) {
      calls.push({ id, since: sinceSeq, options });
      if (opts.throwSync) { throw new Error('同步丟出的例外'); }
      const failure = opts.fail ? opts.fail(calls.length) : null;
      if (failure) { return Promise.reject(failure); }
      const matching = server.filter((item) => opts.ignoreSince || item.seq > sinceSeq);
      const page = matching.slice(0, opts.pageSize || 1000);
      return Promise.resolve({ turns: clone(page), has_more: matching.length > page.length });
    }
  };
  const api = new Proxy(real, {
    get(target, key) {
      if (key === 'calls') { return calls; }
      if (key === 'then') { return undefined; }
      if (!(key in target)) { throw new Error('同步引擎不該呼叫 api.' + String(key)); }
      return target[key];
    }
  });
  return api;
}

const networkError = () => ({ kind: 'network', status: 0, code: 'network', message: '無法連線' });
const timeoutError = () => ({ kind: 'timeout', status: 0, code: 'timeout', message: '逾時' });
const serverFailure = (status, code) => ({ kind: 'server', status, code: code || '', message: '失敗' });

async function seeded(open, seqs) {
  const store = await open();
  await store.putTurns(SESSION, seqs.map((seq) => turn(seq)));
  return store;
}

for (const [name, open] of Object.entries(backends)) {
  test(`[${name}] catching up asks for since_seq equal to the local maximum and adds only the new turns, in sequence order`, async () => {
    const store = await seeded(open, Array.from({ length: 40 }, (_, index) => index + 1));
    const api = fakeApi([...Array.from({ length: 42 }, (_, index) => turn(index + 1))]);
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(api.calls.length, 1);
    assert.equal(api.calls[0].since, 40);
    assert.equal(api.calls[0].id, SESSION);
    assert.deepEqual({ status: result.status, changed: result.changed, added: result.added }, { status: 'ok', changed: true, added: 2 });
    assert.deepEqual(result.turns.map((t) => t.seq), Array.from({ length: 42 }, (_, index) => index + 1));
    assert.equal(await store.maxSeq(SESSION), 42);
  });

  test(`[${name}] with an empty cache the first catch-up starts from since_seq 0`, async () => {
    const store = await open();
    const api = fakeApi([turn(1), turn(2)]);
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(api.calls[0].since, 0);
    assert.deepEqual(result.turns.map((t) => t.seq), [1, 2]);
  });

  test(`[${name}] pages are followed while has_more is true, each continuing after the last sequence received`, async () => {
    const store = await open();
    const api = fakeApi([1, 2, 3, 4, 5].map((seq) => turn(seq)), { pageSize: 2 });
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.deepEqual(api.calls.map((call) => call.since), [0, 2, 4]);
    assert.deepEqual(result.turns.map((t) => t.seq), [1, 2, 3, 4, 5]);
    assert.equal(result.added, 5);
    assert.equal(await store.maxSeq(SESSION), 5);
  });

  test(`[${name}] a server that keeps claiming has_more without moving forward cannot loop the client`, async () => {
    const store = await seeded(open, [1, 2]);
    const calls = [];
    const api = { turns: (id, since) => { calls.push(since); return Promise.resolve({ turns: [], has_more: true }); } };
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(calls.length, 1);
    assert.equal(result.status, 'ok');
    const stuck = { turns: (id, since) => { calls.push(since); return Promise.resolve({ turns: [turn(2)], has_more: true }); } };
    calls.length = 0;
    await Sync.createSync({ api: stuck, store }).catchUp(SESSION);
    assert.equal(calls.length, 1);
  });

  test(`[${name}] when nothing is new the result says nothing changed and the cache is untouched`, async () => {
    const store = await seeded(open, [1, 2, 3]);
    const result = await Sync.createSync({ api: fakeApi([turn(1), turn(2), turn(3)]), store }).catchUp(SESSION);
    assert.deepEqual({ status: result.status, changed: result.changed, added: result.added }, { status: 'ok', changed: false, added: 0 });
    assert.deepEqual(result.turns.map((t) => t.seq), [1, 2, 3]);
  });

  test(`[${name}] the backend wins: a turn with the same sequence replaces the cached content and the display shows the backend's text`, async () => {
    const store = await open();
    await store.putTurns(SESSION, [turn(1), turn(2, { story: '本機舊的劇情' })]);
    const api = fakeApi([turn(1), turn(2, { story: '後端的劇情' })], { ignoreSince: true });
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(result.changed, true);
    assert.equal(result.added, 0);
    assert.equal(result.turns[1].story, '後端的劇情');
    assert.equal((await store.turnsOf(SESSION))[1].story, '後端的劇情');
  });

  test(`[${name}] a blocked turn leaves no text in the cache or in the displayed turns, even if the response carried some`, async () => {
    const store = await open();
    const api = fakeApi([turn(1), turn(2, { status: 'blocked', action: '被擋的行動', story: '被擋的劇情' }), turn(3, { status: 'interrupted', action: '中斷的行動', story: '中斷前的片段' })]);
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    const shown = JSON.stringify(result.turns);
    const cached = JSON.stringify(await store.turnsOf(SESSION));
    for (const text of [shown, cached]) { assert.ok(!text.includes('被擋') && !text.includes('中斷')); }
    assert.deepEqual(result.turns.map((t) => t.status), ['complete', 'blocked', 'interrupted']);
  });

  test(`[${name}] a turn still being generated is shown but not cached, so the next catch-up picks up its final content`, async () => {
    const store = await open();
    const server = [turn(1), turn(2, { status: 'streaming', action: null, story: null })];
    const sync = Sync.createSync({ api: fakeApi(server), store });
    const first = await sync.catchUp(SESSION);
    assert.deepEqual(first.turns.map((t) => [t.seq, t.status]), [[1, 'complete'], [2, 'streaming']]);
    assert.equal(first.added, 1);
    assert.equal(await store.maxSeq(SESSION), 1);
    server[1] = turn(2);                                    // 生成完成
    const api = fakeApi(server);
    const second = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(api.calls[0].since, 1);                    // 從已定稿的最大序號繼續，所以拿得到 2 的最終內容
    assert.deepEqual(second.turns.map((t) => [t.seq, t.status, t.story]), [[1, 'complete', '劇情1'], [2, 'complete', '劇情2']]);
    assert.equal(second.added, 1);
  });

  test(`[${name}] offline (network failure or timeout): no throw, status offline, the cache is unchanged and still readable`, async () => {
    for (const failure of [networkError, timeoutError]) {
      const store = await seeded(open, [1, 2, 3]);
      const result = await Sync.createSync({ api: fakeApi([], { fail: () => failure() }), store }).catchUp(SESSION);
      assert.deepEqual({ status: result.status, changed: result.changed, added: result.added }, { status: 'offline', changed: false, added: 0 });
      assert.deepEqual(result.turns.map((t) => t.seq), [1, 2, 3]);
      assert.equal(await store.maxSeq(SESSION), 3);
    }
  });

  test(`[${name}] a failure after the first page keeps what was fetched so far`, async () => {
    const store = await open();
    const api = fakeApi([1, 2, 3, 4].map((seq) => turn(seq)), { pageSize: 2, fail: (n) => (n === 2 ? networkError() : null) });
    const result = await Sync.createSync({ api, store }).catchUp(SESSION);
    assert.equal(result.status, 'offline');
    assert.deepEqual(result.turns.map((t) => t.seq), [1, 2]);
    assert.equal(result.added, 2);
    assert.equal(await store.maxSeq(SESSION), 2);
  });

  test(`[${name}] an aborted request is reported as canceled and the signal reaches the API`, async () => {
    const store = await seeded(open, [1]);
    const controller = new AbortController();
    const api = fakeApi([], { fail: () => ({ kind: 'canceled', status: 0, code: 'canceled', message: '' }) });
    const result = await Sync.createSync({ api, store }).catchUp(SESSION, { signal: controller.signal });
    assert.equal(result.status, 'canceled');
    assert.equal(api.calls[0].options.signal, controller.signal);
    assert.deepEqual(result.turns.map((t) => t.seq), [1]);
  });

  test(`[${name}] other failures are reported with the error so the screen can describe them, and never thrown`, async () => {
    for (const failure of [serverFailure(401, 'login_required'), serverFailure(403, 'age_confirmation_required'), serverFailure(503, 'unavailable')]) {
      const store = await seeded(open, [1]);
      const result = await Sync.createSync({ api: fakeApi([], { fail: () => failure }), store }).catchUp(SESSION);
      assert.equal(result.status, 'failed');
      assert.equal(result.error, failure);
      assert.deepEqual(result.turns.map((t) => t.seq), [1]);
    }
  });

  test(`[${name}] an API that throws synchronously is reported as a failure, not an exception`, async () => {
    const store = await seeded(open, [1]);
    const result = await Sync.createSync({ api: fakeApi([], { throwSync: true }), store }).catchUp(SESSION);
    assert.equal(result.status, 'failed');
    assert.deepEqual(result.turns.map((t) => t.seq), [1]);
  });

  test(`[${name}] a 404 means the save is gone: its cache and in-flight turn are cleared and other saves are untouched`, async () => {
    const store = await seeded(open, [1, 2]);
    await store.putSession(SESSION, { title: '會被清掉' });
    await store.setInflight(SESSION, { client_msg_id: 'x', action: '行動' });
    await store.putTurns(OTHER, [turn(1, { story: '別的存檔' })]);
    await store.putSession(OTHER, { title: '別的存檔' });
    const result = await Sync.createSync({ api: fakeApi([], { fail: () => serverFailure(404, 'not_found') }), store }).catchUp(SESSION);
    assert.equal(result.status, 'gone');
    assert.deepEqual(result.turns, []);
    assert.equal(await store.getSession(SESSION), null);
    assert.deepEqual(await store.turnsOf(SESSION), []);
    assert.equal(await store.getInflight(SESSION), null);
    assert.equal((await store.getSession(OTHER)).title, '別的存檔');
    assert.deepEqual((await store.turnsOf(OTHER)).map((t) => t.story), ['別的存檔']);
  });

  test(`[${name}] two catch-ups running at the same time leave one consistent cache`, async () => {
    const store = await open();
    const server = [1, 2, 3].map((seq) => turn(seq));
    const sync = Sync.createSync({ api: fakeApi(server, { pageSize: 2 }), store });
    const [a, b] = await Promise.all([sync.catchUp(SESSION), sync.catchUp(SESSION)]);
    assert.deepEqual(a.turns.map((t) => t.seq), [1, 2, 3]);
    assert.deepEqual(b.turns.map((t) => t.seq), [1, 2, 3]);
    assert.deepEqual((await store.turnsOf(SESSION)).map((t) => t.seq), [1, 2, 3]);
  });

  // ---- 在途回合 ----------------------------------------------------------------------------------------

  test(`[${name}] without an in-flight turn resolveInflight returns null and does not even ask the server`, async () => {
    const store = await seeded(open, [1]);
    const api = fakeApi([turn(1)]);
    assert.equal(await Sync.createSync({ api, store }).resolveInflight(SESSION), null);
    assert.equal(api.calls.length, 0);
  });

  test(`[${name}] in-flight turn the backend already completed: catch-up finds it, the record is removed and there is no resend prompt`, async () => {
    const store = await seeded(open, [1, 2]);
    await store.setInflight(SESSION, { client_msg_id: 'm3', action: '串流中途重新整理' });
    const api = fakeApi([turn(1), turn(2), turn(3)]);
    const outcome = await Sync.createSync({ api, store }).resolveInflight(SESSION);
    assert.equal(outcome, null);
    assert.equal(api.calls[0].since, 2);
    assert.equal(await store.getInflight(SESSION), null);
    assert.equal(await store.maxSeq(SESSION), 3);
  });

  test(`[${name}] in-flight turn the backend never completed: the record is returned for resend or abandon and stays stored`, async () => {
    const store = await seeded(open, [1, 2]);
    const record = await store.setInflight(SESSION, { client_msg_id: 'm3', action: '推開那扇門', created_at: '2026-11-11T10:00:00.000Z' });
    const outcome = await Sync.createSync({ api: fakeApi([turn(1), turn(2)]), store }).resolveInflight(SESSION);
    assert.deepEqual(outcome, record);
    assert.deepEqual(await store.getInflight(SESSION), record);
  });

  test(`[${name}] a blocked, interrupted or still-streaming turn with the same id does not count as completed`, async () => {
    for (const status of ['blocked', 'interrupted', 'streaming']) {
      const store = await seeded(open, [1]);
      await store.setInflight(SESSION, { client_msg_id: 'm2', action: '行動' });
      const server = [turn(1), turn(2, { status })];
      const outcome = await Sync.createSync({ api: fakeApi(server), store }).resolveInflight(SESSION);
      assert.equal(outcome && outcome.client_msg_id, 'm2', status);
      assert.equal((await store.getInflight(SESSION)).client_msg_id, 'm2', status);
    }
  });

  test(`[${name}] when the catch-up fails the in-flight record is kept (offline, timeout, server error)`, async () => {
    for (const failure of [networkError(), timeoutError(), serverFailure(503, 'unavailable')]) {
      const store = await seeded(open, [1]);
      await store.setInflight(SESSION, { client_msg_id: 'm2', action: '行動' });
      const outcome = await Sync.createSync({ api: fakeApi([], { fail: () => failure }), store }).resolveInflight(SESSION);
      assert.equal(outcome && outcome.client_msg_id, 'm2');
      assert.equal((await store.getInflight(SESSION)).client_msg_id, 'm2');
    }
  });

  test(`[${name}] offline, but the cache already holds the completed turn: the record is removed without needing the server`, async () => {
    const store = await seeded(open, [1, 2, 3]);
    await store.setInflight(SESSION, { client_msg_id: 'm3', action: '行動' });
    const outcome = await Sync.createSync({ api: fakeApi([], { fail: () => networkError() }), store }).resolveInflight(SESSION);
    assert.equal(outcome, null);
    assert.equal(await store.getInflight(SESSION), null);
  });

  test(`[${name}] when the save no longer exists resolveInflight returns null and everything about it is cleared`, async () => {
    const store = await seeded(open, [1]);
    await store.setInflight(SESSION, { client_msg_id: 'm2', action: '行動' });
    const outcome = await Sync.createSync({ api: fakeApi([], { fail: () => serverFailure(404, 'not_found') }), store }).resolveInflight(SESSION);
    assert.equal(outcome, null);
    assert.equal(await store.getInflight(SESSION), null);
    assert.deepEqual(await store.turnsOf(SESSION), []);
  });

  test(`[${name}] a catch-up result the caller just obtained can be handed over so the server is not asked a second time`, async () => {
    const store = await seeded(open, [1, 2]);
    await store.setInflight(SESSION, { client_msg_id: 'm3', action: '行動' });
    const api = fakeApi([turn(1), turn(2), turn(3)]);
    const sync = Sync.createSync({ api, store });
    const result = await sync.catchUp(SESSION);
    assert.equal(api.calls.length, 1);
    assert.equal(await sync.resolveInflight(SESSION, { result }), null);           // 結果裡已有完整的第 3 輪：移除，不再請求
    assert.equal(api.calls.length, 1);
    assert.equal(await store.getInflight(SESSION), null);
  });

  test(`[${name}] a handed-over result without the turn keeps the record, and a handed-over 404 result yields null, both without asking again`, async () => {
    const store = await seeded(open, [1]);
    const record = await store.setInflight(SESSION, { client_msg_id: 'm2', action: '行動' });
    const api = fakeApi([turn(1)]);
    const sync = Sync.createSync({ api, store });
    const result = await sync.catchUp(SESSION);
    assert.deepEqual(await sync.resolveInflight(SESSION, { result }), record);
    assert.equal(await sync.resolveInflight(SESSION, { result: { status: 'gone', turns: [] } }), null);
    assert.equal(api.calls.length, 1);
  });

  test(`[${name}] a malformed handed-over result is ignored and the server is asked as usual`, async () => {
    const store = await seeded(open, [1]);
    await store.setInflight(SESSION, { client_msg_id: 'm2', action: '行動' });
    const api = fakeApi([turn(1), turn(2)]);
    assert.equal(await Sync.createSync({ api, store }).resolveInflight(SESSION, { result: { status: 'ok' } }), null);
    assert.equal(api.calls.length, 1);
  });

  // ---- 快照 ----------------------------------------------------------------------------------------------

  test(`[${name}] remember stores the whitelisted snapshot fields only`, async () => {
    const store = await open();
    const record = await Sync.createSync({ api: fakeApi([]), store }).remember(SESSION, {
      title: '雨夜歸人', ended: false, completed_turns: 2, opening: { state: 'ready', text: '開場' },
      notebook: { text: '記事簿全文' }, protagonist: { name: '阿青' }
    });
    assert.equal(record.title, '雨夜歸人');
    const text = JSON.stringify(await store.getSession(SESSION));
    assert.ok(!text.includes('記事簿') && !text.includes('阿青'));
  });
}

test('remember, catchUp and resolveInflight never reject even when the store itself fails', async () => {
  const broken = new Proxy({}, { get: () => () => Promise.reject(new Error('儲存壞了')) });
  const sync = Sync.createSync({ api: fakeApi([turn(1)]), store: broken });
  assert.equal(await sync.remember(SESSION, {}), null);
  const result = await sync.catchUp(SESSION);
  assert.equal(result.status, 'failed');
  assert.equal(await sync.resolveInflight(SESSION), null);
});

test('the sync engine never calls the stream: its API is the turns listing only, and the source mentions no stream client or fetch', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'game-sync.js'), 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const forbidden of ['createTurnStream', 'turn-stream', 'fetch', 'XMLHttpRequest', 'EventSource', 'localStorage', 'sessionStorage', 'indexedDB', 'console.']) {
    assert.ok(!code.includes(forbidden), forbidden);
  }
  assert.deepEqual([...code.matchAll(/\bapi\.(\w+)\(/g)].map((match) => match[1]), ['turns']);
});

test('the status helper separates connection failures from server answers', () => {
  assert.equal(Sync.statusOfFailure({ kind: 'network', status: 0 }), 'offline');
  assert.equal(Sync.statusOfFailure({ kind: 'timeout', status: 0 }), 'offline');
  assert.equal(Sync.statusOfFailure({ kind: 'canceled', status: 0 }), 'canceled');
  assert.equal(Sync.statusOfFailure({ kind: 'server', status: 404 }), 'gone');
  assert.equal(Sync.statusOfFailure({ kind: 'server', status: 500 }), 'failed');
  assert.equal(Sync.statusOfFailure(new Error('x')), 'failed');
  assert.equal(Sync.statusOfFailure(undefined), 'failed');
});
