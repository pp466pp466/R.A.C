'use strict';
/* 錢包頁的樣板（app/templates/wallet/hub.html）與掛載腳本（wallet-hub.js）：wallet-hub 2.3、2.4。
 *  1. 樣板能用專案自己離線引入的 Vue 編譯；樣板用到的每個名稱，都在「與 wallet-hub.js 的約定」清單內，反過來清單的每個名稱也都被樣板用到、寫在樣板的標頭說明裡；
 *     setup() 實際回傳的正好就是這份清單（以 `vm` 沙盒執行真的 wallet-hub.js）。
 *  2. 頁面的流程（沙盒：真的 Vue、全站錢包狀態庫、簽到狀態機與純函式，只有 axios、計時器與時鐘是替身）：載入只有一個錢包請求、一般玩家與管理員、讀取失敗與重試、
 *     簽到的各情境（一般、超過上限的預覽與取消／領取、已簽到、重複點擊、登入過期）、倒數逐秒遞減、歸零只重讀一次、時鐘不準時 60 秒內不重複、
 *     分頁與紀錄檢視切換與計時器的啟停、離開頁面清除計時器、受限狀態的申訴捷徑，以及整個流程不寫瀏覽器儲存、不輸出主控台、寫入請求只有簽到。
 * 沒有瀏覽器可以掛載；頁面外殼、載入順序與 360px 版面由 tests/test_wallet_page.py 與 Playwright 的驗收量測檢查。
 * 執行：node --test "tests/js/**" */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { setupWallet, serverError, read, START_MS } = require('./support/wallet-hub-sandbox.js');
const { plain } = require('./support/vue-runtime.js');
const H = require('../../app/static/js/wallet/wallet-hub-state.js');
const Demo = require('../../app/static/js/wallet/wallet-demo-packages.js');

const TEMPLATE = 'app/templates/wallet/hub.html';
const CONTRACT = {
  state: [
    'memberText', 'restriction', 'balance', 'view', 'tab', 'checkinCard', 'checkinBusy', 'checkinMessage', 'checkinFailed', 'countdownText', 'signupGift', 'capNote',
    'topup', 'recordsView', 'guide', 'guideOpen',
  ],
  methods: [
    'retryWallet', 'openRecords', 'closeRecords', 'selectTab', 'onTabKeydown', 'claimCheckin', 'confirmOverflow', 'cancelOverflow',
    'loadMoreRecords', 'retryRecords', 'openGuide',
  ],
};
const CONTRACT_NAMES = [].concat(CONTRACT.state, CONTRACT.methods).sort();

// ---- 樣板 ---------------------------------------------------------------------------------------------------------

const JS_WORDS = new Set(['true', 'false', 'null', 'undefined', 'in', 'of', 'typeof']);

