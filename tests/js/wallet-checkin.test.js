'use strict';
/* 每日簽到的狀態機（ui/wallet-checkin.js）：wallet-hub 2.1。
 * 案例與 game-wallet.test.js 現有的簽到案例一一對應（一般、超過上限、取消、領取、已簽到、重複點擊、401、429、503、網路錯誤），
 * 另外驗證 hooks 的呼叫時機、沒有 hooks 與沒有注入失敗整理函式時的保底，以及 wallet-hub 2.2 的「兩處行為一致」對照
 * （同一組輸入餵給 game-wallet.js 與 createCheckin，預覽內容、請求次數、餘額與簽到按鈕狀態相同）。
 * 以真的 Vue（tests/js/support/vue-runtime.js）、真的錢包狀態庫與真的 `Game.describeCheckinFailure` 執行，只有 API 是替身。
 * 執行：node --test "tests/js/**" */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadVue, plain, ROOT } = require('./support/vue-runtime.js');
const { createCheckin } = require('../../app/static/js/ui/wallet-checkin.js');
const { createWalletStore } = require('../../app/static/js/ui/wallet-store.js');
const Game = require('../../app/static/js/game/game-state.js');
const { createWallet } = require('../../app/static/js/game/game-wallet.js');

const Vue = loadVue();
const WALLET = (total, extra) => ({
  free_balance: total, paid_balance: 0, total, free_cap: 1000, turn_cost: { standard: 30 },
  checkin: Object.assign({ claimable: true, grant: 200, would_gain: 200, would_lose: 0, next_reset_at: '2026-11-12T16:00:00+00:00' }, extra),
});
const serverFailure = (status, code, extra) => Object.assign({ kind: 'server', status, code, message: '', fields: {}, categories: [] }, extra);
const transportFailure = (kind) => ({ kind, status: 0, code: '', message: '', fields: {}, categories: [] });
const rejects = (info) => ({ __reject: info });
const overflowFailure = (gain, lose) => rejects(serverFailure(409, 'checkin_overflow', { checkin: { would_gain: gain, would_lose: lose } }));
const describe = (info, options) => Game.describeCheckinFailure(info, options);

const respond = (queue) => {
  const next = queue.length > 1 ? queue.shift() : queue[0];
  return next instanceof Error || (next && next.__reject) ? Promise.reject(next.__reject || next) : Promise.resolve(next);
};

/* 簽到狀態機加上共用的錢包狀態庫與替身 API。`events` 依序記錄每個 hook 的呼叫；`hooks` 可以覆寫或補上。 */
function setup({ wallet = [], checkin = [], hooks, describeFailure = describe } = {}) {
  const calls = { wallet: [], checkin: [] };
  const events = [];
  const store = createWalletStore({
    reactive: Vue.reactive,
    fetcher: (settings) => { calls.wallet.push(settings); return respond(wallet); },
  });
  const api = { checkin: (confirm) => { calls.checkin.push(confirm); return respond(checkin); } };
  const recording = {
    onFailure: (info) => events.push(['failure', info.status]),
    onLogin: () => events.push(['login']),
    onOverflow: (failure) => events.push(['overflow', failure.kind]),
    onAlready: (failure) => events.push(['already', failure.kind]),
    onError: (failure) => events.push(['error', failure.kind]),
  };
  const target = createCheckin({ Vue: { ref: Vue.ref }, api, store, describeFailure, hooks: hooks === undefined ? recording : Object.assign({}, recording, hooks) });
  return { checkin: target, store, calls, events };
}

// ---- 一般簽到 -------------------------------------------------------------------------------------------------

test('a plain check-in sends no confirm_overflow, writes the wallet into the shared store, and says it succeeded', async () => {
  const { checkin, store, calls } = setup({
    wallet: [{ ok: true, wallet: WALLET(10) }],
    checkin: [{ ok: true, wallet: WALLET(210, { claimable: false }) }],
  });
  await store.load();
  assert.equal(await checkin.claim(), true);
  assert.deepEqual(calls.checkin, [false]);                           // 一般簽到：沒有 confirm_overflow
  assert.equal(store.state.total, 210);
  assert.equal(checkin.finished.value, true);                         // 不再提供簽到按鈕
  assert.equal(checkin.message.value, '簽到成功。目前餘額 210 幣。');
  assert.equal(checkin.preview.value, null);
  assert.equal(checkin.busy.value, false);                            // finally 恢復
  assert.equal(calls.wallet.length, 1);                               // 簽到回應已帶錢包，不必重讀
});

