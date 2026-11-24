'use strict';
/* 後台頁面掛載腳本 admin.js 的 `vm` 沙盒（admin.test.js 與 admin-delete-user.test.js 使用，不是測試檔；做法同 wallet-hub-sandbox.js）：
 * 以真的 Vue、真的 Auth 與真的 admin-format.js 在 `vm` 沙盒執行 admin.js 的 setup()，只把 axios、Toast、捲動與 mount 換成替身。
 * - axios 替身記錄每個請求（method、url、params、data）；`routes(request)` 回傳 Promise（或同步值），沒有對應的路由就拒絕，讓測試明確看到多餘的請求；
 *   回傳 `fail(status, code, message)`（或帶欄位的 `failWith`）時以伺服器的錯誤回應形狀拒絕；
 * - Toast 記在 `toasts`，導頁記在 `navigations`。沒有瀏覽器可以掛載；真實的點擊流程在瀏覽器驗收。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

/* 伺服器的錯誤回應形狀（controllers/errors.py）：`error` 是 { code, message, fields? }。 */
const err = (status, code, message, fields) => ({ response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } } });
const fail = (status, code, message, fields) => ({ __reject: true, error: err(status, code, message, fields) });

function setupAdmin(routes) {
  const requests = [];
  const toasts = [];
  const navigations = [];
  const mounted = [];
  const sandbox = { console, Promise, setTimeout: () => 1, clearTimeout };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.scrollTo = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/admin', search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state' ? { textContent: JSON.stringify({ admin: { display_name: '管理員' } }) } : null),
  };
  sandbox.axios = {
    request: (config) => {
      const record = { method: config.method, url: config.url, params: config.params ? plain(config.params) : undefined, data: config.data === undefined ? undefined : plain(config.data) };
      requests.push(record);
      const result = routes(record);
      return result === undefined ? Promise.reject(err(500, 'no_route', `沒有路由：${record.method} ${record.url}`)) : Promise.resolve(result).then((value) => (value && value.__reject ? Promise.reject(value.error) : { data: value }));
    },
    defaults: { headers: { common: {} } },
  };
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });
  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { component() {}, use() {}, mount() {} }; },
    onMounted: (hook) => { mounted.push(hook); },
  });
  run('app/static/js/auth/auth-common.js');
  run('app/static/js/admin/admin-format.js');
  sandbox.UI = { install: () => {}, toast: (message, settings) => toasts.push({ message, type: settings && settings.type ? settings.type : 'info' }) };
  run('app/static/js/admin/admin.js');
  assert.ok(options && typeof options.setup === 'function', 'admin.js 沒有呼叫 createApp');
  const page = options.setup();
  const gets = () => requests.filter((r) => r.method === 'get').map((r) => `${r.url}${r.params && r.params.status ? '?status=' + r.params.status : ''}`);
  return { page, requests, toasts, navigations, gets, mount: () => Promise.all(mounted.map((hook) => hook())) };
}

module.exports = { setupAdmin, err, fail, read, plain };