/* 取出 Vue 樣板裡所有運算式（插值與 v-*、:、@ 屬性）與元素參照，回傳其中最外層的識別字（不含屬性存取與字串內容）。 */
function templateNames(html) {
  const body = html.slice(html.indexOf('{% raw %}'), html.indexOf('{% endraw %}'));
  const expressions = [];
  const names = new Set();
  for (const match of body.matchAll(/\{\{([\s\S]*?)\}\}/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\s(?:v-if|v-else-if|v-show|v-model|:[\w-]+|@[\w.-]+)="([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sv-for="[^"]*?\sin\s([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sref="(\w+)"/g)) { names.add(match[1]); }
  const aliases = new Set();                                           // v-for 的迴圈變數（pack、item…）是樣板自己的區域名稱，不是 setup() 的回傳
  for (const match of body.matchAll(/\sv-for="\(?\s*(\w+)(?:\s*,\s*(\w+))?\s*\)?\s+in\s/g)) { aliases.add(match[1]); if (match[2]) { aliases.add(match[2]); } }
  for (const expression of expressions) {
    const stripped = expression.replace(/'[^']*'/g, "''");
    for (const match of stripped.matchAll(/(?<![\w.$])([A-Za-z_$][\w$]*)/g)) { names.add(match[1]); }
  }
  return [...names].filter((name) => !JS_WORDS.has(name) && !aliases.has(name)).sort();
}

/* 與 tests/js/wizard-template.test.js 相同的做法：編譯器在瀏覽器裡用 DOM 解開 HTML 實體，本範本的屬性沒有使用實體，以原樣回傳的替身滿足它。 */
function loadVueWithCompiler() {
  const sandbox = { console };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  sandbox.document = {
    createElement: () => ({
      set innerHTML(value) { this.html = value; },
      get textContent() { return this.html; },
      get children() {
        const match = /^<div foo="([\s\S]*)">$/.exec(this.html || '');
        return [{ getAttribute: () => (match ? match[1].replace(/&quot;/g, '"') : null) }];
      },
    }),
  };
  vm.createContext(sandbox);
  vm.runInContext(read('app/static/vendor/vue/vue.global.prod.js') + '\n;this.__Vue = Vue;', sandbox);
  return sandbox.__Vue;
}

function rawTemplate() {
  const html = read(TEMPLATE);
  const start = html.indexOf('{% raw %}');
  const end = html.lastIndexOf('{% endraw %}');
  assert.ok(start >= 0 && end > start, '找不到 raw 區塊');
  return '<div>' + html.slice(start + '{% raw %}'.length, end) + '</div>';
}

test('the wallet template compiles with the offline Vue', () => {
  assert.equal(typeof loadVueWithCompiler().compile(rawTemplate()), 'function');
});

test('a broken expression would be caught by the same check (self test)', () => {
  const Vue = loadVueWithCompiler();
  assert.throws(() => Vue.compile('<div :a="b +"></div>'));
  assert.throws(() => Vue.compile('<div @click="go("></div>'));
});

test('the template uses exactly the names of the contract with wallet-hub.js, no more and no fewer', () => {
  const used = templateNames(read(TEMPLATE));
  assert.ok(used.length >= 15, `只掃到 ${used.length} 個名稱，掃描可能是空洞的：${used}`);
  const unknown = used.filter((name) => !CONTRACT_NAMES.includes(name));
  assert.deepEqual(unknown, [], `樣板用到約定清單沒有的名稱：${unknown}`);
  const unused = CONTRACT_NAMES.filter((name) => !used.includes(name));
  assert.deepEqual(unused, [], `約定清單有樣板沒用到的名稱：${unused}`);
});

test('every name of the contract is written in the header comment of the template, where wallet-hub.js work starts from', () => {
  const html = read(TEMPLATE);
  const header = html.slice(0, html.indexOf('{% block title'));
  assert.ok(header.includes('與 wallet-hub.js 的約定'));
  const missing = CONTRACT_NAMES.filter((name) => !new RegExp(`(?<![\\w])${name}(?![\\w])`).test(header));
  assert.deepEqual(missing, [], `標頭說明缺少：${missing}`);
});

test('setup() returns exactly the names of the contract', () => {
  const env = setupWallet({ respond: () => Promise.reject(new Error('no request expected before mount')) });
  assert.deepEqual(Object.keys(env.page).sort(), CONTRACT_NAMES);
  assert.deepEqual(env.requests, []);                                  // 還沒掛載：沒有任何請求
});

test('the template holds no request, storage, console or global access, no markup rendering, and all texts are plain interpolations', () => {
  const body = read(TEMPLATE);
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'axios', 'fetch(', 'console.', 'location.', 'window.', 'document.']) {
    assert.ok(!body.includes(word), word);
  }
  assert.ok(!/v-html|innerHTML|\|\s*safe/.test(body));
});

test('the tabs follow the ARIA tabs pattern: ids, controls, labels and a roving tab index that match', () => {
  const html = read(TEMPLATE);
  const ids = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]));
  for (const match of html.matchAll(/aria-controls="([^"]+)"/g)) { assert.ok(ids.has(match[1]), `aria-controls 指向不存在的 ${match[1]}`); }
  const panels = [...html.matchAll(/<section[^>]*role="tabpanel"[^>]*>/g)].map((match) => match[0]);
  assert.equal(panels.length, 2);
  for (const panel of panels) { assert.ok(ids.has(/aria-labelledby="([^"]+)"/.exec(panel)[1]), panel); }
  const tabs = [...html.matchAll(/<button[^>]*role="tab"[^>]*>/g)].map((match) => match[0]);
  assert.equal(tabs.length, 2);
  for (const tab of tabs) { assert.ok(/:tabindex="tab === '(free|topup)' \? 0 : -1"/.test(tab) && /:aria-selected=/.test(tab) && /@click="selectTab\('(free|topup)'\)"/.test(tab), tab); }
  assert.ok(/role="tablist"[^>]*@keydown="onTabKeydown"/.test(html));
});

test('the tabs and the check-in buttons are driven by Vue state only: no Bootstrap plugin attribute, and the records entrance is a plain button', () => {
  const html = read(TEMPLATE);
  assert.ok(!/data-bs-|offcanvas|bootstrap\.js/i.test(html));
  assert.ok(/<button type="button" class="ui-btn" @click="openRecords">紀錄<\/button>/.test(html));
  assert.ok(/<button type="button" class="ui-btn" @click="closeRecords">返回<\/button>/.test(html));
  assert.ok(/<button[^>]*:disabled="checkinBusy"[^>]*@click="claimCheckin"/.test(html));       // 簽到進行中按鈕禁用
  assert.ok(/<button[^>]*:disabled="checkinBusy"[^>]*@click="cancelOverflow"/.test(html));
  assert.ok(/<button[^>]*:disabled="checkinBusy"[^>]*@click="confirmOverflow"/.test(html));
  assert.ok(/<button[^>]*:disabled="balance\.loading"[^>]*@click="retryWallet"/.test(html));
});

test('the sign-up gift card has no button and no number, the cap note comes from state, and the countdown is a timer element', () => {
  const html = read(TEMPLATE);
  const card = html.slice(html.indexOf('wh-signup-title"'), html.indexOf('</article>', html.indexOf('wh-signup-title"')));
  assert.ok(!/<button|<a /.test(card));
  assert.ok(card.includes('{{ signupGift }}'));
  assert.ok(html.includes('{{ capNote }}') && html.includes('role="timer"') && html.includes('{{ countdownText }}'));
  assert.ok(!/(?<![\w.-])\d+(?![\w.-])/.test(card.replace(/id="[^"]*"/g, '').replace(/class="[^"]*"/g, '')), '註冊贈幣的數量不寫死在樣板');
});

test('the stylesheet keeps the cards, the tab bar and long text inside a 360px screen', () => {
  const css = read('app/static/css/wallet.css');
  assert.ok(/\.wh-topbar\s*\{[^}]*flex-wrap:\s*wrap/.test(css));
  assert.ok(/\.wh-cards\s*\{[^}]*display:\s*grid/.test(css) && /minmax\(min\(100%,\s*\d+px\),\s*1fr\)/.test(css));        // 單欄時不超出容器
  assert.ok(/\.wh-card__actions\s*\{[^}]*flex-wrap:\s*wrap/.test(css));
  assert.ok(/\.wh-card__text\s*\{[^}]*overflow-wrap:\s*anywhere/.test(css));
  assert.ok(/\.wh-balance__number\s*\{[^}]*overflow-wrap:\s*anywhere/.test(css));
  assert.ok(!/(?<![\w-])(?:width|min-width):\s*\d{3,}px/.test(css));
});

// ---- 頁面的流程 -------------------------------------------------------------------------------------------------

const HOUR = 3600 * 1000;
const iso = (ms) => new Date(ms).toISOString();
const WALLET = (total, checkin, extra) => Object.assign({
  total, free_balance: total, paid_balance: 0, free_cap: 1000, turn_cost: { standard: 30 }, coin_exempt: false,
  checkin: Object.assign({ claimable: true, grant: 200, would_gain: 200, would_lose: 0, next_reset_at: iso(START_MS + 12 * HOUR) }, checkin),
}, extra);
const ok = (data) => Promise.resolve({ data });
const networkError = () => Promise.reject(Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' }));

/* 依路徑回應：`wallets`、`checkins`、`entries` 是依序的回應，最後一個會一直重複；每個可以是回應本文（成功）、函式（自己回傳 Promise）、Error 或 serverError（失敗）。 */
function server({ wallets = [], checkins = [], entries = [] }) {
  const queues = { '/api/wallet': wallets, '/api/wallet/checkin': checkins, '/api/wallet/entries': entries };
  const counters = { '/api/wallet': 0, '/api/wallet/checkin': 0, '/api/wallet/entries': 0 };
  return (request) => {
    const queue = queues[request.url];
    if (!queue || queue.length === 0) { return Promise.reject(new Error('unexpected request ' + request.method + ' ' + request.url)); }
    const item = queue[Math.min(counters[request.url]++, queue.length - 1)];
    if (typeof item === 'function') { return item(); }
    return item && item.response ? Promise.reject(item) : ok(item);
  };
}

const gets = (env) => env.requests.filter((request) => request.method === 'get' && request.url === '/api/wallet');
const posts = (env) => env.requests.filter((request) => request.method === 'post');
const entryGets = (env) => env.requests.filter((request) => request.method === 'get' && request.url === '/api/wallet/entries');

/* 帳本的一列（API 的形狀）；`created_at` 從 START_MS 往前每個序號一分鐘，所以台灣時間是 2026-11-18 12:00 往前。 */
const ROW = (seq, label, free, paid, extra) => Object.assign({
  seq, kind: 'test', label, pending: false, free_delta: free, paid_delta: paid, created_at: iso(START_MS - seq * 60000),
}, extra);
const PAGE = (rows, next) => ({ ok: true, entries: rows, next_before: next === undefined ? null : next });

async function open(config) {
  const env = setupWallet({ respond: server(config) });
  await env.mount();
  return env;
}

test('opening the page asks for the wallet exactly once and shows the balance, the split, the claim card, the sign-up note and the cap', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(850) }] });
  assert.equal(gets(env).length, 1);
  assert.deepEqual(env.requests.map((request) => [request.method, request.url]), [['get', '/api/wallet']]);
  const { page } = env;
  assert.equal(page.memberText.value, '一般');
  assert.equal(page.tab.value, 'free');
  assert.equal(page.view.value, 'main');
  assert.deepEqual(plain(page.balance.value), { amountText: '850', splitText: '付費 0｜贈送 850', exempt: false, failed: false, loading: false, aria: '遊戲幣餘額 850' });
  assert.deepEqual(plain(page.checkinCard.value), { mode: 'claimable', headline: '每日簽到可領取 200 遊戲幣。' });
  assert.match(page.signupGift.value, /自動發放/);
  assert.match(page.capNote.value, /1000/);
  assert.equal(page.checkinBusy.value, false);
  assert.equal(page.restriction.value, null);
  assert.equal(env.activeIntervals().length, 0);                       // 可以簽到時沒有倒數，也就沒有計時器
});

