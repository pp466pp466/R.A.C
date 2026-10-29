// 客服同步引擎的單元測試：假的伺服器（以訊息識別碼冪等）＋記憶體暫存。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Store = require('../../app/static/js/cs/cs-store.js');
const Sync = require('../../app/static/js/cs/cs-sync.js');

const NOW = '2026-10-29T12:00:00.000+00:00';

function failure(kind, status, code, extra) {
  return Object.assign({ kind, status, code, message: code }, extra || {});
}
const NETWORK = () => failure('network', 0, 'network');
const TIMEOUT = () => failure('timeout', 0, 'timeout');
const BUSY = () => failure('server', 409, 'conversation_busy');

/* 假伺服器：與真正的後端一樣以（對話、client_msg_id）去重，序號單調遞增；`script` 是接下來幾次 sendMessage 要丟出的錯誤。 */
function fakeServer() {
  const conversations = new Map();
  const byClient = new Map();
  const server = { calls: [], script: [], inflight: 0, maxInflight: 0, offline: false, delayed: null };
  let conversationCounter = 0;

  function view(conversation) {
    return { id: conversation.id, title: conversation.title, state_version: conversation.state_version, updated_at: conversation.updated_at, created_at: conversation.created_at };
  }

  server.addConversation = (id, title) => {
    const conversation = { id, title, state_version: 1, updated_at: NOW, created_at: NOW, messages: [] };
    conversations.set(id, conversation);
    return conversation;
  };
  server.conversations = conversations;

  server.api = {
    createConversation(clientId) {
      server.calls.push(['create', clientId]);
      if (server.offline) { return Promise.reject(NETWORK()); }
      if (server.createError) { return Promise.reject(server.createError); }
      if (!byClient.has(clientId)) {
        const conversation = server.addConversation('srv-' + (++conversationCounter), '新對話');
        byClient.set(clientId, conversation);
      }
      return Promise.resolve({ conversation: view(byClient.get(clientId)) });
    },
    listConversations() {
      server.calls.push(['list']);
      if (server.offline) { return Promise.reject(NETWORK()); }
      return Promise.resolve({ conversations: [...conversations.values()].map(view) });
    },
    renameConversation(id, title, version) {
      server.calls.push(['rename', id, title, version]);
      const conversation = conversations.get(id);
      if (!conversation) { return Promise.reject(failure('server', 404, 'not_found')); }
      if (conversation.state_version !== version) {
        return Promise.reject(failure('server', 409, 'version_conflict', { conversation: view(conversation) }));
      }
      conversation.title = title;
      conversation.state_version += 1;
      return Promise.resolve({ conversation: view(conversation) });
    },
    deleteConversation(id) {
      server.calls.push(['delete', id]);
      if (server.offline) { return Promise.reject(NETWORK()); }
      if (!conversations.delete(id)) { return Promise.reject(failure('server', 404, 'not_found')); }
      return Promise.resolve({ ok: true });
    },
    sendMessage(id, body, opts) {
      server.calls.push(['send', id, body.client_msg_id, body.content]);
      server.inflight += 1;
      server.maxInflight = Math.max(server.maxInflight, server.inflight);
      const finish = (fn) => { server.inflight -= 1; return fn(); };
      if (opts && opts.signal && opts.signal.aborted) { return Promise.resolve().then(() => finish(() => Promise.reject(failure('canceled', 0, 'canceled')))); }
      const scripted = server.script.shift();
      return Promise.resolve().then(() => server.delayed).then(() => finish(() => {
        if (server.offline) { return Promise.reject(NETWORK()); }
        const conversation = conversations.get(id);
        if (!conversation) { return Promise.reject(failure('server', 404, 'not_found')); }
        if (scripted && scripted.beforeProcessing) { return Promise.reject(scripted.beforeProcessing); }
        // 伺服器照常處理並寫入（即使回應之後因為網路而丟失）
        let user = conversation.messages.find((m) => m.role === 'user' && m.client_msg_id === body.client_msg_id);
        if (user && user.content !== body.content) { return Promise.reject(failure('server', 409, 'message_id_reused')); }
        let reply;
        if (!user) {
          user = { seq: conversation.messages.length + 1, client_msg_id: body.client_msg_id, role: 'user', content: body.content, msg_status: 'ok', suggested_actions: [], created_at: NOW };
          reply = { seq: user.seq + 1, client_msg_id: 'a-' + body.client_msg_id, role: 'assistant', content: '回覆：' + body.content, msg_status: 'ok', suggested_actions: [], created_at: NOW };
          conversation.messages.push(user, reply);
          if (conversation.title === '新對話') { conversation.title = body.content.slice(0, 30); conversation.state_version += 1; }
        } else {
          reply = conversation.messages.find((m) => m.seq === user.seq + 1);
        }
        if (scripted && scripted.afterProcessing) { return Promise.reject(scripted.afterProcessing); }
        return Promise.resolve({ user_message: user, assistant_message: reply, conversation: view(conversation) });
      }));
    },
    listMessages(id, opts) {
      server.calls.push(['messages', id, opts.since_seq]);
      if (server.offline) { return Promise.reject(NETWORK()); }
      const conversation = conversations.get(id);
      if (!conversation) { return Promise.reject(failure('server', 404, 'not_found')); }
      const rest = conversation.messages.filter((m) => m.seq > opts.since_seq);
      return Promise.resolve({ messages: rest.slice(0, opts.limit), has_more: rest.length > opts.limit });
    }
  };
  return server;
}

