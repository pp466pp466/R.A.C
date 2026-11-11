// RPG 入口與遊戲頁 API 封裝的單元測試（rpg-game-ui 2.3）：以真的 Auth 與 game-api.js、替身 axios（`vm` 沙盒）驗證每個呼叫的方法、路徑、本文、逾時與 signal，
// 以及失敗物件帶出 game-state 的對照需要的欄位（記事簿衝突、字數與容量、簽到預覽、餘額與所需數量、審查類別）。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const State = require('../../app/static/js/game/game-state.js');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const SESSION = '99999999-9999-4999-8999-999999999999';

/* 沙盒：真的 auth-common.js（沒有 document，所以不會替 axios 設定預設值）與 game-api.js；axios 是替身，記錄每個請求並依 `respond` 回應。 */
function sandboxWith(respond) {
  const requests = [];
  const sandbox = { Promise };
  sandbox.self = sandbox;
  sandbox.axios = (request) => {
    requests.push(JSON.parse(JSON.stringify(request, (key, value) => (key === 'signal' ? undefined : value))));
    requests[requests.length - 1].signal = request.signal;
    return respond(request);
  };
  vm.createContext(sandbox);
  ['app/static/js/auth/auth-common.js', 'app/static/js/game/game-api.js'].forEach((file) => vm.runInContext(read(file), sandbox, { filename: file }));
  return { api: sandbox.Game.api, requests, Auth: sandbox.Auth };
}

// 沙盒（另一個 realm）建立的物件與陣列原型不同，deepStrictEqual 會判為不相等；比較前先轉成本 realm 的普通物件。
const plain = (value) => JSON.parse(JSON.stringify(value));

const ok = (data) => () => Promise.resolve({ status: 200, data });
const serverError = (status, body) => () => Promise.reject({ response: { status, data: body } });

test('the page-level API exposes exactly the entry, game and wallet calls', () => {
  const { api } = sandboxWith(ok({}));
  assert.deepEqual(Object.keys(api).sort(), ['catalog', 'checkin', 'notebook', 'saveNotebook', 'sessions', 'setup', 'snapshot', 'start', 'suggest', 'turns', 'wallet']);
});

test('snapshot reads the save by its public id and returns the response body untouched', async () => {
  const body = { session: { id: SESSION, title: '雨夜歸人' } };
  const { api, requests } = sandboxWith(ok(body));
  assert.deepEqual(await api.snapshot(SESSION), body);
  assert.deepEqual(requests, [{ method: 'get', url: `/api/game/sessions/${SESSION}`, signal: undefined }]);
});

test('turns asks for since_seq (and limit when given) and passes the signal and timeout from the caller', async () => {
  const controller = new AbortController();
  const { api, requests } = sandboxWith(ok({ turns: [], has_more: false }));
  await api.turns(SESSION, 40, { signal: controller.signal, timeout: 12000 });
  assert.equal(requests[0].method, 'get');
  assert.equal(requests[0].url, `/api/game/sessions/${SESSION}/turns`);
  assert.deepEqual(requests[0].params, { since_seq: 40 });
  assert.equal(requests[0].timeout, 12000);
  assert.equal(requests[0].signal, controller.signal);
  await api.turns(SESSION, 0, { limit: 50 });
  assert.deepEqual(requests[1].params, { since_seq: 0, limit: 50 });
  await api.turns(SESSION);
  assert.deepEqual(requests[2].params, {});
});

test('wallet and notebook are plain reads', async () => {
  const { api, requests } = sandboxWith(ok({ ok: true }));
  await api.wallet();
  await api.notebook(SESSION);
  assert.deepEqual(requests.map((r) => [r.method, r.url, r.data, r.params]), [
    ['get', '/api/wallet', undefined, undefined],
    ['get', `/api/game/sessions/${SESSION}/notebook`, undefined, undefined]
  ]);
});

test('checkin sends confirm_overflow only as a real boolean, true only when the caller confirmed', async () => {
  const { api, requests } = sandboxWith(ok({ ok: true }));
  await api.checkin();
  await api.checkin(false);
  await api.checkin('yes');                          // 不是真正的 true：不當成確認
  await api.checkin(true);
  assert.deepEqual(requests.map((r) => [r.method, r.url, r.data]), [
    ['post', '/api/wallet/checkin', { confirm_overflow: false }],
    ['post', '/api/wallet/checkin', { confirm_overflow: false }],
    ['post', '/api/wallet/checkin', { confirm_overflow: false }],
    ['post', '/api/wallet/checkin', { confirm_overflow: true }]
  ]);
});