test('the paid and the free balance are shown apart', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(1230, {}, { free_balance: 800, paid_balance: 430 }) }] });
  assert.equal(env.page.balance.value.splitText, '付費 430｜贈送 800');
});

test('an admin sees the real balance, the not-consumed note and the sign-up explanation for admins', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(0, {}, { coin_exempt: true }) }] });
  assert.equal(env.page.balance.value.amountText, '0');
  assert.equal(env.page.balance.value.exempt, true);
  assert.equal(env.page.signupGift.value, '管理員帳號不發放註冊贈幣。');
});

test('a failed read shows a dash and a retry, never a guessed number, and the retry loads the wallet again', async () => {
  const env = await open({ wallets: [serverError(503, 'wallet_busy', 'busy'), { ok: true, wallet: WALLET(120) }] });
  assert.deepEqual(plain(env.page.balance.value), { amountText: '—', splitText: '', exempt: false, failed: true, loading: false, aria: '遊戲幣餘額無法讀取' });
  assert.equal(env.page.checkinCard.value.mode, 'failed');
  assert.equal(env.page.signupGift.value, '');
  assert.equal(env.page.restriction.value, null);
  await env.page.retryWallet();
  assert.equal(gets(env).length, 2);
  assert.equal(env.page.balance.value.amountText, '120');
  assert.equal(env.page.balance.value.failed, false);
  assert.equal(env.page.checkinCard.value.mode, 'claimable');
});

test('pressing retry twice while it is loading sends one request', async () => {
  let release = null;
  const env = await open({ wallets: [networkError, () => new Promise((resolve) => { release = () => resolve({ data: { ok: true, wallet: WALLET(5) } }); })] });
  assert.equal(env.page.balance.value.failed, true);
  const first = env.page.retryWallet();
  const second = env.page.retryWallet();
  assert.equal(env.page.balance.value.loading, true);                  // 載入中：重試按鈕禁用
  release();
  await Promise.all([first, second]);
  assert.equal(gets(env).length, 2);
});

test('a locked account gets the appeal shortcut instead of a number', async () => {
  const env = await open({ wallets: [serverError(403, 'account_locked', 'locked')] });
  assert.equal(env.page.restriction.value, 'locked');
  assert.equal(env.page.balance.value.amountText, '—');
  assert.deepEqual(env.navigations, []);
});

test('an expired login on load goes to the login page with the wallet as the way back, exactly once', async () => {
  const env = await open({ wallets: [serverError(401, 'unauthorized', 'login')] });
  assert.deepEqual(env.navigations, ['/login?next=%2Fwallet']);
});

// ---- 簽到 ---------------------------------------------------------------------------------------------------------

test('a plain check-in posts once without confirm_overflow, updates the shared balance, shows a toast and turns the card into a countdown', async () => {
  const done = WALLET(1050, { claimable: false, next_reset_at: iso(START_MS + 12 * HOUR) }, { free_balance: 1050 });
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(850) }], checkins: [{ ok: true, wallet: done }] });
  assert.equal(await env.page.claimCheckin(), true);
  await env.settle();
  assert.deepEqual(posts(env).map((request) => [request.url, request.data]), [['/api/wallet/checkin', { confirm_overflow: false }]]);
  assert.equal(env.page.balance.value.amountText, '1050');
  assert.deepEqual(env.toasts, ['簽到成功。目前餘額 1050 幣。']);
  assert.equal(env.page.checkinMessage.value, '簽到成功。目前餘額 1050 幣。');
  assert.equal(env.page.checkinCard.value.mode, 'done');
  assert.equal(env.page.checkinFailed.value, false);
  assert.equal(env.page.checkinBusy.value, false);
  assert.equal(env.page.countdownText.value, '12:00:00');
  assert.equal(gets(env).length, 1);                                   // 簽到回應已帶錢包：不必再讀
  assert.equal(env.activeIntervals().length, 1);                       // 看得到倒數：計時器啟動
});