async function setup(options) {
  options = options || {};
  const server = fakeServer();
  const store = Store.createStore(Store.createMemoryBackend());
  let counter = 0;
  const sleeps = [];
  const events = [];
  const sync = Sync.createSync({
    api: server.api, store, storeApi: Store, newId: () => 'id-' + (++counter), now: () => NOW,
    sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); },
    onChange: (event) => events.push(event), onAuthLost: () => { events.push({ type: 'auth-lost' }); },
    backoffMs: options.backoffMs
  });
  const conversation = server.addConversation('c1', '新對話');
  await store.saveConversation(Sync.conversationRecord({ id: 'c1', title: '新對話', state_version: 1, updated_at: NOW, created_at: NOW }));
  return { server, store, sync, sleeps, events, conversation };
}

const sentIds = (server) => server.calls.filter((c) => c[0] === 'send').map((c) => c[2]);

test('a queued message is sent, the pending entry is replaced by the two server messages, and the conversation is refreshed', async () => {
  const { server, store, sync } = await setup();
  await sync.enqueue('c1', '你好', 'm1');
  const result = await sync.flush();
  assert.deepEqual(result, { sent: 1, failed: 0, stopped: null });
  assert.deepEqual((await store.getMessages('c1')).map((m) => [m.seq, m.role, m.content]), [[1, 'user', '你好'], [2, 'assistant', '回覆：你好']]);
  assert.deepEqual(await store.listPending(), []);
  const record = await store.getConversation('c1');
  assert.deepEqual({ title: record.title, version: record.state_version }, { title: '你好', version: 2 });
  assert.deepEqual(sentIds(server), ['m1']);
});

test('three messages typed while offline are sent one after another in the order they were typed', async () => {
  const { server, store, sync } = await setup();
  server.offline = true;
  await sync.enqueue('c1', '第一則', 'm1');
  await sync.enqueue('c1', '第二則', 'm2');
  await sync.enqueue('c1', '第三則', 'm3');
  assert.equal((await sync.flush()).stopped, 'network');
  assert.deepEqual(sentIds(server), ['m1']);                          // 第一則失敗就停止本輪，後面的不亂序送出
  assert.equal((await store.listPending()).length, 3);
  server.offline = false;
  server.calls.length = 0;
  assert.deepEqual(await sync.flush(), { sent: 3, failed: 0, stopped: null });
  assert.deepEqual(sentIds(server), ['m1', 'm2', 'm3']);
  assert.equal(server.maxInflight, 1);                                // 同一時間只有一則在途中
  assert.deepEqual((await store.getMessages('c1')).filter((m) => m.role === 'user').map((m) => m.content), ['第一則', '第二則', '第三則']);
  assert.deepEqual(server.conversations.get('c1').messages.map((m) => m.seq), [1, 2, 3, 4, 5, 6]);
});

