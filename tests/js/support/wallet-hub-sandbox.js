'use strict';
/* 錢包頁掛載腳本 wallet-hub.js 的 `vm` 沙盒（wallet-hub-template.test.js 使用，不是測試檔；做法同 game-sandbox.js）：
 * 以真的 Vue、真的 Auth、真的 UI 元件與全站的錢包狀態庫、真的簽到狀態機、真的 game-labels.js／game-state.js／wallet-hub-state.js，
 * 執行 wallet-hub.js 的 `setup()`，只把 axios、location、儲存物件、計時器、時鐘與 mount 換成替身。
 * - axios 替身記錄每個請求（method、url、params、data）；`respond(request)` 回傳 Promise；
 * - 時鐘 `clock.now` 與計時器完全由測試控制：`advance(seconds)` 一秒一秒推進時鐘並觸發目前啟動中的計時器（`activeIntervals()` 是還沒被清除的計時器）；
 * - `localStorage`、`sessionStorage`、`indexedDB`、`document.cookie` 是會記錄寫入的陷阱（`storageWrites`），整個流程結束後斷言它是空的；
 *   `console` 的每個方法都會把輸出記進 `logs`；Toast 記在 `toasts`；
 * - `unmount()` 執行元件卸載時的收尾，`fire('pagehide')` 觸發頁面離開的處理函式。沒有瀏覽器可以掛載；樣板的名稱由 wallet-hub-template.test.js 對照 setup() 的回傳。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { settle } = require('./vue-runtime.js');

const ROOT = path.join(__dirname, '..', '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const STATE = { member_tier: 'free' };
const START_MS = Date.UTC(2026, 10, 18, 4, 0, 0);              // 2026-11-18 12:00 台灣時間

const SCRIPTS = [
  'js/auth/auth-common.js', 'js/ui/toast-store.js', 'js/ui/restriction.js', 'js/ui/wallet-api.js', 'js/ui/wallet-store.js', 'js/ui/ui-components.js',
  'js/game/game-labels.js', 'js/game/game-state.js', 'js/ui/wallet-checkin.js', 'js/wallet/wallet-demo-packages.js', 'js/wallet/wallet-hub-state.js',
];

/* 伺服器的錯誤回應形狀（controllers/errors.py）：`error` 是 { code, message }，簽到預覽 `checkin` 在最外層。 */
function serverError(status, code, message, extra) {
  return { response: { status, data: Object.assign({ ok: false, error: { code, message: message || '' } }, extra || {}) } };
}

/* 建立沙盒並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, params, data, timeout }，回傳 Promise。 */
function setupWallet({ state = STATE, pathname = '/wallet', respond = () => Promise.reject(new Error('unexpected request')) } = {}) {
  const requests = [];
  const navigations = [];
  const mounted = [];
  const unmounted = [];
  const listeners = {};
  const storageWrites = [];
  const logs = [];
  const toasts = [];
  const intervals = [];
  const clock = { now: START_MS };
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { storageWrites.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { storageWrites.push([name, String(key), value]); return true; },
  });
  class FakeDate extends Date { static now() { return clock.now; } }
  const sandbox = { Promise, setTimeout, clearTimeout, Date: FakeDate };
  sandbox.console = new Proxy({}, { get: () => (...args) => { logs.push(args.map(String).join(' ')); } });
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); };
  sandbox.setInterval = (handler, ms) => { const handle = intervals.length + 1; intervals.push({ handle, handler, ms, active: true }); return handle; };
  sandbox.clearInterval = (handle) => { const found = intervals.find((item) => item.handle === handle); if (found) { found.active = false; } };
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname, search: '' };
  sandbox.localStorage = trap('localStorage');
  sandbox.sessionStorage = trap('sessionStorage');
  sandbox.indexedDB = trap('indexedDB');
  const document = {
    createElement: () => ({}),
    querySelector: (selector) => (selector === 'meta[name="csrf-token"]' ? { getAttribute: () => 'CSRF-TOKEN' } : null),
    getElementById: (id) => (id === 'initial-state' ? { textContent: typeof state === 'string' ? state : JSON.stringify(state) } : null),
    addEventListener() {}, removeEventListener() {},
    body: { classList: { add() {}, remove() {} } },
  };
  Object.defineProperty(document, 'cookie', { get: () => '', set: (value) => { storageWrites.push(['cookie', value]); } });
  sandbox.document = document;
  const axios = (request) => {
    requests.push({ method: request.method, url: request.url, params: request.params, data: request.data === undefined ? undefined : JSON.parse(JSON.stringify(request.data)), timeout: request.timeout });
    return respond(request);
  };
  axios.defaults = { headers: { common: {} } };
  sandbox.axios = axios;
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read('app/static/' + file), sandbox, { filename: file });

  run('vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; },
    onMounted: (hook) => { mounted.push(hook); },
    onBeforeUnmount: (hook) => { unmounted.push(hook); },
  });
  SCRIPTS.forEach(run);
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: (message) => toasts.push(message) });
  run('js/wallet/wallet-hub.js');
  assert.ok(options && typeof options.setup === 'function', 'wallet-hub.js 沒有呼叫 createApp');
  const page = options.setup();
  const activeIntervals = () => intervals.filter((item) => item.active);
  return {
    page, requests, navigations, storageWrites, logs, toasts, sandbox, clock, intervals, activeIntervals,
    mount: async () => { await Promise.all(mounted.map((hook) => hook())); await settle(); },
    unmount: () => unmounted.forEach((hook) => hook()),
    /* 一秒一秒推進時鐘並觸發啟動中的計時器，每一秒之後讓排程與請求跑完。 */
    advance: async (seconds) => {
      for (let index = 0; index < seconds; index += 1) {
        clock.now += 1000;
        activeIntervals().forEach((item) => item.handler());
        await settle(4);
      }
    },
    fire: (type, event) => (listeners[type] || []).forEach((handler) => handler(event)),
    listeners, settle,
  };
}

module.exports = { setupWallet, serverError, read, ROOT, STATE, START_MS };