test('over the free-coin cap the card shows a preview; cancel sends nothing, claim sends confirm_overflow', async () => {
  const overflow = serverError(409, 'checkin_overflow', 'overflow', { checkin: { would_gain: 50, would_lose: 150 } });
  const done = WALLET(1000, { claimable: false });
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(950) }], checkins: [overflow, overflow, { ok: true, wallet: done }] });
  await env.page.claimCheckin();
  assert.equal(env.page.checkinCard.value.mode, 'preview');
  assert.match(env.page.checkinMessage.value, /50/);
  assert.match(env.page.checkinMessage.value, /150/);
  assert.equal(env.page.checkinFailed.value, false);                   // 預覽不是錯誤
  assert.equal(posts(env).length, 1);

  assert.equal(await env.page.claimCheckin(), false);                  // 預覽等待選擇時再按領取：不送請求
  assert.equal(posts(env).length, 1);

  assert.equal(env.page.cancelOverflow(), true);                       // 取消：不送第二次請求，不消耗當日簽到
  assert.equal(posts(env).length, 1);
  assert.equal(env.page.checkinCard.value.mode, 'claimable');
  assert.equal(env.page.checkinMessage.value, '已取消，今天的簽到還沒有使用。');

  await env.page.claimCheckin();
  assert.equal(env.page.checkinCard.value.mode, 'preview');
  await env.page.confirmOverflow();
  await env.settle();
  assert.deepEqual(posts(env).map((request) => request.data.confirm_overflow), [false, false, true]);   // 只有「領取」才帶 confirm_overflow
  assert.equal(env.page.balance.value.amountText, '1000');
  assert.equal(env.page.checkinCard.value.mode, 'done');
});

test('already checked in today turns the card into the countdown and leaves the balance alone', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(77) }], checkins: [serverError(409, 'already_checked_in', 'again')] });
  await env.page.claimCheckin();
  await env.settle();
  assert.equal(env.page.balance.value.amountText, '77');
  assert.equal(env.page.checkinCard.value.mode, 'done');
  assert.match(env.page.checkinMessage.value, /今天已經簽到/);
  assert.equal(env.page.checkinFailed.value, false);
  assert.equal(env.page.countdownText.value, '12:00:00');
  assert.deepEqual(env.toasts, []);
});

test('pressing check-in twice quickly sends one request, the button is disabled meanwhile and recovers afterwards', async () => {
  let release = null;
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(10) }],
    checkins: [() => new Promise((resolve) => { release = () => resolve({ data: { ok: true, wallet: WALLET(210, { claimable: false }) } }); })],
  });
  const first = env.page.claimCheckin();
  const second = env.page.claimCheckin();
  assert.equal(env.page.checkinBusy.value, true);
  release();
  assert.deepEqual(await Promise.all([first, second]), [true, false]);
  assert.equal(posts(env).length, 1);
  assert.equal(env.page.checkinBusy.value, false);
});

test('a network failure, a timeout and a busy server keep the card claimable, say so as a warning, and recover for the next try', async () => {
  const timeout = () => Promise.reject(Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }));
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(10) }],
    checkins: [networkError, timeout, serverError(503, 'wallet_busy', 'busy'), { ok: true, wallet: WALLET(210, { claimable: false }) }],
  });
  for (const pattern of [/無法連線/, /逾時/, /較忙碌/]) {
    await env.page.claimCheckin();
    assert.equal(env.page.checkinFailed.value, true);
    assert.match(env.page.checkinMessage.value, pattern);
    assert.equal(env.page.checkinCard.value.mode, 'claimable');          // 可以再試
    assert.equal(env.page.checkinBusy.value, false);
  }
  await env.page.claimCheckin();
  assert.equal(env.page.checkinFailed.value, false);                      // 下一次嘗試開始時清掉警示
  assert.equal(env.page.checkinCard.value.mode, 'done');
});

test('an expired login during check-in goes to the login page once and does not show a failure card', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(10) }], checkins: [serverError(401, 'unauthorized', 'login')] });
  await env.page.claimCheckin();
  assert.deepEqual(env.navigations, ['/login?next=%2Fwallet']);
  assert.equal(env.page.checkinBusy.value, false);
});

test('a locked account that tries to check in gets the appeal shortcut', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(10) }], checkins: [serverError(403, 'account_locked', 'locked')] });
  await env.page.claimCheckin();
  assert.equal(env.page.restriction.value, 'locked');
});

// ---- 倒數 ---------------------------------------------------------------------------------------------------------

test('the countdown shows hours:minutes:seconds and counts down once a second while the free tab is visible', async () => {
  const reset = START_MS + ((16 * 60 + 43) * 60 + 26) * 1000;
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(reset) }) }] });
  assert.equal(env.page.checkinCard.value.mode, 'done');
  assert.equal(env.page.countdownText.value, '16:43:26');
  assert.equal(env.activeIntervals().length, 1);
  assert.equal(env.activeIntervals()[0].ms, 1000);
  await env.advance(1);
  assert.equal(env.page.countdownText.value, '16:43:25');
  await env.advance(3);
  assert.equal(env.page.countdownText.value, '16:43:22');
  assert.equal(gets(env).length, 1);                                   // 倒數期間不重讀錢包
});

test('a tab that was throttled and wakes up much later shows the right remaining time at the next tick', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + 10 * HOUR) }) }] });
  env.clock.now += 3 * HOUR + 17 * 1000;                               // 背景分頁幾小時都沒有計時
  env.activeIntervals().forEach((item) => item.handler());
  await env.settle();
  assert.equal(env.page.countdownText.value, '06:59:43');
});

test('reaching zero reloads the wallet exactly once, and when it says claimable the card offers the claim and the timer stops', async () => {
  const env = await open({
    wallets: [
      { ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + 3000) }) },
      { ok: true, wallet: WALLET(5, { claimable: true, next_reset_at: iso(START_MS + 24 * HOUR) }) },
    ],
  });
  assert.equal(env.page.countdownText.value, '00:00:03');
  await env.advance(2);
  assert.equal(gets(env).length, 1);
  assert.equal(env.page.countdownText.value, '00:00:01');
  await env.advance(1);
  assert.equal(gets(env).length, 2);                                   // 歸零：重讀一次
  assert.equal(env.page.checkinCard.value.mode, 'claimable');
  assert.equal(env.activeIntervals().length, 0);                       // 可以簽到了：不再有計時器
  await env.advance(120);
  assert.equal(gets(env).length, 2);                                   // 之後也不再空轉
});

