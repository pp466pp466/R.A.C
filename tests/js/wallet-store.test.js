// 全站共用的錢包狀態庫（ui/wallet-store.js）：wallet-hub 1.2。
// 載入成功與失敗、並行合併、已讀到不重複請求、型別不對的資料、`coin_exempt` 只認布林、`setTotal`、失敗後重試、訂閱失敗，以及沒有儲存與輸出。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createWalletStore, walletLabelOf } = require('../../app/static/js/ui/wallet-store.js');

const WALLET = (total, extra) => Object.assign({
  free_balance: total, paid_balance: 0, total, free_cap: 1000, turn_cost: { standard: 30 }, coin_exempt: false,
  checkin: { claimable: true, grant: 200, would_gain: 200, would_lose: 0, next_reset_at: '2026-11-18T16:00:00+00:00' },
}, extra);

/* 替身 fetcher：`script` 是依序回應的陣列（最後一個重複使用）；Error 與 `{ __reject }` 會拒絕；`gate` 讓測試控制回應的時間。 */
function setup(script, options = {}) {
  const calls = [];
  const queue = Array.isArray(script) ? script.slice() : [script];
  const fetcher = (settings) => {
    calls.push(settings);
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (options.gate) { return options.gate.then(() => (next instanceof Error ? Promise.reject(next) : { ok: true, wallet: next })); }
    return next instanceof Error ? Promise.reject(next) : Promise.resolve({ ok: true, wallet: next });
  };
  return { store: createWalletStore({ fetcher }), calls };
}

test('a new store knows nothing: no total, not exempt, nothing requested yet', () => {
  const { store, calls } = setup(WALLET(170));
  assert.equal(store.state.status, 'idle');
  assert.equal(store.state.total, null);
  assert.equal(store.state.exempt, false);
  assert.equal(calls.length, 0);
});

test('load reads the wallet once, becomes ready and keeps every field the label and the pages need', async () => {
  const { store, calls } = setup(WALLET(170, { free_balance: 120, paid_balance: 50 }));
  const loading = store.load();
  assert.equal(store.state.status, 'loading');
  assert.equal(await loading, true);
  assert.equal(store.state.status, 'ready');
  assert.equal(store.state.total, 170);
  assert.equal(store.state.free, 120);
  assert.equal(store.state.paid, 50);
  assert.equal(store.state.freeCap, 1000);
  assert.equal(store.state.turnCost, 30);
  assert.deepEqual(JSON.parse(JSON.stringify(store.state.checkin)), { claimable: true, grant: 200, wouldGain: 200, wouldLose: 0, nextResetAt: '2026-11-18T16:00:00+00:00' });
  assert.equal(calls.length, 1);
});

test('concurrent loads are merged into one request and share the result', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { store, calls } = setup(WALLET(170), { gate });
  const first = store.load();
  const second = store.load();
  const third = store.load({ force: true });                          // 進行中時連強制重讀也只是合併
  assert.equal(first, second);
  assert.equal(second, third);
  release();
  assert.deepEqual(await Promise.all([first, second, third]), [true, true, true]);
  assert.equal(calls.length, 1);
});

test('once ready, load returns at once without another request; force reads again', async () => {
  const { store, calls } = setup([WALLET(170), WALLET(140)]);
  await store.load();
  assert.equal(await store.load(), true);
  assert.equal(await store.load(), true);
  assert.equal(calls.length, 1);
  assert.equal(store.state.total, 170);
  assert.equal(await store.load({ force: true }), true);
  assert.equal(calls.length, 2);
  assert.equal(store.state.total, 140);
});

test('the settings given to load are handed to the fetcher untouched (signal and timeout for the request)', async () => {
  const { store, calls } = setup(WALLET(10));
  const settings = { signal: 'S', timeout: 5 };
  await store.load(settings);
  assert.equal(calls[0], settings);
});

test('a failed load sets status failed, never throws, and can be retried', async () => {
  const { store, calls } = setup([new Error('boom'), WALLET(170)]);
  assert.equal(await store.load(), false);
  assert.equal(store.state.status, 'failed');
  assert.equal(store.state.total, null);
  assert.equal(await store.load(), true);                              // failed 之後再呼叫就是重試
  assert.equal(store.state.status, 'ready');
  assert.equal(store.state.total, 170);
  assert.equal(calls.length, 2);
});

