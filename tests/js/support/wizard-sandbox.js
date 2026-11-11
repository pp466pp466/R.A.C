'use strict';
/* 開存檔精靈的 `vm` 沙盒（wizard.test.js 與 wizard-template.test.js 共用，不是測試檔）：
 * 以真的 Vue、真的 Auth、真的 game-labels.js／wizard-state.js／game-api.js 執行 wizard.js 的 `setup()`，只把 axios、location、儲存物件與 mount 換成替身。
 * - axios 替身記錄每個請求（method、url、data、timeout，以及有沒有帶 AbortSignal），`respond(request)` 回傳 Promise；
 * - `location.assign` 記到 `navigations`；`addEventListener` 記下 pagehide 等處理函式，測試用 `fire('pagehide')` 觸發；
 * - `localStorage`、`sessionStorage`、`indexedDB`、`document.cookie` 都是會記錄寫入的陷阱（`storageWrites`），整個流程結束後斷言它是空的；
 * - `console` 的每個方法都會把輸出記進 `logs`，用來斷言沒有任何玩家文字被輸出。
 * 沒有瀏覽器可以掛載；樣板的文字與結構由 tests/test_game_pages.py 檢查，樣板用到的名稱由 wizard-template.test.js 對照 setup() 的回傳。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const VERSION_ID = '11111111-1111-4111-8111-111111111111';
const SESSION_ID = '99999999-9999-4999-8999-999999999999';
const STATE = { member_tier: 'free', gate: null, suggest_timeout_ms: 35000, start_timeout_ms: 65000 };

/* 伺服器的錯誤回應形狀（controllers/errors.py）：`error` 是 { code, message, fields? }，審查類別 `categories` 在最外層。 */
function serverError(status, code, message, extra) {
  const error = Object.assign({ code, message }, extra && extra.fields ? { fields: extra.fields } : {});
  return { response: { status, data: Object.assign({ ok: false, error }, extra && extra.categories ? { categories: extra.categories } : {}) } };
}
const canceled = () => Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' });

/* 建立沙盒並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, data, timeout, signal }，回傳 Promise。 */
function setupWizard({ state = STATE, pathname = `/game/start/${VERSION_ID}`, respond = () => Promise.reject(new Error('unexpected request')) } = {}) {
  const requests = [];
  const navigations = [];
  const mounted = [];
  const listeners = {};
  const storageWrites = [];
  const logs = [];
  const toasts = [];
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { storageWrites.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { storageWrites.push([name, String(key), value]); return true; },
  });
  const sandbox = { Promise, setTimeout, clearTimeout, AbortController };
  sandbox.console = new Proxy({}, { get: () => (...args) => { logs.push(args.map(String).join(' ')); } });
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); };
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname, search: '' };
  sandbox.localStorage = trap('localStorage');
  sandbox.sessionStorage = trap('sessionStorage');
  sandbox.indexedDB = trap('indexedDB');
  const document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state' ? { textContent: typeof state === 'string' ? state : JSON.stringify(state) } : null),
  };
  Object.defineProperty(document, 'cookie', { get: () => '', set: (value) => { storageWrites.push(['cookie', value]); } });
  sandbox.document = document;
  const axios = (request) => {
    requests.push({ method: request.method, url: request.url, data: request.data === undefined ? undefined : JSON.parse(JSON.stringify(request.data)), timeout: request.timeout, signal: request.signal });
    return respond(request);
  };
  axios.defaults = { headers: { common: {} } };
  sandbox.axios = axios;
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });

  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; },
    onMounted: (hook) => { mounted.push(hook); },
  });
  ['js/auth/auth-common.js', 'js/ui/restriction.js', 'js/game/game-labels.js', 'js/game/wizard-state.js', 'js/game/game-api.js'].forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: (message) => toasts.push(message) });
  run('app/static/js/game/wizard.js');
  assert.ok(options && typeof options.setup === 'function', 'wizard.js 沒有呼叫 createApp');
  const page = options.setup();
  return {
    page, requests, navigations, storageWrites, logs, toasts,
    mount: () => Promise.all(mounted.map((hook) => hook())),
    fire: (type, event) => (listeners[type] || []).forEach((handler) => handler(event)),
    listeners,
  };
}

module.exports = { setupWizard, serverError, canceled, read, ROOT, VERSION_ID, SESSION_ID, STATE };