test('a timeout keeps the message queued, stops the round, and the resend produces no duplicate', async () => {
  const { server, store, sync } = await setup();
  server.script.push({ afterProcessing: TIMEOUT() });                  // 伺服器已經寫入，但回應沒有到
  await sync.enqueue('c1', '你好', 'm1');
  const first = await sync.flush();
  assert.equal(first.stopped, 'network');
  assert.equal((await store.findPending('m1')).status, 'queued');
  assert.equal((await store.findPending('m1')).attempts, 1);
  assert.equal(server.conversations.get('c1').messages.length, 2);     // 後端已經有兩則
  const second = await sync.flush();
  assert.deepEqual(second, { sent: 1, failed: 0, stopped: null });
  assert.equal(server.conversations.get('c1').messages.length, 2);     // 重送沒有多出訊息
  assert.equal((await store.getMessages('c1')).length, 2);
  assert.deepEqual(await store.listPending(), []);
});

for (const [label, scripted] of [
  ['500', failure('server', 500, 'internal_error')],
  ['503 service_unavailable', failure('server', 503, 'service_unavailable')],
  ['429 too_many_requests', failure('server', 429, 'too_many_requests')],
  ['a dropped connection', NETWORK()]
]) {
  test(`${label}: the message stays queued and the round stops`, async () => {
    const { server, store, sync } = await setup();
    server.script.push({ beforeProcessing: scripted });
    await sync.enqueue('c1', '甲', 'm1');
    await sync.enqueue('c1', '乙', 'm2');
    const result = await sync.flush();
    assert.equal(result.stopped, 'network');
    assert.deepEqual(sentIds(server), ['m1']);
    assert.deepEqual((await store.listPending()).map((p) => [p.client_msg_id, p.status, p.error]), [['m1', 'queued', scripted.code], ['m2', 'queued', null]]);
  });
}

test('conversation_busy backs off 1, 2 and 4 seconds and then succeeds', async () => {
  const { server, sync, sleeps, store } = await setup();
  server.script.push({ beforeProcessing: BUSY() }, { beforeProcessing: BUSY() });
  await sync.enqueue('c1', '你好', 'm1');
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
  assert.deepEqual(sleeps, [1000, 2000]);
  assert.deepEqual(sentIds(server), ['m1', 'm1', 'm1']);              // 同一個識別碼重試
  assert.deepEqual(await store.listPending(), []);
});

test('if the conversation stays busy the message is kept and the round stops after the last backoff', async () => {
  const { server, sync, sleeps, store } = await setup();
  server.script.push(...Array.from({ length: 10 }, () => ({ beforeProcessing: BUSY() })));
  await sync.enqueue('c1', '你好', 'm1');
  await sync.enqueue('c1', '下一則', 'm2');
  const result = await sync.flush();
  assert.equal(result.stopped, 'busy');
  assert.deepEqual(sleeps, [1000, 2000, 4000]);
  assert.equal(sentIds(server).length, 4);                             // 一次加三次重試
  assert.deepEqual((await store.listPending()).map((p) => [p.client_msg_id, p.status, p.error]), [['m1', 'queued', 'conversation_busy'], ['m2', 'queued', null]]);
});

test('a message the server rejects (4xx) is marked as unsendable, skipped from then on, and does not block the ones after it', async () => {
  const { server, sync, store, events } = await setup();
  server.script.push({ beforeProcessing: failure('server', 400, 'validation_error', { message: '訊息最多 1000 字。' }) });
  await sync.enqueue('c1', '壞掉的', 'm1');
  await sync.enqueue('c1', '正常的', 'm2');
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 1, stopped: null });
  const failed = await store.findPending('m1');
  assert.deepEqual({ status: failed.status, error: failed.error, message: failed.message }, { status: 'failed', error: 'validation_error', message: '訊息最多 1000 字。' });
  assert.deepEqual(sentIds(server), ['m1', 'm2']);
  server.calls.length = 0;
  assert.deepEqual(await sync.flush(), { sent: 0, failed: 0, stopped: null });
  assert.deepEqual(server.calls, []);                                  // 無法送出的不會被自動重送
  assert.ok(events.some((e) => e.type === 'pending'));
});