test('saveNotebook is a PUT carrying the text and the version the edit started from', async () => {
  const controller = new AbortController();
  const { api, requests } = sandboxWith(ok({ notebook: {} }));
  await api.saveNotebook(SESSION, '第一行\n第二行', 3, { signal: controller.signal, timeout: 9000 });
  assert.equal(requests[0].method, 'put');
  assert.equal(requests[0].url, `/api/game/sessions/${SESSION}/notebook`);
  assert.deepEqual(requests[0].data, { text: '第一行\n第二行', expected_version: 3 });
  assert.equal(requests[0].timeout, 9000);
  assert.equal(requests[0].signal, controller.signal);
});

test('timeout and signal are only put on the request when the caller gave them', async () => {
  const { api, requests } = sandboxWith(ok({}));
  await api.snapshot(SESSION);
  await api.wallet();
  await api.checkin(false);
  await api.saveNotebook(SESSION, 'x', 1);
  for (const request of requests) { assert.ok(!('timeout' in request) && request.signal === undefined, request.url); }
});

test('ids are URL-encoded so a hostile id cannot change the path', async () => {
  const { api, requests } = sandboxWith(ok({}));
  await api.snapshot('../../admin?x=1');
  await api.turns('a/b', 1);
  await api.notebook('a b');
  await api.saveNotebook('a#b', 'x', 1);
  assert.deepEqual(requests.map((r) => r.url), [
    '/api/game/sessions/..%2F..%2Fadmin%3Fx%3D1',
    '/api/game/sessions/a%2Fb/turns',
    '/api/game/sessions/a%20b/notebook',
    '/api/game/sessions/a%23b/notebook'
  ]);
});

test('the entry calls keep their behaviour: GET lists with paging, setup, and the two POSTs with the caller timeout', async () => {
  const { api, requests } = sandboxWith(ok({}));
  await api.catalog({ limit: 12, offset: 24 });
  await api.sessions();
  await api.setup('v/1', { timeout: 5000 });
  await api.suggest({ intro: 'x' }, { timeout: 35000 });
  await api.start({ version_id: 'v' }, { timeout: 65000 });
  assert.deepEqual(requests.map((r) => [r.method, r.url, r.params, r.data, r.timeout]), [
    ['get', '/api/sessions/catalog', { limit: 12, offset: 24 }, undefined, undefined],
    ['get', '/api/sessions', {}, undefined, undefined],
    ['get', '/api/sessions/setup/v%2F1', undefined, undefined, 5000],
    ['post', '/api/sessions/protagonist/suggest', undefined, { intro: 'x' }, 35000],
    ['post', '/api/sessions', undefined, { version_id: 'v' }, 65000]
  ]);
});

test('the only calls that can change anything are suggest, start, checkin and saveNotebook', async () => {
  const { api, requests } = sandboxWith(ok({}));
  const args = {
    catalog: [], sessions: [], setup: ['v'], suggest: [{}], start: [{}], snapshot: [SESSION], turns: [SESSION, 0], notebook: [SESSION],
    saveNotebook: [SESSION, 'x', 1], wallet: [], checkin: [false]
  };
  for (const name of Object.keys(api)) { await api[name](...args[name]); }
  const writes = requests.filter((r) => r.method !== 'get').map((r) => `${r.method} ${r.url}`).sort();
  assert.deepEqual(writes, [
    'post /api/sessions',
    'post /api/sessions/protagonist/suggest',
    'post /api/wallet/checkin',
    `put /api/game/sessions/${SESSION}/notebook`
  ]);
});

// ---- 失敗物件 ---------------------------------------------------------------------------------------

async function failureOf(call, respond) {
  const { api } = sandboxWith(respond);
  try { await call(api); } catch (info) { return info; }
  throw new Error('應該被拒絕');
}

test('a notebook conflict carries the notebook the backend holds now, in the shape game-state describes', async () => {
  const latest = { text: 'AI 剛追加的內容', version: 5, length: 9, capacity: 2000, reserve: 500, full: false };
  const info = await failureOf((api) => api.saveNotebook(SESSION, '我的草稿', 4),
    serverError(409, { ok: false, error: { code: 'notebook_conflict', message: '記事簿已經有了新的內容，已為您更新到最新的版本，請確認後再儲存。' }, notebook: latest }));
  assert.equal(info.status, 409);
  assert.equal(info.code, 'notebook_conflict');
  assert.deepEqual(info.notebook, latest);
  const described = State.describeNotebookFailure(info);
  assert.equal(described.kind, 'conflict');
  assert.equal(described.keepDraft, true);
  assert.deepEqual(described.notebook, { text: 'AI 剛追加的內容', version: 5, length: 9, capacity: 2000, reserve: 500, full: false });
});

test('a notebook that is too long carries the length and capacity as integers only', async () => {
  const info = await failureOf((api) => api.saveNotebook(SESSION, 'x', 1),
    serverError(422, { ok: false, error: { code: 'notebook_too_long', message: '記事簿的內容超過容量，請刪減到容量以內再儲存。' }, length: 2240, capacity: 2000 }));
  assert.equal(info.length, 2240);
  assert.equal(info.capacity, 2000);
  const described = State.describeNotebookFailure(info);
  assert.equal(described.kind, 'too_long');
  assert.deepEqual([described.length, described.capacity], [2240, 2000]);
  assert.ok(described.message.includes('2240') && described.message.includes('2000'));
});

