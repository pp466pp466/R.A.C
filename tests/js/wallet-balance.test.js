// 全站的餘額標籤元件（ui-components.js 的 <ui-wallet-balance> 與全站單例 UI.wallet）：wallet-hub 1.3。
// 以真的 Vue、Auth、ui 模組與替身 axios（`vm` 沙盒，沒有瀏覽器可以掛載）：元件的註冊、掛載時只請求一次、載入中與失敗顯示「—」、管理員的「不消耗」、
// 無障礙名稱、樣板是指向 /wallet 的一般連結且純文字渲染，以及沒有任何瀏覽器儲存與主控台輸出。標籤出現在哪些頁面由 tests/test_wallet_badge_pages.py 檢查。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { settle } = require('./support/vue-runtime.js');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const SCRIPTS = [
  'js/auth/auth-common.js', 'js/ui/toast-store.js', 'js/ui/restriction.js', 'js/ui/wallet-api.js', 'js/ui/wallet-store.js', 'js/ui/ui-components.js',
];

/* 沙盒：`respond(request)` 是 axios 的替身；`onMounted` 被換成收集器，元件的 setup() 由測試直接呼叫，再手動執行收集到的掛載函式。 */
function setupLabel(respond) {
  const requests = [];
  const mounted = [];
  const storageWrites = [];
  const logs = [];
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { storageWrites.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { storageWrites.push([name, String(key), value]); return true; },
  });
  const sandbox = { Promise, setTimeout, clearTimeout };
  sandbox.console = new Proxy({}, { get: () => (...args) => { logs.push(args.map(String).join(' ')); } });
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.localStorage = trap('localStorage');
  sandbox.sessionStorage = trap('sessionStorage');
  sandbox.document = {
    createElement: () => ({}), querySelector: () => null, addEventListener() {}, removeEventListener() {}, body: { classList: { add() {}, remove() {} } },
  };
  Object.defineProperty(sandbox.document, 'cookie', { get: () => '', set: (value) => { storageWrites.push(['cookie', value]); } });
  const axios = (request) => { requests.push({ method: request.method, url: request.url }); return respond(request); };
  axios.defaults = { headers: { common: {} } };
  sandbox.axios = axios;
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read('app/static/' + file), sandbox, { filename: file });
  run('vendor/vue/vue.global.prod.js');
  sandbox.Vue = Object.assign({}, sandbox.Vue, { onMounted: (hook) => { mounted.push(hook); } });
  SCRIPTS.forEach(run);
  const components = {};
  sandbox.UI.install({ component: (name, definition) => { components[name] = definition; } });
  return { sandbox, UI: sandbox.UI, requests, mounted, components, storageWrites, logs };
}

const walletReply = (wallet) => () => Promise.resolve({ data: { ok: true, wallet } });
const WALLET = (total, extra) => Object.assign({ total, free_balance: total, paid_balance: 0, free_cap: 1000, turn_cost: { standard: 30 }, coin_exempt: false, checkin: { claimable: true } }, extra);

/* 掛載一個標籤（呼叫 setup() 再執行它註冊的掛載函式）；回傳可讀的標籤狀態。 */
async function mountLabel(env) {
  const before = env.mounted.length;
  const setup = env.components['ui-wallet-balance'].setup();
  env.mounted.slice(before).forEach((hook) => hook());
  await settle();
  return { read: () => ({ amount: setup.label.value.amount, exempt: setup.label.value.exempt, aria: setup.label.value.aria }) };
}

test('UI.install registers the label as ui-wallet-balance, and UI.wallet is the one store of the page', () => {
  const env = setupLabel(walletReply(WALLET(170)));
  assert.ok(env.components['ui-wallet-balance']);
  assert.equal(env.components['ui-wallet-balance'], env.UI.WalletBalance);
  assert.equal(typeof env.UI.wallet.load, 'function');
  assert.equal(env.UI.wallet.state.status, 'idle');                    // 沒有標籤掛載之前不請求（登入頁等沒有標籤的頁面不會讀錢包）
  assert.deepEqual(env.requests, []);
});

test('mounting the label reads the wallet once and shows the total', async () => {
  const env = setupLabel(walletReply(WALLET(170)));
  const label = await mountLabel(env);
  assert.deepEqual(env.requests, [{ method: 'get', url: '/api/wallet' }]);
  assert.deepEqual(label.read(), { amount: '170', exempt: false, aria: '遊戲幣餘額 170，前往錢包' });
});