test('an unsendable message can be retried or removed', async () => {
  const { server, sync, store } = await setup();
  server.script.push({ beforeProcessing: failure('server', 403, 'age_confirmation_required') });
  await sync.enqueue('c1', '被閘門擋下', 'm1');
  await sync.flush();
  assert.equal((await store.findPending('m1')).status, 'failed');
  await sync.retryFailed('m1');
  assert.equal((await store.findPending('m1')).status, 'queued');
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
  await sync.enqueue('c1', '另一則', 'm2');
  server.script.push({ beforeProcessing: failure('server', 400, 'validation_error') });
  await sync.flush();
  await sync.removeFailed('m2');
  assert.deepEqual(await store.listPending(), []);
});

test('a lost login (401) stops the round, tells the page, and leaves the message untouched', async () => {
  const { server, sync, store, events } = await setup();
  server.script.push({ beforeProcessing: failure('server', 401, 'unauthorized') });
  await sync.enqueue('c1', '你好', 'm1');
  assert.equal((await sync.flush()).stopped, 'auth');
  assert.ok(events.some((e) => e.type === 'auth-lost'));
  const entry = await store.findPending('m1');
  assert.deepEqual({ status: entry.status, attempts: entry.attempts }, { status: 'queued', attempts: 0 });
});

test('aborting (cancel, switching conversation, leaving the page) keeps the message queued and is not an error', async () => {
  const { server, sync, store } = await setup();
  await sync.enqueue('c1', '你好', 'm1');
  const controller = new AbortController();
  controller.abort();
  const result = await sync.flush({ signal: controller.signal });
  assert.equal(result.stopped, 'canceled');
  assert.equal((await store.findPending('m1')).status, 'queued');
  assert.equal((await store.findPending('m1')).attempts, 0);
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
});

test('flush calls made while a round is running do not send anything twice; a second pass picks up new messages', async () => {
  const { server, sync, store } = await setup();
  let release;
  server.delayed = new Promise((resolve) => { release = resolve; });
  await sync.enqueue('c1', '甲', 'm1');
  const first = sync.flush();
  await new Promise((resolve) => setImmediate(resolve));
  await sync.enqueue('c1', '乙', 'm2');
  const second = sync.flush();
  assert.equal(second, first);                                         // 同一個進行中的工作
  release();
  assert.deepEqual(await first, { sent: 2, failed: 0, stopped: null });
  assert.deepEqual(sentIds(server), ['m1', 'm2']);
  assert.deepEqual(await store.listPending(), []);
});

// ---- 以後端為準的拉取 --------------------------------------------------------------------------

test('pull asks only for messages after the highest cached sequence and the backend overwrites the cached copy', async () => {
  const { server, store, sync } = await setup();
  server.conversations.get('c1').messages.push(
    { seq: 1, client_msg_id: 'a', role: 'user', content: '後端的第一則', msg_status: 'ok', suggested_actions: [], created_at: NOW },
    { seq: 2, client_msg_id: 'b', role: 'assistant', content: '後端的第二則', msg_status: 'ok', suggested_actions: [], created_at: NOW },
    { seq: 3, client_msg_id: 'c', role: 'user', content: '後端的第三則', msg_status: 'ok', suggested_actions: [], created_at: NOW }
  );
  await store.upsertMessages('c1', [{ seq: 1, client_msg_id: 'a', role: 'user', content: '本機過期的', msg_status: 'ok', suggested_actions: [], created_at: NOW }]);
  const got = await sync.pull('c1');
  assert.deepEqual(server.calls.filter((c) => c[0] === 'messages').map((c) => c[2]), [1]);
  assert.deepEqual(got.map((m) => m.seq), [2, 3]);
  assert.deepEqual((await store.getMessages('c1')).map((m) => m.content), ['本機過期的', '後端的第二則', '後端的第三則']);
});