test('a rejected notebook carries only category identifiers and the description names them without echoing anything else', async () => {
  const info = await failureOf((api) => api.saveNotebook(SESSION, 'x', 1),
    serverError(422, { ok: false, error: { code: 'notebook_blocked', message: '記事簿的內容違反平台的內容規範，無法儲存。請修改後再試。' }, categories: ['minor_sexual'] }));
  assert.deepEqual(plain(info.categories), ['minor_sexual']);
  assert.ok(!('notebook' in info) && !('length' in info));
  assert.equal(State.describeNotebookFailure(info).kind, 'rejected');
});

test('a check-in that would waste coins carries the preview, and the description exposes it for the cancel or claim choice', async () => {
  const info = await failureOf((api) => api.checkin(false),
    serverError(409, { ok: false, error: { code: 'checkin_overflow', message: '領取後免費遊戲幣會超過上限，超出的部分將作廢。要繼續領取嗎？' }, checkin: { would_gain: 200, would_lose: 150 } }));
  assert.deepEqual(plain(info.checkin), { would_gain: 200, would_lose: 150 });
  const described = State.describeCheckinFailure(info);
  assert.equal(described.kind, 'overflow');
  assert.deepEqual(plain(described.checkin), { wouldGain: 200, wouldLose: 150 });
});

test('an already-checked-in answer has no preview and the description takes the next reset time from the wallet', async () => {
  const info = await failureOf((api) => api.checkin(false),
    serverError(409, { ok: false, error: { code: 'already_checked_in', message: '您今天已經簽到過了，明天 00:00（台灣時間）後可以再簽到。' } }));
  assert.ok(!('checkin' in info));
  assert.equal(State.describeCheckinFailure(info, { nextResetAt: '2026-11-11T16:00:00+00:00' }).kind, 'already');
});

test('insufficient coins carries the total balance and the needed amount', async () => {
  const info = await failureOf((api) => api.snapshot(SESSION),
    serverError(409, { ok: false, error: { code: 'insufficient_coins', message: '遊戲幣不足，可以先到簽到領取今日的遊戲幣。' }, balance: 10, needed: 30 }));
  assert.deepEqual([info.balance, info.needed], [10, 30]);
  const described = State.describeRequestFailure(info);
  assert.equal(described.kind, 'coins');
  assert.deepEqual([described.balance, described.needed], [10, 30]);
});

test('values of the wrong type and fields that are not part of the contract are not copied into the failure', async () => {
  const info = await failureOf((api) => api.saveNotebook(SESSION, 'x', 1), serverError(422, {
    ok: false, error: { code: 'notebook_too_long', message: '太長' }, length: '2240', capacity: -1, balance: 1.5, needed: null,
    notebook: 'not an object', checkin: { would_gain: 'x', would_lose: 5 }, secret: '不該被複製', categories: 'nope'
  }));
  for (const field of ['length', 'capacity', 'balance', 'needed', 'notebook', 'checkin', 'secret']) { assert.ok(!(field in info), field); }
  assert.deepEqual(plain(info.categories), []);
});

test('a missing, text or array response body never breaks the failure handling', async () => {
  for (const data of [undefined, null, '<html>502 Bad Gateway</html>', [1, 2, 3], 42]) {
    const info = await failureOf((api) => api.wallet(), serverError(502, data));
    assert.equal(info.kind, 'server');
    assert.equal(info.status, 502);
    assert.deepEqual(plain(info.categories), []);
  }
});

test('connection failures keep the kind that game-sync and game-state rely on', async () => {
  const network = await failureOf((api) => api.snapshot(SESSION), () => Promise.reject(Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' })));
  const timeout = await failureOf((api) => api.turns(SESSION, 0), () => Promise.reject(Object.assign(new Error('timeout'), { code: 'ECONNABORTED' })));
  const canceled = await failureOf((api) => api.wallet(), () => Promise.reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' })));
  assert.deepEqual([network.kind, network.status], ['network', 0]);
  assert.deepEqual([timeout.kind, timeout.status], ['timeout', 0]);
  assert.deepEqual([canceled.kind, canceled.status], ['canceled', 0]);
});

test('a 404 is reported with its status so the sync engine can clear the cache of a deleted save', async () => {
  const info = await failureOf((api) => api.turns(SESSION, 0), serverError(404, { ok: false, error: { code: 'not_found', message: '找不到這個資源。' } }));
  assert.deepEqual([info.kind, info.status, info.code], ['server', 404, 'not_found']);
});
