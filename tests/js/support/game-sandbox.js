'use strict';
/* 遊戲主畫面掛載腳本 game.js 的 `vm` 沙盒（game.test.js 與 game-template.test.js 共用，不是測試檔；做法同 wizard-sandbox.js）：
 * 以真的 Vue、真的 Auth、真的 UI 元件與全部真的 game-*.js 模組，執行 game.js 的 `setup()`，只把 axios、location、儲存物件、計時與 mount 換成替身。
 * - `innerWidth` 可指定（桌面與手機的側欄案例）；`resize` 等事件用 `fire('resize')` 觸發，`document` 的事件（keydown）用 `fireDocument('keydown', event)`；
 * - axios 替身記錄每個請求；`localStorage`、`sessionStorage`、`document.cookie` 是會記錄寫入的陷阱（`storageWrites`），整個流程結束後斷言它是空的；
 *   `indexedDB` 在沙盒裡不存在（`Game.store.openStore` 因此退回記憶體，不碰任何瀏覽器儲存）；
 * - `console` 的每個方法都會把輸出記進 `logs`。沒有瀏覽器可以掛載；樣板的文字與結構由 tests/test_game_pages.py 檢查，樣板的名稱由 game-template.test.js 對照。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const SESSION_ID = '99999999-9999-4999-8999-999999999999';
const STATE = {
  session: { id: SESSION_ID, title: '青雲問道', content_tier: 'safe', ended: false, player_name: '林初' },
  user: { public_id: 'user-public-1', member_tier: 'free' },
  gate: null,
  content_mode: { effective: 'standard', is_adult: true },
  stream: { idle_timeout_ms: 45000, total_timeout_ms: 180000 },
  limits: { action_max: 1000 },
};

const SCRIPTS = [
  'js/auth/auth-common.js', 'js/ui/typewriter.js', 'js/ui/toast-store.js', 'js/ui/restriction.js', 'js/ui/ui-components.js',
  'js/game/game-labels.js', 'js/game/sse-parser.js', 'js/game/turn-stream.js', 'js/game/game-state.js', 'js/game/game-store.js', 'js/game/game-sync.js',
  'js/game/game-api.js', 'js/game/game-wallet.js', 'js/game/game-session.js',
];

/* 建立沙盒並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, data, timeout, signal }，回傳 Promise。 */
function setupGame({ state = STATE, pathname = `/game/${SESSION_ID}`, width = 1280, online = true, respond = () => Promise.reject(new Error('unexpected request')) } = {}) {
  const requests = [];
  const navigations = [];
  const mounted = [];
  const listeners = {};
  const documentListeners = {};
  const storageWrites = [];
  const logs = [];
  const toasts = [];
  const fetches = [];
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { storageWrites.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { storageWrites.push([name, String(key), value]); return true; },
  });
  const sandbox = { Promise, setTimeout, clearTimeout, AbortController, TextDecoder, Uint8Array };
  sandbox.console = new Proxy({}, { get: () => (...args) => { logs.push(args.map(String).join(' ')); } });
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.innerWidth = width;
  sandbox.navigator = { onLine: online };
  sandbox.addEventListener = (type, handler) => { (listeners[type] = listeners[type] || []).push(handler); };
  sandbox.fetch = (...args) => { fetches.push(args); return Promise.reject(new TypeError('network')); };
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname, search: '' };
  sandbox.localStorage = trap('localStorage');
  sandbox.sessionStorage = trap('sessionStorage');
  const document = {
    createElement: () => ({}),
    querySelector: (selector) => (selector === 'meta[name="csrf-token"]' ? { getAttribute: () => 'CSRF-TOKEN' } : null),
    getElementById: (id) => (id === 'initial-state' ? { textContent: typeof state === 'string' ? state : JSON.stringify(state) } : null),
    addEventListener: (type, handler) => { (documentListeners[type] = documentListeners[type] || []).push(handler); },
    removeEventListener: () => {},
    body: { classList: { add() {}, remove() {} } },
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
  const run = (file) => vm.runInContext(read('app/static/' + file), sandbox, { filename: file });

  run('vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; },
    onMounted: (hook) => { mounted.push(hook); },
  });
  SCRIPTS.forEach(run);
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: (message) => toasts.push(message) });
  run('js/game/game.js');
  assert.ok(options && typeof options.setup === 'function', 'game.js 沒有呼叫 createApp');
  const page = options.setup();
  return {
    page, requests, navigations, storageWrites, logs, toasts, fetches, sandbox,
    mount: () => Promise.all(mounted.map((hook) => hook())),
    fire: (type, event) => (listeners[type] || []).forEach((handler) => handler(event)),
    fireDocument: (type, event) => (documentListeners[type] || []).forEach((handler) => handler(event)),
    listeners, documentListeners,
  };
}

module.exports = { setupGame, read, ROOT, STATE, SESSION_ID };