test('a full pull of a long conversation follows the pages until nothing is left', async () => {
  const { server, store, sync } = await setup();
  for (let seq = 1; seq <= 450; seq++) {
    server.conversations.get('c1').messages.push({ seq, client_msg_id: 'x' + seq, role: seq % 2 ? 'user' : 'assistant', content: 'm' + seq, msg_status: 'ok', suggested_actions: [], created_at: NOW });
  }
  await sync.pull('c1');
  assert.deepEqual(server.calls.filter((c) => c[0] === 'messages').map((c) => c[2]), [0, 200, 400]);
  assert.equal((await store.getMessages('c1')).length, 450);
});

test('a pending message whose reply is already on the server is dropped after pulling; one without a reply stays', async () => {
  const { server, store, sync } = await setup();
  server.conversations.get('c1').messages.push(
    { seq: 1, client_msg_id: 'answered', role: 'user', content: '有回覆', msg_status: 'ok', suggested_actions: [], created_at: NOW },
    { seq: 2, client_msg_id: 'a-answered', role: 'assistant', content: '回覆', msg_status: 'ok', suggested_actions: [], created_at: NOW },
    { seq: 3, client_msg_id: 'orphan', role: 'user', content: '處理中斷', msg_status: 'ok', suggested_actions: [], created_at: NOW }
  );
  await sync.enqueue('c1', '有回覆', 'answered');
  await sync.enqueue('c1', '處理中斷', 'orphan');
  await sync.enqueue('c1', '還沒送出', 'unsent');
  await sync.pull('c1');
  assert.deepEqual((await store.listPending()).map((p) => p.client_msg_id), ['orphan', 'unsent']);
});

test('pulling a conversation that was deleted on another device removes it locally', async () => {
  const { server, store, sync } = await setup();
  server.conversations.delete('c1');
  await store.upsertMessages('c1', [{ seq: 1, client_msg_id: 'a', role: 'user', content: 'x', msg_status: 'ok', suggested_actions: [], created_at: NOW }]);
  assert.deepEqual(await sync.pull('c1'), []);
  assert.equal(await store.getConversation('c1'), undefined);
  assert.deepEqual(await store.getMessages('c1'), []);
});

test('refreshing the list lets the backend decide which conversations exist', async () => {
  const { server, store, sync } = await setup();
  await store.saveConversation(Sync.conversationRecord({ id: 'gone', title: '已被刪除', state_version: 1, updated_at: NOW, created_at: NOW }));
  server.addConversation('c2', '另一台裝置建立的');
  const removed = await sync.refreshConversations();
  assert.deepEqual(removed, ['gone']);
  assert.deepEqual((await store.listConversations()).map((c) => c.key).sort(), ['c1', 'c2']);
});

// ---- 離線建立的對話 ------------------------------------------------------------------------------

test('a conversation created offline keeps its client id, is created idempotently later, and its messages follow', async () => {
  const { server, store, sync } = await setup();
  server.offline = true;
  const local = await sync.createConversation('client-1');
  assert.equal(local.key, 'new:client-1');
  assert.equal(local.id, null);
  await sync.enqueue(local.key, '離線時開始的對話', 'm1');
  assert.equal((await sync.flush()).stopped, 'network');
  assert.equal((await store.getConversation('new:client-1')).client_id, 'client-1');
  server.offline = false;
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
  assert.deepEqual(server.calls.filter((c) => c[0] === 'create').map((c) => c[1]), ['client-1', 'client-1', 'client-1']);   // 建立時、離線重送、連線後重送：都是同一個識別碼
  const created = (await store.listConversations()).filter((c) => c.client_id === 'client-1');
  assert.equal(created.length, 1);
  assert.notEqual(created[0].key, 'new:client-1');
  assert.equal((await store.getMessages(created[0].key)).length, 2);
  assert.equal([...server.conversations.values()].filter((c) => c.id !== 'c1').length, 1);   // 伺服器只有一個
});