test('after a successful check-in the new day still opens the claim: the countdown reload overrides what this page remembered', async () => {
  const env = await open({
    wallets: [
      { ok: true, wallet: WALLET(10, { claimable: true, next_reset_at: iso(START_MS + 2000) }) },
      { ok: true, wallet: WALLET(210, { claimable: true, next_reset_at: iso(START_MS + 24 * HOUR) }) },
    ],
    checkins: [{ ok: true, wallet: WALLET(210, { claimable: false, next_reset_at: iso(START_MS + 2000) }) }],
  });
  await env.page.claimCheckin();
  await env.settle();
  assert.equal(env.page.checkinCard.value.mode, 'done');
  await env.advance(2);
  assert.equal(gets(env).length, 2);
  assert.equal(env.page.checkinCard.value.mode, 'claimable');          // 新的一天：又可以領取（簽到狀態機的「今天簽過了」記憶被更新的快照取代）
  assert.equal(env.page.checkinMessage.value, '');
});

test('a successful check-in never looks like "reached zero" while the new snapshot is still being written into the store', async () => {
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(10, { claimable: true, next_reset_at: iso(START_MS - 1000) }) }],       // 舊快照的下次時間已經過了（時鐘誤差）
    checkins: [{ ok: true, wallet: WALLET(210, { claimable: false, next_reset_at: iso(START_MS + 12 * HOUR) }) }],
  });
  await env.page.claimCheckin();
  await env.settle();
  assert.equal(gets(env).length, 1);                                   // 狀態庫一欄一欄寫入的中途（已簽到但下次時間還是舊的）不會被當成歸零而多讀一次
  assert.equal(env.page.countdownText.value, '12:00:00');
  assert.equal(env.page.checkinMessage.value, '簽到成功。目前餘額 210 幣。');   // 說明沒有被重讀之後的重設清掉
  assert.equal(env.activeIntervals().length, 1);
});

test('when the local clock is ahead and the reload still says not claimable, it does not reload again for 60 seconds, then once more', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + 2000) }) }] });
  await env.advance(2);
  assert.equal(gets(env).length, 2);                                   // 歸零那一秒：重讀一次
  assert.equal(env.page.countdownText.value, '00:00:00');
  assert.equal(env.page.checkinCard.value.mode, 'done');               // 伺服器仍說今天不能簽
  await env.advance(59);
  assert.equal(gets(env).length, 2);                                   // 60 秒內不重複請求
  await env.advance(1);
  assert.equal(gets(env).length, 3);                                   // 滿 60 秒才再讀一次
  await env.advance(30);
  assert.equal(gets(env).length, 3);
});

test('a reload at zero that fails does not hammer the server: the next try waits the same 60 seconds', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + 1000) }) }, networkError] });
  await env.advance(1);
  assert.equal(gets(env).length, 2);
  await env.advance(58);
  assert.equal(gets(env).length, 2);
  await env.advance(2);
  assert.equal(gets(env).length, 3);
  assert.equal(env.page.balance.value.amountText, '5');                // 重讀失敗不洗掉已讀到的數字
});

test('a wallet without a usable next reset time shows no countdown and starts no timer', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: 'not a date' }) }] });
  assert.equal(env.page.checkinCard.value.mode, 'done');
  assert.equal(env.page.countdownText.value, '');
  assert.equal(env.activeIntervals().length, 0);
});

// ---- 分頁與檢視 -----------------------------------------------------------------------------------------------------

test('switching tabs sends no request, the keyboard moves between them, and an unknown tab is ignored', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }] });
  env.page.selectTab('topup');
  assert.equal(env.page.tab.value, 'topup');
  env.page.selectTab('nonsense');
  assert.equal(env.page.tab.value, 'topup');
  const prevented = [];
  env.page.onTabKeydown({ key: 'ArrowLeft', preventDefault: () => prevented.push('left') });
  assert.equal(env.page.tab.value, 'free');
  env.page.onTabKeydown({ key: 'End', preventDefault: () => prevented.push('end') });
  assert.equal(env.page.tab.value, 'topup');
  env.page.onTabKeydown({ key: 'a', preventDefault: () => prevented.push('a') });
  assert.equal(env.page.tab.value, 'topup');
  assert.deepEqual(prevented, ['left', 'end']);                        // 不處理的按鍵不攔截
  env.page.onTabKeydown(undefined);                                    // 壞掉的事件不拋出
  assert.equal(env.requests.length, 1);                                // 只有載入時的那一個請求
});

test('the records view hides the tabs, remembers the tab, and going back returns to it', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [PAGE([])] });
  env.page.selectTab('topup');
  await env.page.openRecords();
  assert.deepEqual([env.page.view.value, env.page.tab.value], ['records', 'topup']);
  env.page.selectTab('free');
  assert.equal(env.page.tab.value, 'topup');                           // 紀錄檢視裡分頁列是隱藏的
  env.page.closeRecords();
  assert.deepEqual([env.page.view.value, env.page.tab.value], ['main', 'topup']);
  assert.deepEqual(env.requests.map((request) => request.url), ['/api/wallet', '/api/wallet/entries']);   // 錢包一次，紀錄第一頁一次
});

test('the timer runs only while the countdown is visible: leaving the free tab or opening the records stops it, coming back starts it again', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + HOUR) }) }] });
  assert.equal(env.activeIntervals().length, 1);
  env.page.selectTab('topup');
  await env.settle();
  assert.equal(env.activeIntervals().length, 0);
  env.page.selectTab('free');
  await env.settle();
  assert.equal(env.activeIntervals().length, 1);
  env.page.openRecords();
  await env.settle();
  assert.equal(env.activeIntervals().length, 0);
  env.page.closeRecords();
  await env.settle();
  assert.equal(env.activeIntervals().length, 1);
  assert.equal(env.intervals.length, 3);                               // 每次回來都是新的計時器，不會同時有兩個
});

test('leaving the page clears the timer, both when the component unmounts and when the page is hidden', async () => {
  const unmounted = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + HOUR) }) }] });
  assert.equal(unmounted.activeIntervals().length, 1);
  unmounted.unmount();
  assert.equal(unmounted.activeIntervals().length, 0);
  const hidden = await open({ wallets: [{ ok: true, wallet: WALLET(5, { claimable: false, next_reset_at: iso(START_MS + HOUR) }) }] });
  assert.equal(hidden.activeIntervals().length, 1);
  hidden.fire('pagehide');
  assert.equal(hidden.activeIntervals().length, 0);
});

// ---- 儲值（示意）與使用說明 -------------------------------------------------------------------------------------------