test('two labels on one page share the same request and the same number', async () => {
  const env = setupLabel(walletReply(WALLET(170)));
  const first = await mountLabel(env);
  const second = await mountLabel(env);
  assert.equal(env.requests.length, 1);
  assert.equal(first.read().amount, '170');
  assert.equal(second.read().amount, '170');
});

test('while the request is pending the label shows a dash and still names the link', async () => {
  const env = setupLabel(() => new Promise(() => {}));
  const label = await mountLabel(env);
  assert.deepEqual(label.read(), { amount: '—', exempt: false, aria: '遊戲幣餘額讀取中，前往錢包' });
});

test('when the read fails (5xx, 403 or a network error) the label shows a dash and nothing is printed or stored', async () => {
  const failures = [
    () => Promise.reject({ response: { status: 503, data: { error: { code: 'wallet_busy' } } } }),
    () => Promise.reject({ response: { status: 403, data: { error: { code: 'age_confirmation_required' } } } }),
    () => Promise.reject(new Error('network')),
  ];
  for (const respond of failures) {
    const env = setupLabel(respond);
    const label = await mountLabel(env);
    assert.deepEqual(label.read(), { amount: '—', exempt: false, aria: '遊戲幣餘額無法讀取，前往錢包' });
    assert.deepEqual(env.logs, []);
    assert.deepEqual(env.storageWrites, []);
  }
});

test('a malformed total is ignored and the label keeps the dash', async () => {
  for (const total of ['170', -3, 1.5, '<b>1</b>', null]) {
    const env = setupLabel(walletReply({ total }));
    const label = await mountLabel(env);
    assert.equal(label.read().amount, '—', String(total));
  }
});

test('an exempt account shows its real balance, even 0, plus the note; a missing or non-boolean flag does not', async () => {
  const exempt = await mountLabel(setupLabel(walletReply(WALLET(0, { coin_exempt: true, turn_cost: { standard: 0 } }))));
  assert.deepEqual(exempt.read(), { amount: '0', exempt: true, aria: '遊戲幣餘額 0（不消耗遊戲幣），前往錢包' });
  for (const flag of [undefined, 'true', 1, null]) {
    const env = setupLabel(walletReply(WALLET(5, { coin_exempt: flag })));
    assert.equal((await mountLabel(env)).read().exempt, false, String(flag));
  }
});

test('the label follows done.coins and a check-in result written to UI.wallet without another request', async () => {
  const env = setupLabel(walletReply(WALLET(200)));
  const label = await mountLabel(env);
  env.UI.wallet.setTotal(170);
  assert.equal(label.read().amount, '170');
  env.UI.wallet.apply(WALLET(370, { checkin: { claimable: false } }));
  assert.equal(label.read().amount, '370');
  assert.equal(env.requests.length, 1);
});

test('the template is a plain link to /wallet with a full accessible name, plain-text interpolation and no Bootstrap interaction', () => {
  const source = read('app/static/js/ui/ui-components.js');
  const block = source.slice(source.indexOf('var WalletBalance'), source.indexOf('// ---- Toast 容器元件'));
  assert.ok(block.includes('<a class="ui-wallet-balance" href="/wallet" :aria-label="label.aria">'));
  assert.ok(block.includes('{{ label.amount }}'));
  assert.ok(block.includes('不消耗'));
  assert.ok(block.includes('v-if="label.exempt"'));
  for (const forbidden of ['v-html', 'innerHTML', 'data-bs-', '@click', 'localStorage', 'sessionStorage', 'indexedDB', 'console.']) { assert.equal(block.includes(forbidden), false, forbidden); }
});

test('the label and the singleton write nothing to browser storage and print nothing', async () => {
  const env = setupLabel(walletReply(WALLET(170)));
  await mountLabel(env);
  env.UI.wallet.setTotal(150);
  assert.deepEqual(env.storageWrites, []);
  assert.deepEqual(env.logs, []);
});

test('a page reload (a new sandbox) reads the wallet again and never shows the number of the previous page', async () => {
  const first = setupLabel(walletReply(WALLET(170)));
  await mountLabel(first);
  const second = setupLabel(() => new Promise(() => {}));
  const label = await mountLabel(second);
  assert.equal(second.requests.length, 1);
  assert.equal(label.read().amount, '—');
});
