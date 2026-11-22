'use strict';
/* script-anonymous-retention 規格的前端部分（帳號頁可設定偏好、刪除帳號視窗再次確認偏好）：
 * ① 純函式與狀態機（account-script-retention.js）：載入、切換、失敗恢復、送出中保護；
 * ② 帳號頁接線：以真的 Vue 與真的 Auth.useForm 執行 account.js 的 setup()，只把 axios、Toast 與 mount 換成替身，
 *    驗證什麼人會載入偏好、刪除視窗的核取方塊與送出的本文、樣板用到的每個名稱都由 setup() 回傳；
 * ③ 刪除請求本文（account-lifecycle.js 的 deletionBody）。沒有瀏覽器可以掛載；真實的點擊流程在瀏覽器驗收。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Retention = require('../../app/static/js/auth/account-script-retention.js');
const Lifecycle = require('../../app/static/js/auth/account-lifecycle.js');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

const answer = (keep, liveCount = 2) => ({ data: { ok: true, keep, live_count: liveCount } });
const serverError = (status, code, message) => ({ response: { status, data: { error: { code, message } } } });
const describe = (error) => (error && error.response
  ? { kind: 'server', message: error.response.data.error.message, fields: {}, code: error.response.data.error.code, status: error.response.status }
  : { kind: 'network', message: '網路連線不穩，請稍後再試。', fields: {}, code: 'network', status: 0 });

function controllerWith({ get = () => Promise.resolve(answer(false)), put = () => Promise.resolve(answer(true)) } = {}) {
  const calls = [];
  const toasts = [];
  const errors = [];
  const controller = Retention.createController({
    request: {
      get: () => { calls.push(['get']); return get(); },
      put: (body) => { calls.push(['put', plain(body)]); return put(body); },
    },
    describeError: describe,
    toast: (message) => toasts.push(message),
    onError: (info) => errors.push(info),
  });
  return { controller, view: controller.view, calls, toasts, errors };
}

// ---- 純函式 -----------------------------------------------------------------------------------------

test('parse accepts only a boolean keep and a non-negative integer live_count', () => {
  assert.deepEqual(Retention.parse({ ok: true, keep: true, live_count: 3 }), { keep: true, liveCount: 3 });
  assert.deepEqual(Retention.parse({ keep: false, live_count: 0 }), { keep: false, liveCount: 0 });
  for (const bad of [null, undefined, 'x', 7, [], {}, { keep: 'yes', live_count: 1 }, { keep: 1, live_count: 1 }, { keep: true }, { keep: true, live_count: -1 },
    { keep: true, live_count: 1.5 }, { keep: true, live_count: '2' }, { keep: true, live_count: null }, { keep: true, live_count: Infinity }]) {
    assert.equal(Retention.parse(bad), null, JSON.stringify(bad));
  }
});

test('the copy is fixed Traditional Chinese text that names the anonymous author and the 12-month rule', () => {
  const text = JSON.stringify(Retention.COPY);
  assert.match(text, /已註銷的作者/);
  assert.match(text, /連續 12 個月未登入/);
  assert.match(text, /草稿、待審、被退回、已下架與舊版本一律刪除/);
  assert.match(text, /仍可被檢舉/);
  assert.equal(Retention.COPY.title, '匿名保留我已發佈的劇本');
  for (const value of [Retention.COPY.title, Retention.COPY.stateOn, Retention.COPY.stateOff, Retention.COPY.deleteNote, ...Retention.COPY.points]) {
    assert.ok(typeof value === 'string' && value !== '' && !/[<>]/.test(value));
  }
});

// ---- 載入 -------------------------------------------------------------------------------------------

test('loading shows the block only after the preference was read, and reads it once', async () => {
  const { controller, view, calls } = controllerWith({ get: () => Promise.resolve(answer(true, 4)) });
  assert.equal(view.available, false);
  assert.equal(await controller.load(), true);
  assert.deepEqual(plain({ available: view.available, keep: view.keep, liveCount: view.liveCount }), { available: true, keep: true, liveCount: 4 });
  await controller.load();
  assert.deepEqual(calls, [['get']]);
});

test('a second load while the first is in flight sends nothing more', async () => {
  let release = null;
  const { controller, calls } = controllerWith({ get: () => new Promise((resolve) => { release = () => resolve(answer(false)); }) });
  const first = controller.load();
  const second = controller.load();
  await new Promise((resolve) => setImmediate(resolve));                                                // 請求在下一個微任務才送出
  release();
  assert.equal(await first, true);
  assert.equal(await second, false);
  assert.equal(calls.length, 1);
});

test('a failed or senseless load leaves the block hidden and hands the failure to onError', async () => {
  for (const respond of [() => Promise.reject(serverError(403, 'admin_script_retention_forbidden', '管理員無法使用。')), () => Promise.reject({ code: 'ERR_NETWORK' }),
    () => Promise.resolve({ data: { ok: true, keep: 'maybe', live_count: 1 } }), () => Promise.resolve({ data: null }), () => Promise.resolve(undefined)]) {
    const { controller, view, errors } = controllerWith({ get: respond });
    assert.equal(await controller.load(), false);
    assert.equal(view.available, false);
    assert.equal(view.error, '');
    assert.ok(errors.length <= 1);
  }
  const { controller, errors } = controllerWith({ get: () => Promise.reject(serverError(401, 'unauthorized', '請先登入。')) });
  await controller.load();
  assert.equal(errors[0].status, 401);                                                                  // 401 交給頁面導向登入
});

test('a load that failed can be tried again later', async () => {
  let fail = true;
  const { controller, view } = controllerWith({ get: () => (fail ? Promise.reject({ code: 'ERR_NETWORK' }) : Promise.resolve(answer(false))) });
  assert.equal(await controller.load(), false);
  fail = false;
  assert.equal(await controller.load(), true);
  assert.equal(view.available, true);
});

// ---- 切換 -------------------------------------------------------------------------------------------

async function ready(options) {
  const setup = controllerWith(options);
  await setup.controller.load();
  setup.calls.length = 0;
  return setup;
}

test('switching on puts keep: true, takes the value the server answered and toasts', async () => {
  const { controller, view, calls, toasts } = await ready({ put: () => Promise.resolve(answer(true, 2)) });
  assert.equal(await controller.toggle(), true);
  assert.deepEqual(calls, [['put', { keep: true }]]);
  assert.equal(view.keep, true);
  assert.equal(view.busy, false);
  assert.deepEqual(toasts, ['已開啟「匿名保留我已發佈的劇本」。']);
});

test('switching off puts keep: false', async () => {
  const { controller, view, calls, toasts } = await ready({ get: () => Promise.resolve(answer(true)), put: () => Promise.resolve(answer(false)) });
  assert.equal(view.keep, true);
  await controller.toggle();
  assert.deepEqual(calls, [['put', { keep: false }]]);
  assert.equal(view.keep, false);
  assert.deepEqual(toasts, ['已關閉「匿名保留我已發佈的劇本」。']);
});

test('the value shown is the one the server answered, not the one asked for', async () => {
  const { controller, view } = await ready({ put: () => Promise.resolve(answer(false, 5)) });
  await controller.toggle();
  assert.equal(view.keep, false);
  assert.equal(view.liveCount, 5);
});

test('a failed save puts the switch back as it was, says so, restores the button and tells onError', async () => {
  const { controller, view, errors, toasts } = await ready({ put: () => Promise.reject(serverError(500, 'internal_error', '伺服器的原文不會被顯示')) });
  assert.equal(await controller.toggle(), false);
  assert.equal(view.keep, false);                                                                       // 沒有樂觀更新
  assert.equal(view.busy, false);
  assert.equal(view.error, '暫時無法儲存，請稍後再試。');
  assert.ok(!view.error.includes('伺服器'));
  assert.equal(errors.length, 1);
  assert.deepEqual(toasts, []);
});

test('a network failure keeps the switch and shows the network message', async () => {
  const { controller, view } = await ready({ put: () => Promise.reject({ code: 'ERR_NETWORK' }) });
  await controller.toggle();
  assert.equal(view.keep, false);
  assert.equal(view.error, '網路連線不穩，請稍後再試。');
});

test('a body that makes no sense is an error and leaves the switch alone', async () => {
  const { controller, view } = await ready({ put: () => Promise.resolve({ data: { ok: true } }) });
  assert.equal(await controller.toggle(), false);
  assert.equal(view.keep, false);
  assert.equal(view.error, '無法確認目前的設定，請重新整理頁面後再試。');
  assert.equal(view.busy, false);
});

test('while saving, pressing again sends nothing; afterwards the switch works again and an old error is cleared', async () => {
  let release = null;
  const { controller, view, calls } = await ready({ put: () => new Promise((resolve) => { release = () => resolve(answer(true)); }) });
  view.error = '舊的錯誤';
  const first = controller.toggle();
  assert.equal(view.busy, true);
  assert.equal(view.error, '');
  assert.equal(await controller.toggle(), false);
  assert.equal(calls.length, 1);
  await new Promise((resolve) => setImmediate(resolve));
  release();
  assert.equal(await first, true);
  assert.equal(view.busy, false);
});

test('before the preference was loaded there is nothing to switch', async () => {
  const { controller, calls } = controllerWith();
  assert.equal(await controller.toggle(), false);
  assert.deepEqual(calls, []);
});

// ---- 刪除請求本文 -----------------------------------------------------------------------------------

test('the deletion body carries keep_published_scripts only when there are live scripts and the value is a boolean', () => {
  const base = { hasPassword: false, confirmText: '刪除我的帳號' };
  assert.deepEqual(Lifecycle.deletionBody({ ...base, keepPublished: true, liveCount: 2 }), { confirm_text: '刪除我的帳號', keep_published_scripts: true });
  assert.deepEqual(Lifecycle.deletionBody({ ...base, keepPublished: false, liveCount: 1 }), { confirm_text: '刪除我的帳號', keep_published_scripts: false });
  assert.deepEqual(Lifecycle.deletionBody({ hasPassword: true, password: 'p', keepPublished: true, liveCount: 3 }), { password: 'p', keep_published_scripts: true });
  for (const liveCount of [0, -1, 1.5, '2', null, undefined, NaN]) {
    assert.deepEqual(Lifecycle.deletionBody({ ...base, keepPublished: true, liveCount }), { confirm_text: '刪除我的帳號' }, String(liveCount));
  }
  for (const keepPublished of ['true', 1, null, undefined]) {
    assert.deepEqual(Lifecycle.deletionBody({ ...base, keepPublished, liveCount: 2 }), { confirm_text: '刪除我的帳號' }, String(keepPublished));
  }
  assert.deepEqual(Lifecycle.deletionBody(base), { confirm_text: '刪除我的帳號' });                    // 沒有給這兩個欄位：與以前完全相同
});

// ---- 帳號頁接線 -------------------------------------------------------------------------------------

const USER = { role: 'user', member_tier: 'free', age_confirmed: true, display_name: '玩家', email: 'a@example.com', story_preference: 'all', has_password: false };
const CONFIRM = '刪除我的帳號';

function setupAccount({ user = USER, get, put, post } = {}) {
  const requests = [];
  const toasts = [];
  const navigations = [];
  const sandbox = { console, Promise, clearTimeout };
  sandbox.setTimeout = () => 1;
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/account', search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state'
      ? { textContent: JSON.stringify({ user, ageRules: {}, deletion: { confirmText: CONFIRM }, contentMode: { effective: 'safe', confirmed: false, forced: false, returnPath: null } }) } : null),
  };
  const record = (method, handler) => (url, body) => { requests.push({ method, url, body: body === undefined ? undefined : plain(body) }); return handler(url, body); };
  sandbox.axios = {
    get: record('get', get || (() => Promise.resolve(answer(false, 2)))),
    put: record('put', put || ((url, body) => Promise.resolve(answer(body.keep, 2)))),
    post: record('post', post || (() => Promise.resolve({ data: { ok: true } }))),
    defaults: { headers: { common: {} } },
  };
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });
  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, { createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; } });
  ['js/auth/auth-common.js', 'js/auth/auth-form.js', 'js/auth/auth-flows.js', 'js/auth/account-lifecycle.js', 'js/auth/account-safe-mode.js', 'js/auth/account-script-retention.js', 'js/ui/restriction.js']
    .forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, { toast: (message) => toasts.push(message), install: () => {} });
  run('app/static/js/auth/account.js');
  assert.ok(options && typeof options.setup === 'function', 'account.js 沒有呼叫 createApp');
  return { page: options.setup(), requests, toasts, navigations };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const gets = (requests) => requests.filter((r) => r.url === '/api/account/script-retention' && r.method === 'get');

test('a regular member who confirmed the age loads the preference once when the page starts and then sees the block', async () => {
  const { page, requests } = setupAccount({ get: () => Promise.resolve(answer(true, 3)) });
  await settle();
  assert.equal(gets(requests).length, 1);
  assert.deepEqual(plain({ available: page.scriptRetention.available, keep: page.scriptRetention.keep, liveCount: page.scriptRetention.liveCount }), { available: true, keep: true, liveCount: 3 });
});

test('an admin, an account awaiting deletion and a member who has not confirmed the age never ask for the preference', async () => {
  for (const user of [{ ...USER, role: 'admin' }, { ...USER, deletion_scheduled_at: '2026-12-01T00:00:00Z' }, { ...USER, age_confirmed: false }]) {
    const { page, requests } = setupAccount({ user });
    await settle();
    assert.equal(requests.length, 0, JSON.stringify(user));
    assert.equal(page.scriptRetention.available, false);
  }
});

test('when the preference cannot be read the block stays hidden and the page still works', async () => {
  const { page, requests } = setupAccount({ get: () => Promise.reject({ response: { status: 500, data: { error: { code: 'internal_error', message: 'x' } } } }) });
  await settle();
  assert.equal(gets(requests).length, 1);
  assert.equal(page.scriptRetention.available, false);
  assert.equal(typeof page.deleteAccount, 'function');
});

test('flipping the switch puts the new value and the delete modal starts from the saved value', async () => {
  const { page, requests, toasts } = setupAccount();
  await settle();
  await page.toggleScriptRetention();
  assert.deepEqual(requests.filter((r) => r.method === 'put').map((r) => [r.url, r.body]), [['/api/account/script-retention', { keep: true }]]);
  assert.equal(page.scriptRetention.keep, true);
  assert.deepEqual(toasts, ['已開啟「匿名保留我已發佈的劇本」。']);
  page.deleteOpen.value = true;
  await settle();
  assert.equal(page.deleteKeep.value, true);
});

test('with live scripts the delete request carries the checkbox value; the player can change it in the modal', async () => {
  const { page, requests, navigations } = setupAccount({ get: () => Promise.resolve(answer(false, 2)) });
  await settle();
  page.deleteOpen.value = true;
  await settle();
  assert.equal(page.deleteKeep.value, false);
  page.deleteKeep.value = true;
  page.deleteForm.values.confirm_text = CONFIRM;
  await page.deleteAccount();
  const posts = requests.filter((r) => r.method === 'post');
  assert.deepEqual(posts.map((r) => [r.url, r.body]), [['/api/account/delete', { confirm_text: CONFIRM, keep_published_scripts: true }]]);
  assert.deepEqual(navigations, ['/login?notice=deletion_requested']);
});

test('without live scripts the delete request has no such field and the modal shows no checkbox', async () => {
  const { page, requests } = setupAccount({ get: () => Promise.resolve(answer(true, 0)) });
  await settle();
  assert.equal(page.scriptRetention.liveCount, 0);
  page.deleteOpen.value = true;
  page.deleteForm.values.confirm_text = CONFIRM;
  await page.deleteAccount();
  assert.deepEqual(requests.filter((r) => r.method === 'post').map((r) => r.body), [{ confirm_text: CONFIRM }]);
});

test('when the preference never loaded the delete request has no such field either', async () => {
  const { page, requests } = setupAccount({ get: () => Promise.reject({ code: 'ERR_NETWORK' }) });
  await settle();
  page.deleteForm.values.confirm_text = CONFIRM;
  await page.deleteAccount();
  assert.deepEqual(requests.filter((r) => r.method === 'post').map((r) => r.body), [{ confirm_text: CONFIRM }]);
});

test('a wrong confirmation sends nothing, so the preference is not touched either', async () => {
  const { page, requests } = setupAccount();
  await settle();
  page.deleteForm.values.confirm_text = '刪除帳號';
  assert.equal(await page.deleteAccount(), false);
  assert.equal(requests.filter((r) => r.method === 'post' || r.method === 'put').length, 0);
});

test('closing the delete modal puts the checkbox back to the saved preference', async () => {
  const { page } = setupAccount({ get: () => Promise.resolve(answer(false, 2)) });
  await settle();
  page.deleteOpen.value = true;
  await settle();
  page.deleteKeep.value = true;
  page.resetDeleteForm();
  assert.equal(page.deleteKeep.value, false);
});

// ---- 樣板 -------------------------------------------------------------------------------------------

test('the account template uses the retention names that setup() returns and shows every text as plain interpolation', () => {
  const html = read('app/templates/auth/account.html');
  const section = html.slice(html.indexOf('id="script-retention-title"') - 200, html.indexOf('id="script-retention-title"') + 2600);
  const modal = html.slice(html.indexOf('v-model="deleteOpen"'));
  const { page } = setupAccount();
  for (const name of ['scriptRetention', 'scriptRetentionCopy', 'toggleScriptRetention', 'deleteKeep']) {
    assert.ok((section + modal).includes(name), `樣板沒有用到 ${name}`);
    assert.ok(name in page, `setup() 沒有回傳 ${name}`);
  }
  for (const [, field] of (section + modal).matchAll(/scriptRetention\.(\w+)/g)) { assert.ok(field in page.scriptRetention, `scriptRetention.${field} 不存在`); }
  for (const [, field] of (section + modal).matchAll(/scriptRetentionCopy\.(\w+)/g)) { assert.ok(field in page.scriptRetentionCopy, `scriptRetentionCopy.${field} 不存在`); }
  assert.match(section, /role="switch"/);
  assert.match(modal, /v-if="scriptRetention\.available && scriptRetention\.liveCount > 0"/);           // 沒有上架劇本就不顯示
  assert.match(modal, /type="checkbox" name="keep_published_scripts" v-model="deleteKeep"/);
  for (const forbidden of ['v-html', 'data-bs-toggle', 'data-bs-dismiss', 'innerHTML']) { assert.ok(!html.includes(forbidden), forbidden); }
});