test('the top-up tab shows the demo notice and one card per demo package, with every number taken from the data module', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }] });
  const view = plain(env.page.topup.value);
  assert.equal(view.demo, true);
  assert.ok(view.notice.includes('示意，非正式價格'));
  assert.equal(view.cards.length, Demo.PACKAGES.length);
  Demo.PACKAGES.forEach((pack, index) => {
    const card = view.cards[index];
    assert.equal(card.id, pack.id);
    assert.equal(card.coinsText, `${H.groupDigits(pack.coins)} 幣`);
    assert.equal(card.priceText, `${Demo.CURRENCY}${H.groupDigits(pack.price)}`);
    assert.equal(card.bonusText, pack.bonus > 0 ? `另贈 ${H.groupDigits(pack.bonus)} 幣` : '');
    assert.equal(card.turnsText, `約可進行 ${Math.floor((pack.coins + pack.bonus) / 30)} 輪冒險`);      // 每輪幣價取自錢包（WALLET 的 turn_cost 是 30）
  });
  assert.equal(env.requests.length, 1);                                // 只有載入錢包的那一個請求
});

test('an admin has no per-turn price, so the cards say nothing about how many turns a package gives', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(0, {}, { coin_exempt: true, turn_cost: { standard: 0 } }) }] });
  assert.ok(env.page.topup.value.cards.length > 0);
  assert.ok(env.page.topup.value.cards.every((card) => card.turnsText === ''));
});

test('the top-up tab needs no request to be shown, switching to it and back sends nothing, and there is no purchase method', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }] });
  env.page.selectTab('topup');
  env.page.selectTab('free');
  env.page.selectTab('topup');
  assert.equal(env.requests.length, 1);
  assert.equal(env.page.tab.value, 'topup');
  assert.deepEqual(Object.keys(env.page).filter((name) => /buy|purchase|pay|order|topup[A-Z]|checkout/i.test(name)), []);
});

test('the top-up panel has a disabled "coming soon" button per card and no click binding, link or form at all', () => {
  const html = read(TEMPLATE);
  const panel = html.slice(html.indexOf('id="wh-panel-topup"'), html.indexOf('<!-- ===== 紀錄'));
  assert.ok(panel.includes('v-for="pack in topup.cards"'));
  assert.ok(!/@click|v-on:|href=|<form|<a |<input|<select|type="submit"/.test(panel), panel);
  const buttons = [...panel.matchAll(/<button[^>]*>/g)].map((match) => match[0]);
  assert.equal(buttons.length, 1);                                     // 迴圈裡的一顆，每張卡片各一顆
  assert.ok(/\sdisabled(?=[\s>])/.test(buttons[0]) && !/:disabled/.test(buttons[0]));          // 靜態的 disabled，不是會被改成可點的動態綁定
  assert.ok(panel.includes('>即將推出</button>'));
  assert.ok(panel.includes('{{ topup.notice }}'));                     // 分頁頂端的說明
  const card = panel.slice(panel.indexOf('<article'), panel.indexOf('</article>'));
  assert.ok(card.includes('示意，非正式價格'));                           // 每張卡片自己也標示
  assert.ok(!/(?<![\w.-])\d+(?![\w.-])/.test(card.replace(/class="[^"]*"/g, '')), '樣板裡沒有幣數、贈送數與價格');
});

test('the usage guide opens in the shared modal from a top bar button, states only settled rules and never a price or a way to pay', async () => {
  const html = read(TEMPLATE);
  assert.ok(/<button type="button" class="wh-topbar__link wh-topbar__button" @click="openGuide">使用說明<\/button>/.test(html));
  assert.ok(html.indexOf('href="/account">帳號</a>') < html.indexOf('@click="openGuide"'));            // 在「帳號」旁邊（它之後）
  assert.ok(/<ui-modal v-model="guideOpen" title="使用說明" status-code="GUIDE"/.test(html));
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5, {}, { free_cap: 1000 }) }] });
  assert.equal(env.page.guideOpen.value, false);
  env.page.openGuide();
  assert.equal(env.page.guideOpen.value, true);
  const lines = plain(env.page.guide.value);
  assert.ok(lines.includes('每一輪冒險消耗 30 遊戲幣。'));
  assert.ok(lines.some((line) => line.includes('先扣贈送幣，再扣付費幣')));
  assert.ok(lines.some((line) => line.includes('不會過期')));
  assert.ok(lines.some((line) => line.includes('持有上限') && line.includes('1000')));
  assert.ok(lines.every((line) => !/價格|付款|付費購買|退款|訂閱|儲值|NT\$|元(?![\w])|折扣/.test(line)), lines.join('\n'));
  assert.equal(env.requests.length, 1);
});

test('the usage guide tells an admin that turns cost nothing, and states no number before the wallet is read', async () => {
  const admin = await open({ wallets: [{ ok: true, wallet: WALLET(0, {}, { coin_exempt: true, turn_cost: { standard: 0 } }) }] });
  const adminLines = plain(admin.page.guide.value);
  assert.ok(adminLines.includes('管理員帳號進行冒險時不消耗遊戲幣。'));
  assert.ok(adminLines.every((line) => !line.includes('每一輪冒險消耗')));
  const failed = await open({ wallets: [networkError] });
  const failedLines = plain(failed.page.guide.value);
  assert.equal(failedLines.length, 2);                                 // 只有不依賴錢包的兩條規則
  assert.ok(failedLines.every((line) => !/\d/.test(line)));
});

// ---- 紀錄檢視 -----------------------------------------------------------------------------------------------------

test('entering the records asks for the first page once and lists it newest first with signs, both balances and Taipei time', async () => {
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [PAGE([
      ROW(5, '冒險回合', -30, 0, { pending: true }), ROW(4, '冒險回合', -30, -10), ROW(3, '每日簽到', 200, 0), ROW(2, '註冊贈送', 200, 0), ROW(1, '管理員調整', 0, 1500),
    ])],
  });
  assert.equal(env.page.recordsView.value.mode, 'loading');             // 還沒進入紀錄：沒有請求，畫面上也看不到
  assert.deepEqual(entryGets(env), []);
  const loading = env.page.openRecords();
  assert.equal(env.page.recordsView.value.mode, 'loading');             // 讀取中
  await loading;
  assert.equal(entryGets(env).length, 1);
  assert.deepEqual(plain(entryGets(env)[0].params), {});                       // 第一頁沒有游標與筆數參數
  const view = plain(env.page.recordsView.value);
  assert.equal(view.mode, 'list');
  assert.equal(view.moreMode, 'end');
  assert.deepEqual(view.items.map((item) => [item.key, item.label, item.pending, item.parts, item.timeText]), [
    [5, '冒險回合', true, ['贈送 −30'], '2026-11-18 11:55'],
    [4, '冒險回合', false, ['贈送 −30', '付費 −10'], '2026-11-18 11:56'],
    [3, '每日簽到', false, ['贈送 +200'], '2026-11-18 11:57'],
    [2, '註冊贈送', false, ['贈送 +200'], '2026-11-18 11:58'],
    [1, '管理員調整', false, ['付費 +1,500'], '2026-11-18 11:59'],
  ]);
});