test('creating a conversation while online goes straight to its real identifier', async () => {
  const { store, sync } = await setup();
  const created = await sync.createConversation('client-2');
  assert.match(created.key, /^srv-/);
  assert.equal(created.client_id, 'client-2');
  assert.equal(await store.getConversation('new:client-2'), undefined);
});

// ---- 改名與刪除 ----------------------------------------------------------------------------------

test('renaming with the current version updates the title and the version', async () => {
  const { sync, store } = await setup();
  assert.deepEqual(await sync.rename('c1', '新標題'), { ok: true, conflict: false });
  const record = await store.getConversation('c1');
  assert.deepEqual({ title: record.title, version: record.state_version }, { title: '新標題', version: 2 });
});

test('a version conflict adopts the backend latest title and version', async () => {
  const { sync, store, server } = await setup();
  server.conversations.get('c1').title = '另一台裝置改的';
  server.conversations.get('c1').state_version = 5;
  assert.deepEqual(await sync.rename('c1', '我的改名'), { ok: false, conflict: true });
  const record = await store.getConversation('c1');
  assert.deepEqual({ title: record.title, version: record.state_version }, { title: '另一台裝置改的', version: 5 });
});

test('deleting removes the conversation locally and remotely; a 404 counts as done; a network error leaves everything', async () => {
  const { sync, store, server } = await setup();
  await store.upsertMessages('c1', [{ seq: 1, client_msg_id: 'a', role: 'user', content: 'x', msg_status: 'ok', suggested_actions: [], created_at: NOW }]);
  server.offline = true;
  await assert.rejects(sync.deleteConversation('c1'), (error) => error.kind === 'network');
  assert.notEqual(await store.getConversation('c1'), undefined);
  assert.equal((await store.getMessages('c1')).length, 1);
  server.offline = false;
  server.conversations.delete('c1');                                  // 已被別的裝置刪除
  await sync.deleteConversation('c1');
  assert.equal(await store.getConversation('c1'), undefined);
  assert.deepEqual(await store.getMessages('c1'), []);
});

test('pending messages of a conversation that no longer exists locally are discarded instead of sent', async () => {
  const { sync, store, server } = await setup();
  await sync.enqueue('c1', '孤兒', 'm1');
  await store.removeConversation('c1');
  await store.addPending({ client_msg_id: 'm2', conversation_key: 'c1', content: '孤兒二' });
  assert.deepEqual(await sync.flush(), { sent: 0, failed: 0, stopped: null });
  assert.deepEqual(sentIds(server), []);
  assert.deepEqual(await store.listPending(), []);
});

test('classify maps the failures the way the engine expects', () => {
  assert.equal(Sync.classify(NETWORK()), 'retry');
  assert.equal(Sync.classify(TIMEOUT()), 'retry');
  assert.equal(Sync.classify(failure('canceled', 0, 'canceled')), 'canceled');
  assert.equal(Sync.classify(failure('server', 401, 'unauthorized')), 'auth');
  assert.equal(Sync.classify(BUSY()), 'busy');
  for (const status of [408, 429, 500, 502, 503, 504]) { assert.equal(Sync.classify(failure('server', status, 'x')), 'retry', String(status)); }
  for (const status of [400, 403, 404, 413, 415]) { assert.equal(Sync.classify(failure('server', status, 'x')), 'fail', String(status)); }
  assert.equal(Sync.classify(failure('server', 409, 'message_id_reused')), 'fail');
});

// ---- axios 轉接 ----------------------------------------------------------------------------------

