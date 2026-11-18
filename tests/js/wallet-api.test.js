// 全站共用的遊戲幣 API 封裝（ui/wallet-api.js）：wallet-hub 1.1。
// 以真的 Auth 與 wallet-api.js（另載入 game-api.js 做對照）、替身 axios（`vm` 沙盒）驗證每個呼叫的方法、路徑、本文與參數，
// 以及失敗物件的固定形狀（簽到預覽、餘額與所需數量；型別不對的值一律不放進來）。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

/* 沙盒：真的 auth-common.js、wallet-api.js 與 game-api.js（對照用）；axios 是替身，記錄每個請求並依 `respond` 回應。 */
function sandboxWith(respond, { withAuth = true } = {}) {
  const requests = [];
  const sandbox = { Promise };
  sandbox.self = sandbox;
  sandbox.axios = (request) => {
    requests.push(JSON.parse(JSON.stringify(request, (key, value) => (key === 'signal' ? undefined : value))));
    requests[requests.length - 1].signal = request.signal;
    return respond(request);
  };
  vm.createContext(sandbox);
  const files = ['app/static/js/ui/wallet-api.js'];
  if (withAuth) { files.unshift('app/static/js/auth/auth-common.js'); files.push('app/static/js/game/game-api.js'); }
  files.forEach((file) => vm.runInContext(read(file), sandbox, { filename: file }));
  return { api: sandbox.UI.walletApi, gameApi: sandbox.Game && sandbox.Game.api, requests };
}

const ok = (data) => () => Promise.resolve({ status: 200, data });
const serverError = (status, body) => () => Promise.reject({ response: { status, data: body } });
const rejection = async (promise) => { try { await promise; } catch (info) { return info; } throw new Error('應該被拒絕'); };

test('the wallet API exposes exactly wallet, checkin and entries', () => {
  const { api } = sandboxWith(ok({}));
  assert.deepEqual(Object.keys(api).sort(), ['checkin', 'entries', 'wallet']);
});

test('wallet reads GET /api/wallet, returns the body untouched and passes the signal and timeout from the caller', async () => {
  const body = { ok: true, wallet: { total: 170 } };
  const controller = new AbortController();
  const { api, requests } = sandboxWith(ok(body));
  assert.deepEqual(plain(await api.wallet()), body);
  assert.deepEqual(requests[0], { method: 'get', url: '/api/wallet', signal: undefined });
  await api.wallet({ signal: controller.signal, timeout: 9000 });
  assert.equal(requests[1].signal, controller.signal);
  assert.equal(requests[1].timeout, 9000);
});

test('checkin posts to /api/wallet/checkin and sends confirm_overflow true only for a real true', async () => {
  const { api, requests } = sandboxWith(ok({ ok: true }));
  await api.checkin();
  await api.checkin(false);
  await api.checkin(true);
  await api.checkin('true');                                          // 不是布林：不算確認
  await api.checkin(1);
  assert.deepEqual(requests.map((request) => request.method), ['post', 'post', 'post', 'post', 'post']);
  assert.ok(requests.every((request) => request.url === '/api/wallet/checkin'));
  assert.deepEqual(requests.map((request) => request.data), [{ confirm_overflow: false }, { confirm_overflow: false }, { confirm_overflow: true }, { confirm_overflow: false }, { confirm_overflow: false }]);
});

test('entries reads GET /api/wallet/entries and only sends a cursor and a limit that are usable integers', async () => {
  const { api, requests } = sandboxWith(ok({ ok: true, entries: [], next_before: null }));
  await api.entries();
  await api.entries(null, undefined);
  await api.entries(120, 20);
  await api.entries(0, 100);
  await api.entries(-1, 0);                                           // 負數與 0 不送（範圍由伺服器驗證，前端不重複實作規則）
  await api.entries('12', '5');                                       // 字串不送
  await api.entries(1.5, 2.5);
  assert.ok(requests.every((request) => request.method === 'get' && request.url === '/api/wallet/entries'));
  assert.deepEqual(requests.map((request) => request.params), [{}, {}, { before: 120, limit: 20 }, { before: 0, limit: 100 }, {}, {}, {}]);
  assert.ok(requests.every((request) => request.data === undefined));
});

test('entries passes the signal and timeout from the caller', async () => {
  const controller = new AbortController();
  const { api, requests } = sandboxWith(ok({ ok: true, entries: [], next_before: null }));
  await api.entries(5, 20, { signal: controller.signal, timeout: 7000 });
  assert.equal(requests[0].signal, controller.signal);
  assert.equal(requests[0].timeout, 7000);
});