test('"load more" asks for the next page with the cursor, puts it after the list, and shows the end when there is no cursor', async () => {
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [PAGE([ROW(30, '冒險回合', -30, 0), ROW(29, '每日簽到', 200, 0)], 29), PAGE([ROW(28, '註冊贈送', 200, 0), ROW(29, '每日簽到', 200, 0)])],
  });
  await env.page.openRecords();
  assert.equal(env.page.recordsView.value.moreMode, 'more');
  await env.page.loadMoreRecords();
  assert.equal(entryGets(env).length, 2);
  assert.deepEqual(plain(entryGets(env)[1].params), { before: 29 });
  const view = plain(env.page.recordsView.value);
  assert.deepEqual(view.items.map((item) => item.key), [30, 29, 28]);   // 接在後面，重複的序號只留一列
  assert.equal(view.moreMode, 'end');
  assert.equal(await env.page.loadMoreRecords(), false);               // 沒有游標了：不送請求
  assert.equal(entryGets(env).length, 2);
});

test('pressing "load more" twice quickly sends one request, and the button shows it is loading meanwhile', async () => {
  let release = null;
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [PAGE([ROW(9, '冒險回合', -30, 0)], 9), () => new Promise((resolve) => { release = () => resolve({ data: PAGE([ROW(8, '每日簽到', 200, 0)]) }); })],
  });
  await env.page.openRecords();
  const first = env.page.loadMoreRecords();
  const second = env.page.loadMoreRecords();
  assert.equal(env.page.recordsView.value.moreMode, 'loading');
  release();
  assert.deepEqual(await Promise.all([first, second]), [true, false]);
  assert.equal(entryGets(env).length, 2);
  assert.equal(env.page.recordsView.value.items.length, 2);
  assert.equal(env.page.recordsView.value.moreMode, 'end');
});

test('an empty ledger shows the empty state and no "load more"', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [PAGE([])] });
  await env.page.openRecords();
  assert.deepEqual(plain(env.page.recordsView.value), { mode: 'empty', items: [], errorText: '', moreMode: 'end' });
});

test('an admin turn with a zero amount is listed with a 0 change', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(0, {}, { coin_exempt: true }) }], entries: [PAGE([ROW(2, '冒險回合', 0, 0)])] });
  await env.page.openRecords();
  assert.deepEqual(plain(env.page.recordsView.value.items[0].parts), ['0']);
});

test('a failed first page shows the error with a retry and no list; going back and in again does not ask by itself; retry asks again', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [networkError, PAGE([ROW(1, '註冊贈送', 200, 0)])] });
  await env.page.openRecords();
  let view = plain(env.page.recordsView.value);
  assert.equal(view.mode, 'failed');
  assert.match(view.errorText, /無法連線/);
  assert.deepEqual(view.items, []);
  env.page.closeRecords();
  await env.page.openRecords();
  assert.equal(entryGets(env).length, 1);                              // 失敗後返回再進入：等玩家按「重試」
  assert.equal(env.page.recordsView.value.mode, 'failed');
  await env.page.retryRecords();
  assert.equal(entryGets(env).length, 2);
  view = plain(env.page.recordsView.value);
  assert.equal(view.mode, 'list');
  assert.equal(view.errorText, '');
});

test('a timeout, a busy server and a broken answer each give their own explanation and never show a half ledger', async () => {
  const timeout = () => Promise.reject(Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }));
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [timeout, serverError(503, 'wallet_busy', 'busy'), { ok: true, entries: [ROW(2, '冒險回合', -30, 0), { seq: 1, label: '壞掉的一列' }], next_before: null }, serverError(500, 'boom', 'boom')],
  });
  await env.page.openRecords();
  assert.match(env.page.recordsView.value.errorText, /逾時/);
  await env.page.retryRecords();
  assert.match(env.page.recordsView.value.errorText, /較忙碌/);
  await env.page.retryRecords();
  assert.equal(env.page.recordsView.value.mode, 'failed');             // 有一列壞掉：整頁當作失敗，不顯示少一筆的帳本
  assert.deepEqual(plain(env.page.recordsView.value.items), []);
  await env.page.retryRecords();
  assert.match(env.page.recordsView.value.errorText, /暫時無法讀取/);
});

test('a failed "load more" keeps the loaded list, says so with a retry, and the retry uses the same cursor', async () => {
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [PAGE([ROW(20, '冒險回合', -30, 0)], 20), networkError, PAGE([ROW(19, '每日簽到', 200, 0)])],
  });
  await env.page.openRecords();
  await env.page.loadMoreRecords();
  let view = plain(env.page.recordsView.value);
  assert.equal(view.mode, 'list');
  assert.equal(view.moreMode, 'error');
  assert.match(view.errorText, /無法連線/);
  assert.deepEqual(view.items.map((item) => item.key), [20]);           // 已載入的清單還在
  await env.page.retryRecords();
  assert.deepEqual(plain(entryGets(env).map((request) => request.params)), [{}, { before: 20 }, { before: 20 }]);
  view = plain(env.page.recordsView.value);
  assert.deepEqual(view.items.map((item) => item.key), [20, 19]);
  assert.equal(view.moreMode, 'end');
  assert.equal(view.errorText, '');
});

test('going back and entering the records again does not ask the server again', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [PAGE([ROW(1, '註冊贈送', 200, 0)])] });
  await env.page.openRecords();
  env.page.closeRecords();
  await env.page.openRecords();
  env.page.closeRecords();
  await env.page.openRecords();
  assert.equal(entryGets(env).length, 1);
  assert.equal(env.page.recordsView.value.items.length, 1);
});

test('leaving while the first page is still loading and coming back does not send a second request', async () => {
  let release = null;
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    entries: [() => new Promise((resolve) => { release = () => resolve({ data: PAGE([ROW(1, '註冊贈送', 200, 0)]) }); })],
  });
  const loading = env.page.openRecords();
  env.page.closeRecords();
  await env.page.openRecords();
  release();
  await loading;
  assert.equal(entryGets(env).length, 1);
  assert.equal(env.page.recordsView.value.mode, 'list');
});