test('onSuccess receives the balance sentence, and a returned string replaces the default message', async () => {
  const seen = [];
  const { checkin } = setup({
    checkin: [{ ok: true, wallet: WALLET(200) }],
    hooks: { onSuccess: (current) => { seen.push(current); return '自訂：' + current; } },
  });
  await checkin.claim();
  assert.deepEqual(seen, ['目前餘額 200 幣。']);
  assert.equal(checkin.message.value, '自訂：目前餘額 200 幣。');
  const plainText = setup({ checkin: [{ ok: true, wallet: WALLET(200) }], hooks: { onSuccess: () => undefined } });
  await plainText.checkin.claim();
  assert.equal(plainText.checkin.message.value, '簽到成功。目前餘額 200 幣。');   // hook 沒回字串：用預設說明
});

test('a check-in response without a wallet reloads the wallet once, so the balance still comes from the server', async () => {
  const { checkin, store, calls } = setup({ wallet: [{ ok: true, wallet: WALLET(400) }], checkin: [{ ok: true }] });
  assert.equal(await checkin.claim(), true);
  assert.equal(calls.wallet.length, 1);
  assert.equal(calls.wallet[0] && calls.wallet[0].force, true);       // 已經 ready 的錢包才需要 force；這裡只是記錄請求有送出
  assert.equal(store.state.total, 400);
  assert.equal(checkin.message.value, '簽到成功。');                  // 說明在重讀之前寫入，餘額當時還不知道
});

// ---- 超過免費幣上限 ---------------------------------------------------------------------------------------------

test('over the free-coin cap: the preview shows what would be credited and lost; cancel sends nothing, claim confirms', async () => {
  const { checkin, store, calls, events } = setup({
    checkin: [overflowFailure(50, 150), overflowFailure(50, 150), { ok: true, wallet: WALLET(1000, { claimable: false }) }],
  });
  await checkin.claim();
  assert.deepEqual(plain(checkin.preview.value), { wouldGain: 50, wouldLose: 150 });
  assert.match(checkin.message.value, /50/);
  assert.match(checkin.message.value, /150/);
  assert.deepEqual(events, [['failure', 409], ['overflow', 'overflow']]);
  assert.equal(calls.checkin.length, 1);

  assert.equal(checkin.cancelOverflow(), true);                       // 取消：不送第二次請求，不消耗當日簽到
  assert.equal(calls.checkin.length, 1);
  assert.equal(checkin.preview.value, null);
  assert.equal(checkin.message.value, '已取消，今天的簽到還沒有使用。');

  await checkin.claim();                                              // 再簽一次（伺服器仍說會超過上限），這回選「領取」
  assert.equal(calls.checkin.length, 2);
  assert.equal(calls.checkin[1], false);
  assert.ok(checkin.preview.value);
  assert.equal(await checkin.confirmOverflow(), true);
  assert.deepEqual(calls.checkin, [false, false, true]);              // 只有「領取」才帶 confirm_overflow
  assert.equal(store.state.total, 1000);
  assert.equal(checkin.preview.value, null);
  assert.equal(checkin.finished.value, true);
});

test('onOverflow runs before the preview and the message are written, so a page can open its window first', async () => {
  const observed = [];
  let target;
  const built = setup({
    checkin: [overflowFailure(10, 190)],
    hooks: { onOverflow: () => { observed.push([target.preview.value, target.message.value]); } },
  });
  target = built.checkin;
  await target.claim();
  assert.deepEqual(observed, [[null, '']]);                           // 呼叫 hook 時預覽與說明還沒寫入
  assert.ok(target.preview.value);
  assert.notEqual(target.message.value, '');
});

test('an overflow response without the preview fields keeps the explanation but no preview', async () => {
  const { checkin, calls } = setup({ checkin: [rejects(serverFailure(409, 'checkin_overflow'))] });
  await checkin.claim();
  assert.equal(checkin.preview.value, null);                          // 只顯示說明，玩家重新簽到即可
  assert.notEqual(checkin.message.value, '');
  assert.equal(await checkin.confirmOverflow(), false);               // 沒有預覽就不會以確認的方式簽到
  assert.deepEqual(calls.checkin, [false]);
});