test('the axios adapter sends with the timeout given by the server, passes the abort signal, and normalises errors', async () => {
  const requests = [];
  const axios = {
    post: (url, body, config) => { requests.push(['post', url, body, config]); return Promise.resolve({ data: { ok: true } }); },
    get: (url, config) => { requests.push(['get', url, config]); return Promise.resolve({ data: { messages: [], has_more: false } }); },
    patch: (url, body) => { requests.push(['patch', url, body]); return Promise.reject({ response: { status: 409, data: { error: { code: 'version_conflict', message: 'x' }, conversation: { id: 'c1', title: '後端的' } } } }); },
    delete: (url) => { requests.push(['delete', url]); return Promise.resolve({ data: { ok: true } }); }
  };
  const describeError = (error) => ({ kind: 'server', status: error.response.status, code: error.response.data.error.code, message: error.response.data.error.message });
  const adapter = Sync.createAxiosApi(axios, describeError, { sendTimeoutMs: 45000 });
  const controller = new AbortController();
  await adapter.sendMessage('c/1', { client_msg_id: 'm', content: 'x' }, { signal: controller.signal });
  assert.deepEqual(requests[0].slice(0, 3), ['post', '/api/cs/conversations/c%2F1/messages', { client_msg_id: 'm', content: 'x' }]);
  assert.equal(requests[0][3].timeout, 45000);
  assert.equal(requests[0][3].signal, controller.signal);
  await adapter.listMessages('c1', { since_seq: 4, limit: 200 });
  assert.deepEqual(requests[1][2].params, { since_seq: 4, limit: 200 });
  await assert.rejects(adapter.renameConversation('c1', '我的', 1), (error) => error.code === 'version_conflict' && error.conversation.title === '後端的');
  await adapter.deleteConversation('c1');
  assert.equal(requests[3][1], '/api/cs/conversations/c1');
});


// ---- 用量限制（usage-limits）--------------------------------------------------------------------------

const RATE_LIMITED = () => failure('server', 429, 'rate_limited', { message: '您傳送訊息的速度太快了，請稍候幾秒再試。' });
const SYSTEM_BUSY = () => failure('server', 503, 'system_busy', { message: '目前使用人數較多，請稍後再試。' });
const DAILY_QUOTA = () => failure('server', 429, 'daily_quota_exceeded', { message: '您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。' });
const SUSPENDED = () => failure('server', 403, 'chat_suspended', { message: '客服對話已暫停，將於 13:00（台灣時間）恢復。' });
const CONVERSATION_LIMIT = () => failure('server', 409, 'conversation_limit_reached', { message: '對話數已達上限，請先刪除不需要的對話。' });

test('usage rejections are classified: temporary ones are retried later, the ones that cannot succeed soon are not', () => {
  assert.equal(Sync.classify(RATE_LIMITED()), 'retry');
  assert.equal(Sync.classify(SYSTEM_BUSY()), 'retry');
  assert.equal(Sync.classify(DAILY_QUOTA()), 'fail');          // 429，但不能被當成「稍後再試」自動重送
  assert.equal(Sync.classify(SUSPENDED()), 'fail');
  assert.equal(Sync.classify(CONVERSATION_LIMIT()), 'fail');
});

test('the existing classifications do not change', () => {
  assert.equal(Sync.classify(failure('server', 429, 'too_many_requests')), 'retry');
  assert.equal(Sync.classify(failure('server', 503, 'service_unavailable')), 'retry');
  assert.equal(Sync.classify(BUSY()), 'busy');
  assert.equal(Sync.classify(failure('server', 403, 'forbidden')), 'fail');
  assert.equal(Sync.classify(failure('server', 401, 'unauthorized')), 'auth');
  assert.equal(Sync.classify(NETWORK()), 'retry');
  assert.equal(Sync.classify(failure('canceled', 0, 'canceled')), 'canceled');
});

for (const [label, scripted] of [['rate_limited', RATE_LIMITED()], ['system_busy', SYSTEM_BUSY()]]) {
  test(`${label}: the message stays queued with the code, the round stops, and the same id is sent again later`, async () => {
    const { server, store, sync } = await setup();
    server.script.push({ beforeProcessing: scripted });
    await sync.enqueue('c1', '甲', 'm1');
    const first = await sync.flush();
    assert.equal(first.stopped, 'network');
    assert.deepEqual((await store.listPending()).map((p) => [p.client_msg_id, p.status, p.error]), [['m1', 'queued', label]]);
    assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
    assert.deepEqual(sentIds(server), ['m1', 'm1']);
  });
}

