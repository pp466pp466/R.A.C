'use strict';
/* 登入後設定精靈（`/age`，age.js）的 `vm` 沙盒（age-wizard.test.js 使用，不是測試檔）：
 * 以真的 Vue、真的 Auth（auth-common.js、auth-form.js、auth-flows.js）與真的 onboarding.js 執行 age.js 的 `setup()`，
 * 只把 axios、location、儲存物件與 mount 換成替身。
 * - axios 替身（`post`／`put`）記錄每個請求，`respond(request)` 回傳 Promise；
 * - `location.assign` 記到 `navigations`；
 * - `localStorage`、`sessionStorage`、`indexedDB`、`document.cookie` 都是會記錄寫入的陷阱（`storageWrites`），整個流程結束後斷言它是空的；
 * - `console` 的每個方法都會把輸出記進 `logs`，用來斷言沒有任何玩家文字被輸出。
 * 沒有瀏覽器可以掛載；樣板的文字與結構由 tests/test_age_page.py 檢查。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const RULES = {
  minRegisterAge: 18, adultAge: 18, birthDateMin: '1900-01-01', birthDateMax: '2026-10-27',
  ranges: [{ code: '18_24', label: '18 至 24 歲' }, { code: '25_34', label: '25 至 34 歲' }],
};
const STATE = { next: '/game', ageRules: RULES };

/* 伺服器的錯誤回應形狀（controllers/errors.py）：`error` 是 { code, message, fields? }。 */
function serverError(status, code, message, fields) {
  const error = Object.assign({ code, message }, fields ? { fields } : {});
  return { response: { status, data: { ok: false, error } } };
}

/* 建立沙盒並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, data }，回傳 Promise。 */
function setupAge({ state = STATE, respond = () => Promise.resolve({ data: { ok: true } }) } = {}) {
  const requests = [];
  const navigations = [];
  const storageWrites = [];
  const logs = [];
  const toasts = [];
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { storageWrites.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { storageWrites.push([name, String(key), value]); return true; },
  });
  const sandbox = { Promise, setTimeout, clearTimeout };
  sandbox.console = new Proxy({}, { get: () => (...args) => { logs.push(args.map(String).join(' ')); } });
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/age', search: '' };
  sandbox.localStorage = trap('localStorage');
  sandbox.sessionStorage = trap('sessionStorage');
  sandbox.indexedDB = trap('indexedDB');
  const document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state' ? { textContent: JSON.stringify(state) } : null),
  };
  Object.defineProperty(document, 'cookie', { get: () => '', set: (value) => { storageWrites.push(['cookie', value]); } });
  sandbox.document = document;
  const call = (method) => (url, data) => {
    requests.push({ method, url, data: data === undefined ? undefined : JSON.parse(JSON.stringify(data)) });
    return respond({ method, url, data });
  };
  sandbox.axios = { post: call('POST'), put: call('PUT'), get: call('GET'), defaults: { headers: { common: {} } } };
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });

  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  const mounts = [];
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { use() {}, component() {}, mount: (target) => mounts.push(target) }; },
  });
  ['js/auth/auth-common.js', 'js/auth/auth-form.js', 'js/auth/auth-flows.js', 'js/auth/onboarding.js'].forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: (message) => toasts.push(message) });
  run('app/static/js/auth/age.js');
  assert.ok(options && typeof options.setup === 'function', 'age.js 沒有呼叫 createApp');
  const page = options.setup();
  return { page, requests, navigations, storageWrites, logs, toasts, mounts, sandbox };
}

module.exports = { setupAge, serverError, read, ROOT, RULES, STATE };