test('a 409 checkin_overflow carries the preview as integers, and a malformed preview is dropped', async () => {
  const body = { error: { code: 'checkin_overflow', message: '會有幣作廢' }, checkin: { would_gain: 150, would_lose: 50 } };
  const { api } = sandboxWith(serverError(409, body));
  const info = await rejection(api.checkin(false));
  assert.equal(info.status, 409);
  assert.equal(info.code, 'checkin_overflow');
  assert.deepEqual(plain(info.checkin), { would_gain: 150, would_lose: 50 });
  for (const bad of [{ would_gain: '150', would_lose: 50 }, { would_gain: 150 }, { would_gain: -1, would_lose: 0 }, { would_gain: 1.5, would_lose: 0 }, 'x', null]) {
    const dropped = await rejection(sandboxWith(serverError(409, { error: { code: 'checkin_overflow' }, checkin: bad })).api.checkin(false));
    assert.equal('checkin' in dropped, false, JSON.stringify(bad));
  }
});

test('a 409 insufficient_coins carries the balance and the needed amount as non-negative integers only', async () => {
  const { api } = sandboxWith(serverError(409, { error: { code: 'insufficient_coins' }, balance: 10, needed: 30 }));
  const info = await rejection(api.wallet());
  assert.equal(info.balance, 10);
  assert.equal(info.needed, 30);
  const bad = await rejection(sandboxWith(serverError(409, { error: { code: 'insufficient_coins' }, balance: '10', needed: -30 })).api.wallet());
  assert.equal('balance' in bad, false);
  assert.equal('needed' in bad, false);
});

test('other response fields are never copied into the failure', async () => {
  const body = { error: { code: 'x' }, notebook: { text: '玩家文字' }, categories: ['sexual'], length: 3, capacity: 4, secret: 'abc' };
  const info = await rejection(sandboxWith(serverError(500, body)).api.wallet());
  for (const key of ['notebook', 'categories', 'length', 'capacity', 'secret', 'checkin', 'balance', 'needed']) { assert.equal(key in info, false, key); }
  assert.equal(info.status, 500);
});

test('network failures, timeouts and cancellations are described by Auth.describeError', async () => {
  const network = await rejection(sandboxWith(() => Promise.reject(new Error('x'))).api.wallet());
  assert.equal(network.kind, 'network');
  const timeout = await rejection(sandboxWith(() => Promise.reject({ code: 'ECONNABORTED' })).api.wallet());
  assert.equal(timeout.kind, 'timeout');
  const canceled = await rejection(sandboxWith(() => Promise.reject({ code: 'ERR_CANCELED' })).api.wallet());
  assert.equal(canceled.kind, 'canceled');
});

test('without Auth loaded a failure is still a fixed network-shaped object, never a thrown error', async () => {
  const { api } = sandboxWith(() => Promise.reject(new Error('x')), { withAuth: false });
  const info = await rejection(api.wallet());
  assert.equal(info.kind, 'network');
  assert.equal(info.status, 0);
});

test('the checkin preview and balance fields have the same shape as Game.api for the same errors', async () => {
  const bodies = [
    { error: { code: 'checkin_overflow' }, checkin: { would_gain: 150, would_lose: 50 } },
    { error: { code: 'checkin_overflow' }, checkin: { would_gain: '150', would_lose: 50 } },
    { error: { code: 'insufficient_coins' }, balance: 10, needed: 30 },
    { error: { code: 'insufficient_coins' }, balance: -1, needed: 'x' },
    { error: { code: 'already_checked_in' } },
    { error: { code: 'wallet_busy' }, checkin: null, balance: null },
    undefined,
  ];
  const statuses = [409, 409, 409, 409, 409, 503, 500];
  for (let index = 0; index < bodies.length; index += 1) {
    const mine = await rejection(sandboxWith(serverError(statuses[index], bodies[index])).api.checkin(false));
    const game = await rejection(sandboxWith(serverError(statuses[index], bodies[index])).gameApi.checkin(false));
    const pick = (info) => plain({ status: info.status, code: info.code, kind: info.kind, checkin: info.checkin, balance: info.balance, needed: info.needed });
    assert.deepEqual(pick(mine), pick(game), `body #${index}`);
  }
});

test('the module touches no browser storage, cookie, document or console', () => {
  const source = read('app/static/js/ui/wallet-api.js').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document', 'console', 'cookie']) { assert.equal(source.includes(word), false, word); }
});