test('a response without a usable wallet counts as a failure', async () => {
  for (const wallet of [undefined, null, 'x', [], { total: 'many' }, { total: -5 }, { total: 1.5 }]) {
    const store = createWalletStore({ fetcher: () => Promise.resolve({ ok: true, wallet }) });
    assert.equal(await store.load(), false, JSON.stringify(wallet));
    assert.equal(store.state.status, 'failed');
    assert.equal(store.state.total, null);
  }
  const noBody = createWalletStore({ fetcher: () => Promise.resolve(undefined) });
  assert.equal(await noBody.load(), false);
  assert.equal(noBody.state.status, 'failed');
});

test('a failed refresh does not wipe the numbers already read', async () => {
  const { store } = setup([WALLET(170), new Error('boom')]);
  await store.load();
  assert.equal(await store.load({ force: true }), false);
  assert.equal(store.state.status, 'ready');
  assert.equal(store.state.total, 170);
});

test('a fetcher that throws synchronously is a failure, not an exception', async () => {
  const store = createWalletStore({ fetcher: () => { throw new Error('sync'); } });
  assert.equal(await store.load(), false);
  assert.equal(store.state.status, 'failed');
});

test('coin_exempt counts only as the boolean true; every snapshot overwrites the flag', async () => {
  for (const value of [undefined, null, 'true', 1, 'yes', {}, [], 0]) {
    const store = createWalletStore({});
    assert.equal(store.apply(WALLET(10, { coin_exempt: value })), true);
    assert.equal(store.state.exempt, false, String(value));
  }
  const store = createWalletStore({});
  store.apply(WALLET(0, { coin_exempt: true }));
  assert.equal(store.state.exempt, true);
  assert.equal(store.state.total, 0);                                  // 管理員的真實餘額（0）照實保留
  store.apply(WALLET(0));                                              // 之後的快照不再豁免：跟著伺服器
  assert.equal(store.state.exempt, false);
});

test('apply takes only non-negative integers and booleans, field by field', () => {
  const store = createWalletStore({});
  assert.equal(store.apply(WALLET(170)), true);
  const bad = {
    total: '999', free_balance: -1, paid_balance: 1.5, free_cap: 'big', turn_cost: { standard: -30 },
    checkin: { claimable: 'yes', grant: -2, would_gain: '3', would_lose: 2.5, next_reset_at: 12 },
  };
  assert.equal(store.apply(bad), false);                               // 合計沒有採用
  assert.equal(store.state.total, 170);
  assert.equal(store.state.free, 170);
  assert.equal(store.state.paid, 0);
  assert.equal(store.state.freeCap, 1000);
  assert.equal(store.state.turnCost, 30);
  assert.deepEqual(JSON.parse(JSON.stringify(store.state.checkin)), { claimable: true, grant: 200, wouldGain: 200, wouldLose: 0, nextResetAt: '2026-11-18T16:00:00+00:00' });
  for (const input of [null, undefined, 'x', 5, []]) { assert.equal(store.apply(input), false); }
  assert.equal(store.state.total, 170);
});

test('a zero turn cost (the exempt account) is not adopted as a price', () => {
  const store = createWalletStore({});
  store.apply(WALLET(0, { coin_exempt: true, turn_cost: { standard: 0 } }));
  assert.equal(store.state.turnCost, null);
});

test('setTotal accepts only a non-negative integer (done.coins) and marks the store ready', () => {
  const store = createWalletStore({});
  assert.equal(store.setTotal(140), true);
  assert.equal(store.state.total, 140);
  assert.equal(store.state.status, 'ready');
  for (const value of [-1, 1.5, '140', NaN, Infinity, null, undefined, {}]) {
    assert.equal(store.setTotal(value), false, String(value));
    assert.equal(store.state.total, 140);
  }
  assert.equal(store.setTotal(0), true);
  assert.equal(store.state.total, 0);
});