test('a successful check-in drops the loaded ledger so the next visit shows the new entry', async () => {
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    checkins: [{ ok: true, wallet: WALLET(205, { claimable: false }) }],
    entries: [PAGE([ROW(1, '註冊贈送', 200, 0)]), PAGE([ROW(2, '每日簽到', 200, 0), ROW(1, '註冊贈送', 200, 0)])],
  });
  await env.page.openRecords();
  env.page.closeRecords();
  await env.page.claimCheckin();
  await env.settle();
  await env.page.openRecords();
  assert.equal(entryGets(env).length, 2);                              // 簽到之後再進入：重新讀
  assert.deepEqual(plain(env.page.recordsView.value.items).map((item) => item.key), [2, 1]);
});

test('an answer that arrives after a check-in dropped the ledger is ignored, never shown as if it were current', async () => {
  let release = null;
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    checkins: [{ ok: true, wallet: WALLET(205, { claimable: false }) }],
    entries: [() => new Promise((resolve) => { release = () => resolve({ data: PAGE([ROW(1, '舊的一頁', 200, 0)]) }); }), PAGE([ROW(2, '每日簽到', 200, 0)])],
  });
  const stale = env.page.openRecords();
  env.page.closeRecords();
  await env.page.claimCheckin();
  await env.settle();
  release();
  assert.equal(await stale, false);
  await env.settle();
  assert.deepEqual(plain(env.page.recordsView.value.items), []);
  await env.page.openRecords();
  assert.deepEqual(plain(env.page.recordsView.value.items).map((item) => item.label), ['每日簽到']);
});

test('a failure that arrives after a check-in dropped the ledger is ignored too: the next visit still reads the first page', async () => {
  let fail = null;
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(5) }],
    checkins: [{ ok: true, wallet: WALLET(205, { claimable: false }) }],
    entries: [
      () => new Promise((_resolve, reject) => { fail = () => reject(Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' })); }),
      PAGE([ROW(2, '每日簽到', 200, 0)]),
    ],
  });
  const stale = env.page.openRecords();
  env.page.closeRecords();
  await env.page.claimCheckin();
  await env.settle();
  fail();
  assert.equal(await stale, false);
  await env.settle();
  await env.page.openRecords();                                        // 舊請求的失敗沒有把狀態弄成「失敗」：仍然會重新讀第一頁
  assert.equal(entryGets(env).length, 2);
  assert.deepEqual(plain(env.page.recordsView.value.items).map((item) => item.label), ['每日簽到']);
});

test('an expired login while reading the ledger goes to the login page once; a locked account gets the appeal shortcut', async () => {
  const expired = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [serverError(401, 'unauthorized', 'login')] });
  await expired.page.openRecords();
  assert.deepEqual(expired.navigations, ['/login?next=%2Fwallet']);
  const locked = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [serverError(403, 'account_locked', 'locked')] });
  await locked.page.openRecords();
  assert.equal(locked.page.restriction.value, 'locked');
  assert.equal(locked.page.recordsView.value.mode, 'failed');
});

test('the records list shows ledger text as plain text only, and the template never renders markup', async () => {
  const html = read(TEMPLATE);
  const records = html.slice(html.indexOf('id="wh-panel-records"'), html.indexOf('<!-- ===== 使用說明'));
  for (const field of ['item.label', 'item.timeText', 'part', 'recordsView.errorText']) { assert.ok(records.includes(`{{ ${field} }}`), field); }
  assert.ok(!/v-html|innerHTML/.test(html));
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], entries: [PAGE([ROW(1, '<b>標記</b>', 200, 0)])] });
  await env.page.openRecords();
  assert.equal(env.page.recordsView.value.items[0].label, '<b>標記</b>');           // 原樣的文字，由 {{ }} 以純文字顯示
});

// ---- 整頁的守門 -----------------------------------------------------------------------------------------------------

test('a whole visit writes to no browser storage, prints nothing, and the only request that is not a GET is the check-in', async () => {
  const overflow = serverError(409, 'checkin_overflow', 'overflow', { checkin: { would_gain: 50, would_lose: 150 } });
  const env = await open({
    wallets: [{ ok: true, wallet: WALLET(950) }, { ok: true, wallet: WALLET(1000, { claimable: false, next_reset_at: iso(START_MS + 2000) }) }],
    checkins: [overflow, { ok: true, wallet: WALLET(1000, { claimable: false, next_reset_at: iso(START_MS + 2000) }) }],
    entries: [PAGE([ROW(2, '每日簽到', 200, 0), ROW(1, '註冊贈送', 200, 0)], 1), PAGE([ROW(0, '註冊贈送', 0, 0)])],
  });
  await env.page.claimCheckin();
  env.page.cancelOverflow();
  await env.page.claimCheckin();
  await env.page.confirmOverflow();
  env.page.selectTab('topup');
  env.page.selectTab('free');
  await env.page.openRecords();
  await env.page.loadMoreRecords();
  env.page.closeRecords();
  env.page.openGuide();
  await env.advance(5);
  await env.page.retryWallet();
  env.unmount();
  assert.deepEqual(env.storageWrites, []);
  assert.deepEqual(env.logs, []);
  const writes = env.requests.filter((request) => request.method !== 'get');
  assert.ok(writes.length >= 2);
  assert.deepEqual([...new Set(writes.map((request) => `${request.method} ${request.url}`))], ['post /api/wallet/checkin']);       // 寫入請求只有簽到
  assert.ok(env.requests.every((request) => ['/api/wallet', '/api/wallet/checkin', '/api/wallet/entries'].includes(request.url)));
  assert.equal(entryGets(env).length, 2);                              // 紀錄只用 GET：第一頁與載入更多
});

test('the page reads nothing from the URL and sends nothing but the confirm flag', async () => {
  const env = await open({ wallets: [{ ok: true, wallet: WALLET(5) }], checkins: [{ ok: true, wallet: WALLET(205, { claimable: false }) }] });
  await env.page.claimCheckin();
  for (const request of env.requests) { assert.ok(!request.url.includes('?') && request.params === undefined, JSON.stringify(request)); }
  assert.deepEqual(posts(env).map((request) => Object.keys(request.data)), [['confirm_overflow']]);
});