test('confirming without a preview never sends a confirmed check-in', async () => {
  const { checkin, calls } = setup({ checkin: [{ ok: true, wallet: WALLET(200) }] });
  assert.equal(await checkin.confirmOverflow(), false);
  assert.equal(calls.checkin.length, 0);
  assert.equal(checkin.cancelOverflow(), false);
});

test('while a preview waits for a choice, pressing check-in again sends nothing', async () => {
  const { checkin, calls } = setup({ checkin: [overflowFailure(10, 190)] });
  await checkin.claim();
  assert.equal(await checkin.claim(), false);
  assert.equal(calls.checkin.length, 1);
});

test('onCanceled runs after the preview is dropped; returning true means the page showed it, so the message stays untouched', async () => {
  const texts = [];
  const shown = setup({
    checkin: [overflowFailure(10, 190)],
    hooks: { onCanceled: (text) => { texts.push(text); return true; } },
  });
  await shown.checkin.claim();
  const before = shown.checkin.message.value;
  assert.equal(shown.checkin.cancelOverflow(), true);
  assert.deepEqual(texts, ['已取消，今天的簽到還沒有使用。']);
  assert.equal(shown.checkin.preview.value, null);
  assert.equal(shown.checkin.message.value, before);                  // 頁面已用別的方式呈現，不重複寫入
  const quiet = setup({ checkin: [overflowFailure(10, 190)], hooks: { onCanceled: () => false } });
  await quiet.checkin.claim();
  quiet.checkin.cancelOverflow();
  assert.equal(quiet.checkin.message.value, '已取消，今天的簽到還沒有使用。');
});

test('dropPreview only clears the waiting preview', async () => {
  const { checkin, calls } = setup({ checkin: [overflowFailure(10, 190)] });
  await checkin.claim();
  const message = checkin.message.value;
  checkin.dropPreview();                                              // 呼叫端的視窗被 Esc 或遮罩關掉
  assert.equal(checkin.preview.value, null);
  assert.equal(checkin.message.value, message);
  assert.equal(await checkin.confirmOverflow(), false);               // 沒有預覽了：不會送出確認
  assert.equal(calls.checkin.length, 1);
});

// ---- 已簽到 -----------------------------------------------------------------------------------------------------

test('already checked in shows when the next check-in is possible (Taipei time) and leaves the balance alone', async () => {
  const { checkin, store, events } = setup({
    wallet: [{ ok: true, wallet: WALLET(77) }],
    checkin: [rejects(serverFailure(409, 'already_checked_in'))],
  });
  await store.load();
  await checkin.claim();
  assert.equal(store.state.total, 77);
  assert.equal(checkin.finished.value, true);                         // 不再提供簽到按鈕
  assert.match(checkin.message.value, /今天已經簽到/);
  assert.match(checkin.message.value, /2026-11-13 00:00（台灣時間）/);   // 16:00 UTC = 隔天 00:00 台灣時間；時間來自狀態庫的 next_reset_at
  assert.deepEqual(events, [['failure', 409], ['already', 'already']]);
});

test('once this page has seen "already claimed", it overrides a wallet snapshot that still says claimable', async () => {
  const { checkin, store } = setup({
    wallet: [{ ok: true, wallet: WALLET(77) }],                       // claimable: true
    checkin: [rejects(serverFailure(409, 'already_checked_in'))],
  });
  await store.load();
  checkin.reset();
  assert.equal(checkin.finished.value, false);                        // 錢包說還能簽：提供簽到按鈕
  await checkin.claim();
  checkin.reset();
  assert.equal(checkin.finished.value, true);                         // 看過「今天簽過了」之後，舊快照不再開放簽到
  assert.equal(checkin.message.value, '');
  assert.equal(checkin.preview.value, null);
});

test('after a successful check-in this page does not offer it again, even if the snapshot in the response still says claimable', async () => {
  const { checkin } = setup({ checkin: [{ ok: true, wallet: WALLET(210) }] });     // 快照的 claimable 仍為 true（過期或不一致）
  await checkin.claim();
  checkin.reset();                                                    // 呼叫端重新打開簽到畫面
  assert.equal(checkin.finished.value, true);
});