test('after setTotal or apply the next load does not request again', async () => {
  const { store, calls } = setup(WALLET(170));
  store.setTotal(140);
  assert.equal(await store.load(), true);
  assert.equal(calls.length, 0);
  assert.equal(store.state.total, 140);
});

test('onFailure tells every subscriber the raw failure of each failed load, whoever started it; unsubscribe stops it', async () => {
  const failure = { kind: 'server', status: 401 };
  const failing = createWalletStore({ fetcher: () => Promise.reject(failure) });
  const seen = [];
  const off = failing.onFailure((info) => seen.push(['a', info]));
  failing.onFailure(() => { throw new Error('a broken subscriber'); });        // 訂閱者的錯誤不影響其他訂閱者與狀態庫
  failing.onFailure((info) => seen.push(['c', info]));
  assert.equal(await failing.load(), false);
  assert.deepEqual(seen.map((entry) => entry[0]), ['a', 'c']);
  assert.equal(seen[0][1], failure);
  off();
  assert.equal(await failing.load(), false);
  assert.deepEqual(seen.map((entry) => entry[0]), ['a', 'c', 'c']);
  assert.equal(typeof failing.onFailure('not a function'), 'function');
});

test('reactive is injected like the toast store: the state it returns is the reactive one', async () => {
  const wrapped = [];
  const store = createWalletStore({ reactive: (value) => { wrapped.push(value); return value; }, fetcher: () => Promise.resolve({ wallet: WALLET(5) }) });
  assert.equal(wrapped.length, 1);
  assert.equal(store.state, wrapped[0]);
  await store.load();
  assert.equal(store.state.total, 5);
});

test('the store touches no browser storage, cookie, console or document', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'ui', 'wallet-store.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document', 'console', 'cookie']) { assert.equal(source.includes(word), false, word); }
});

// ---- 標籤的文字（純函式）-----------------------------------------------------------------------------

test('the label shows the total, and a dash while loading or after a failure', () => {
  assert.deepEqual(walletLabelOf({ status: 'ready', total: 170, exempt: false }), { amount: '170', exempt: false, aria: '遊戲幣餘額 170，前往錢包' });
  assert.equal(walletLabelOf({ status: 'idle', total: null, exempt: false }).amount, '—');
  assert.equal(walletLabelOf({ status: 'loading', total: null, exempt: false }).amount, '—');
  assert.equal(walletLabelOf({ status: 'loading', total: null, exempt: false }).aria, '遊戲幣餘額讀取中，前往錢包');
  assert.equal(walletLabelOf({ status: 'failed', total: null, exempt: false }).amount, '—');
  assert.equal(walletLabelOf({ status: 'failed', total: null, exempt: false }).aria, '遊戲幣餘額無法讀取，前往錢包');
});

test('the label of an exempt account shows the real balance, even 0, plus the note', () => {
  const label = walletLabelOf({ status: 'ready', total: 0, exempt: true });
  assert.equal(label.amount, '0');
  assert.equal(label.exempt, true);
  assert.equal(label.aria, '遊戲幣餘額 0（不消耗遊戲幣），前往錢包');
});

test('the label only trusts a real non-negative integer total and the boolean exempt flag', () => {
  for (const total of ['170', -1, 1.5, NaN, undefined, '<b>1</b>', {}]) {
    assert.equal(walletLabelOf({ status: 'ready', total, exempt: false }).amount, '—', String(total));
  }
  assert.equal(walletLabelOf({ status: 'ready', total: 5, exempt: 'true' }).exempt, false);
  assert.equal(walletLabelOf({ status: 'ready', total: 5, exempt: 1 }).exempt, false);
  assert.deepEqual(walletLabelOf(null), { amount: '—', exempt: false, aria: '遊戲幣餘額讀取中，前往錢包' });
});

test('the label strings are plain text made only from the validated number and fixed words', () => {
  const store = createWalletStore({});
  store.apply({ total: '<img src=x onerror=alert(1)>', coin_exempt: true });
  const label = walletLabelOf(store.state);
  assert.equal(label.amount, '—');
  assert.ok(!/[<>]/.test(label.amount + label.aria));
});