for (const [label, scripted] of [['daily_quota_exceeded', DAILY_QUOTA()], ['chat_suspended', SUSPENDED()]]) {
  test(`${label}: the message is marked unsendable with the server's explanation and is not sent again by itself`, async () => {
    const { server, store, sync } = await setup();
    server.script.push({ beforeProcessing: scripted });
    await sync.enqueue('c1', '甲', 'm1');
    assert.deepEqual(await sync.flush(), { sent: 0, failed: 1, stopped: null });
    const [entry] = await store.listPending();
    assert.deepEqual([entry.status, entry.error, entry.message], ['failed', label, scripted.message]);
    server.calls.length = 0;
    assert.deepEqual(await sync.flush(), { sent: 0, failed: 0, stopped: null });
    assert.deepEqual(sentIds(server), []);                              // 不自動重送；使用者按「重試」才會再送
  });
}

test('retrying an unsendable message sends it again with the same id', async () => {
  const { server, store, sync } = await setup();
  server.script.push({ beforeProcessing: DAILY_QUOTA() });
  await sync.enqueue('c1', '甲', 'm1');
  await sync.flush();
  await sync.retryFailed('m1');
  assert.deepEqual(await sync.flush(), { sent: 1, failed: 0, stopped: null });
  assert.deepEqual(sentIds(server), ['m1', 'm1']);
  assert.deepEqual(await store.listPending(), []);
});

test('a full conversation list: creating one is refused, nothing is left behind, and the reason reaches the caller', async () => {
  const { server, store, sync } = await setup();
  server.createError = CONVERSATION_LIMIT();
  await assert.rejects(sync.createConversation('client-9'), (error) => error.code === 'conversation_limit_reached' && /對話數已達上限/.test(error.message));
  assert.deepEqual((await store.listConversations()).map((c) => c.key), ['c1']);      // 沒有留下永遠「待建立」的對話
});

test('creating a conversation still survives other failures by staying an offline conversation', async () => {
  const { server, store, sync } = await setup();
  server.createError = failure('server', 503, 'service_unavailable');
  const local = await sync.createConversation('client-8');
  assert.equal(local.key, 'new:client-8');
  assert.ok(await store.getConversation('new:client-8'));
});

test('an offline conversation that can never be created is dropped with its pending messages when the list turns out to be full', async () => {
  const { server, store, sync } = await setup();
  server.offline = true;
  const local = await sync.createConversation('client-7');
  await sync.enqueue(local.key, '離線時打的', 'm1');
  server.offline = false;
  server.createError = CONVERSATION_LIMIT();
  const result = await sync.flush();
  assert.equal(result.limited, '對話數已達上限，請先刪除不需要的對話。');
  assert.equal(result.sent, 0);
  assert.deepEqual((await store.listConversations()).map((c) => c.key), ['c1']);
  assert.deepEqual(await store.listPending(), []);
});

test('the usage call goes to /api/cs/usage and carries its error description like the other calls', async () => {
  const requests = [];
  const axios = {
    get: (url, config) => {
      requests.push([url, config]);
      if (requests.length === 2) { return Promise.reject({ response: { status: 401, data: { error: { code: 'unauthorized', message: '請先登入。' } } } }); }
      return Promise.resolve({ data: { usage: { daily: { used: 3, limit: 30 } } } });
    }
  };
  const describeError = (error) => ({ kind: 'server', status: error.response.status, code: error.response.data.error.code, message: error.response.data.error.message });
  const adapter = Sync.createAxiosApi(axios, describeError, { sendTimeoutMs: 45000 });
  const controller = new AbortController();
  assert.deepEqual(await adapter.getUsage({ signal: controller.signal }), { usage: { daily: { used: 3, limit: 30 } } });
  assert.equal(requests[0][0], '/api/cs/usage');
  assert.equal(requests[0][1].signal, controller.signal);
  await assert.rejects(adapter.getUsage(), (error) => error.status === 401 && error.code === 'unauthorized');
});