test('reset reads the wallet: claimable false means finished, claimable true or unknown means not finished', async () => {
  const claimed = setup({ wallet: [{ ok: true, wallet: WALLET(5, { claimable: false }) }] });
  await claimed.store.load();
  claimed.checkin.reset();
  assert.equal(claimed.checkin.finished.value, true);
  const unknown = setup();                                            // 還沒讀過錢包：claimable 是 null
  unknown.checkin.reset();
  assert.equal(unknown.checkin.finished.value, false);
});

// ---- 重複點擊與失敗 ---------------------------------------------------------------------------------------------

test('pressing check-in twice quickly sends exactly one request, and the button recovers afterwards', async () => {
  let release = null;
  const calls = [];
  const store = createWalletStore({ reactive: Vue.reactive, fetcher: () => Promise.resolve({ wallet: WALLET(0) }) });
  const checkin = createCheckin({
    Vue: { ref: Vue.ref }, store, describeFailure: describe,
    api: { checkin: (confirm) => { calls.push(confirm); return new Promise((resolve) => { release = resolve; }); } },
  });
  const first = checkin.claim();
  assert.equal(checkin.busy.value, true);
  const second = checkin.claim();
  assert.equal(await second, false);
  assert.equal(calls.length, 1);
  release({ ok: true, wallet: WALLET(200) });
  await first;
  assert.equal(checkin.busy.value, false);
  assert.equal(calls.length, 1);
});

test('every failure path restores the button and explains in Traditional Chinese: network, timeout, 429, 503, 500', async () => {
  const failures = [
    [rejects(transportFailure('network')), /無法連線/],
    [rejects(transportFailure('timeout')), /連線逾時/],
    [rejects(serverFailure(429, 'rate_limited')), /操作太頻繁/],
    [rejects(serverFailure(503, 'wallet_busy')), /較忙碌/],
    [rejects(serverFailure(500, 'internal_error')), /./],
  ];
  for (const [failure, pattern] of failures) {
    const { checkin, store, events } = setup({ checkin: [failure] });
    assert.equal(await checkin.claim(), false);
    assert.equal(checkin.busy.value, false);
    assert.match(checkin.message.value, pattern);
    assert.equal(checkin.preview.value, null);
    assert.equal(checkin.finished.value, false);                      // 失敗不代表簽過了：玩家可以再試
    assert.equal(store.state.total, null);
    assert.deepEqual(events.map((event) => event[0]), ['failure', 'error']);
  }
});

test('a throwing API restores the button too, and the failure is explained', async () => {
  const store = createWalletStore({ reactive: Vue.reactive, fetcher: () => Promise.reject(new Error('boom')) });
  const checkin = createCheckin({
    Vue: { ref: Vue.ref }, store, describeFailure: describe,
    api: { checkin: () => { throw new Error('boom'); } },
  });
  assert.equal(await checkin.claim(), false);
  assert.equal(checkin.busy.value, false);
  assert.notEqual(checkin.message.value, '');
});

test('a 401 calls onLogin once and shows nothing; a cancelled request shows nothing and calls no page hook', async () => {
  const unauthorized = setup({ checkin: [rejects(serverFailure(401, 'unauthorized'))] });
  assert.equal(await unauthorized.checkin.claim(), false);
  assert.deepEqual(unauthorized.events, [['failure', 401], ['login']]);
  assert.equal(unauthorized.checkin.message.value, '');
  assert.equal(unauthorized.checkin.busy.value, false);
  const canceled = setup({ checkin: [rejects(transportFailure('canceled'))] });
  assert.equal(await canceled.checkin.claim(), false);
  assert.deepEqual(canceled.events, [['failure', 0]]);                // 只有原始 info 交給 onFailure；不開視窗、不導向登入
  assert.equal(canceled.checkin.message.value, '');
  assert.equal(canceled.checkin.busy.value, false);
});

// ---- hooks 與保底 -----------------------------------------------------------------------------------------------

test('without hooks every path still works silently', async () => {
  const store = createWalletStore({ reactive: Vue.reactive, fetcher: () => Promise.resolve({ wallet: WALLET(0) }) });
  const queue = [overflowFailure(10, 190), rejects(serverFailure(409, 'already_checked_in')), rejects(serverFailure(401, 'unauthorized')), { ok: true, wallet: WALLET(200) }];
  const checkin = createCheckin({ Vue: { ref: Vue.ref }, store, describeFailure: describe, api: { checkin: () => respond(queue) } });
  await checkin.claim();
  assert.ok(checkin.preview.value);
  assert.equal(checkin.cancelOverflow(), true);
  await checkin.claim();                                              // already
  assert.equal(checkin.finished.value, true);
  await checkin.claim();                                              // 401
  await checkin.claim();                                              // 成功
  assert.equal(store.state.total, 200);
  assert.equal(checkin.busy.value, false);
});

test('a hook that is not a function is ignored', async () => {
  const { checkin } = setup({ checkin: [rejects(serverFailure(503, 'wallet_busy'))], hooks: { onError: 'nope', onFailure: null } });
  assert.equal(await checkin.claim(), false);
  assert.match(checkin.message.value, /較忙碌/);
});

test('the failure description is injected: the page decides the wording, and gets the next reset time from the shared store', async () => {
  const seen = [];
  const { checkin, store } = setup({
    wallet: [{ ok: true, wallet: WALLET(5) }],
    checkin: [rejects(serverFailure(503, 'wallet_busy'))],
    describeFailure: (info, options) => { seen.push([info.status, options.nextResetAt]); return { kind: 'unavailable', message: '自訂失敗說明', checkin: null }; },
  });
  await store.load();
  await checkin.claim();
  assert.deepEqual(seen, [[503, '2026-11-12T16:00:00+00:00']]);
  assert.equal(checkin.message.value, '自訂失敗說明');
});

test('without an injected description or with a broken one, a failure is still explained and the button recovers', async () => {
  for (const describeFailure of [undefined, () => null, () => 'oops']) {
    const store = createWalletStore({ reactive: Vue.reactive, fetcher: () => Promise.resolve({ wallet: WALLET(0) }) });
    const checkin = createCheckin({
      Vue: { ref: Vue.ref }, store, describeFailure,
      api: { checkin: () => respond([rejects(serverFailure(500, 'internal_error'))]) },
    });
    assert.equal(await checkin.claim(), false);
    assert.equal(checkin.busy.value, false);
    assert.equal(checkin.message.value, '簽到沒有完成，請稍後再試。');
  }
});

// ---- 兩處行為一致（wallet-hub 2.2）-----------------------------------------------------------------------------------

/* 同一組輸入分別餵給 game-wallet.js（簽到 Modal）與 createCheckin（錢包頁用的），記錄請求與可見的結果。 */
function runBoth(scenario) {
  const outcomes = [];
  for (const kind of ['game-wallet', 'checkin']) {
    const calls = { wallet: [], checkin: [] };
    const wallet = (scenario.wallet || [{ ok: true, wallet: WALLET(10) }]).slice();     // 每一邊各用自己的佇列（回應是依序消耗的）
    const checkinQueue = scenario.checkin.slice();
    const api = {
      wallet: (options) => { calls.wallet.push(options); return respond(wallet); },
      checkin: (confirm) => { calls.checkin.push(confirm); return respond(checkinQueue); },
    };
    const store = createWalletStore({ reactive: Vue.reactive, fetcher: (settings) => api.wallet(settings) });
    let driver;
    if (kind === 'game-wallet') {
      const page = createWallet({ Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch }, api, store });
      driver = {
        claim: page.claimCheckin, confirm: page.confirmOverflow, cancel: page.cancelOverflow,
        preview: () => plain(page.checkinPreview.value), finished: () => page.checkinFinished.value, busy: () => page.checkinBusy.value,
      };
    } else {
      const shared = createCheckin({ Vue: { ref: Vue.ref }, api, store, describeFailure: describe });
      driver = {
        claim: shared.claim, confirm: shared.confirmOverflow, cancel: shared.cancelOverflow,
        preview: () => plain(shared.preview.value), finished: () => shared.finished.value, busy: () => shared.busy.value,
      };
    }
    outcomes.push({ kind, calls, store, driver });
  }
  return outcomes;
}

async function drive(outcome, actions) {
  const trace = [];
  await outcome.store.load();
  for (const action of actions) {
    const result = await outcome.driver[action]();
    trace.push([action, result, outcome.driver.preview(), outcome.driver.busy(), outcome.store.state.total]);
  }
  return {
    trace, checkinCalls: outcome.calls.checkin.slice(), walletCalls: outcome.calls.wallet.length,
    total: outcome.store.state.total, claimable: outcome.store.state.checkin.claimable, preview: outcome.driver.preview(), busy: outcome.driver.busy(),
  };
}

const SCENARIOS = [
  ['a plain check-in', { checkin: [{ ok: true, wallet: WALLET(210, { claimable: false }) }] }, ['claim']],
  ['a success without a wallet', { wallet: [{ ok: true, wallet: WALLET(10) }, { ok: true, wallet: WALLET(410, { claimable: false }) }], checkin: [{ ok: true }] }, ['claim']],
  ['an overflow preview, then cancel, then claim again and confirm', { checkin: [overflowFailure(50, 150), overflowFailure(50, 150), { ok: true, wallet: WALLET(1000, { claimable: false }) }] }, ['claim', 'cancel', 'claim', 'confirm']],
  ['pressing check-in while a preview waits', { checkin: [overflowFailure(10, 190)] }, ['claim', 'claim', 'cancel']],
  ['confirm and cancel without a preview', { checkin: [{ ok: true, wallet: WALLET(200) }] }, ['confirm', 'cancel']],
  ['already checked in, then check-in again', { checkin: [rejects(serverFailure(409, 'already_checked_in'))] }, ['claim', 'claim']],
  ['a 401', { checkin: [rejects(serverFailure(401, 'unauthorized'))] }, ['claim']],
  ['a 429', { checkin: [rejects(serverFailure(429, 'rate_limited'))] }, ['claim']],
  ['a 503', { checkin: [rejects(serverFailure(503, 'wallet_busy'))] }, ['claim']],
  ['a network error and then a success', { checkin: [rejects(transportFailure('network')), { ok: true, wallet: WALLET(200, { claimable: false }) }] }, ['claim', 'claim']],
];

for (const [name, scenario, actions] of SCENARIOS) {
  test(`the game page and the shared state machine behave the same: ${name}`, async () => {
    const [fromPage, fromShared] = runBoth(scenario);
    const page = await drive(fromPage, actions);
    const shared = await drive(fromShared, actions);
    assert.deepEqual(shared, page);                                   // 預覽內容、請求次數、餘額、簽到按鈕與每一步的回傳值都相同
  });
}

test('two clicks at once send one request through the game page and through the shared state machine alike', async () => {
  for (const kind of ['game-wallet', 'checkin']) {
    let release = null;
    let sent = 0;
    const api = { wallet: () => Promise.resolve({ wallet: WALLET(0) }), checkin: () => { sent += 1; return new Promise((resolve) => { release = resolve; }); } };
    const store = createWalletStore({ reactive: Vue.reactive, fetcher: () => api.wallet() });
    const claim = kind === 'game-wallet'
      ? createWallet({ Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch }, api, store }).claimCheckin
      : createCheckin({ Vue: { ref: Vue.ref }, api, store, describeFailure: describe }).claim;
    const first = claim();
    const second = claim();
    assert.equal(await second, false, kind);
    release({ ok: true, wallet: WALLET(200) });
    await first;
    assert.equal(sent, 1, kind);
  }
});

// ---- 沒有儲存、網路與輸出 ---------------------------------------------------------------------------------------

test('the module touches no browser storage, network or console, and builds no markup', () => {
  const source = fs.readFileSync(path.join(ROOT, 'app/static/js/ui/wallet-checkin.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'axios', 'XMLHttpRequest', 'console.', 'v-html', 'innerHTML']) {
    assert.ok(!source.includes(word), word);
  }
});

test('game-wallet.js no longer carries its own copy of the check-in flow', () => {
  const source = fs.readFileSync(path.join(ROOT, 'app/static/js/game/game-wallet.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  assert.ok(source.includes('createCheckin('));
  assert.ok(!source.includes('confirm_overflow'));                    // 請求本文只由 wallet-api.js／game-api.js 組出，狀態機只傳布林
  assert.ok(!/api\.checkin\(/.test(source));                          // 呼叫 API 的是共用的狀態機，不是這裡
});
