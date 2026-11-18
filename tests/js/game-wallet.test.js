'use strict';
/* 遊戲幣（game-wallet.js）：rpg-wallet-ui 規格的每個情境——頂列餘額、每輪以 done.coins 更新、讀取失敗顯示「—」、送出前的餘額不足提示、
 * 409 insufficient_coins 的 Modal、簽到（預覽、取消、領取、已簽到、重複點擊、finally 恢復）。
 * 以真的 Vue（tests/js/support/vue-runtime.js）與替身 API 執行；替身 API 記錄每個呼叫，用來證明「沒有多送的請求」。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadVue, plain, settle, ROOT } = require('./support/vue-runtime.js');
const { createWallet } = require('../../app/static/js/game/game-wallet.js');
const { createWalletStore, walletLabelOf } = require('../../app/static/js/ui/wallet-store.js');

const Vue = loadVue();
const WALLET = (total, extra) => ({
  free_balance: total, paid_balance: 0, total, free_cap: 1000, turn_cost: { standard: 30 },
  checkin: Object.assign({ claimable: true, grant: 200, would_gain: 200, would_lose: 0, next_reset_at: '2026-11-12T16:00:00+00:00' }, extra),
});
const serverFailure = (status, code, extra) => Object.assign({ kind: 'server', status, code, message: '', fields: {}, categories: [] }, extra);

/* 建立被測試的錢包與替身 API。`scripts.wallet`／`scripts.checkin` 是依序回應的陣列（值或 Error 物件）。 */
function setup({ wallet = [], checkin = [] } = {}) {
  const calls = { wallet: [], checkin: [] };
  const toasts = [];
  const logins = [];
  const noted = [];
  const respond = (queue, list) => {
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return next instanceof Error || (next && next.__reject) ? Promise.reject(next.__reject || next) : Promise.resolve(next);
  };
  const api = {
    wallet: (options) => { calls.wallet.push(options); return respond(wallet); },
    checkin: (confirm, options) => { calls.checkin.push({ confirm, options }); return respond(checkin); },
  };
  const target = createWallet({
    Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch }, api,
    toast: (message) => toasts.push(message), goLogin: () => logins.push(true), noteFailure: (info) => noted.push(info),
  });
  return { wallet: target, calls, toasts, logins, noted };
}
const rejects = (info) => ({ __reject: info });

test('before the wallet is read the balance shows a dash, and the dash stays when the read fails', async () => {
  const { wallet } = setup({ wallet: [rejects(serverFailure(503, 'wallet_busy'))] });
  assert.equal(wallet.balanceText.value, '—');
  assert.equal(await wallet.load(), false);
  assert.equal(wallet.balanceText.value, '—');                       // 讀取失敗：顯示「—」，不拋出，時間軸與輸入不受影響
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(wallet.lowBalanceText.value, '');
});

test('loading shows the total balance and keeps the per-turn price', async () => {
  const { wallet, calls } = setup({ wallet: [{ ok: true, wallet: WALLET(230) }] });
  assert.equal(await wallet.load({ signal: 'S' }), true);
  assert.equal(wallet.balanceText.value, '230');
  assert.equal(wallet.turnCost.value, 30);
  assert.equal(calls.wallet.length, 1);
  assert.equal(calls.wallet[0].signal, 'S');                          // 中止訊號由呼叫端傳入
});

test('a finished turn overwrites the balance from done.coins without another wallet request', async () => {
  const { wallet, calls } = setup({ wallet: [{ ok: true, wallet: WALLET(200) }] });
  await wallet.load();
  assert.equal(wallet.setBalance(170), true);
  assert.equal(wallet.balanceText.value, '170');
  assert.equal(calls.wallet.length, 1);                               // 沒有額外的錢包請求
  assert.equal(wallet.setBalance(-1), false);
  assert.equal(wallet.setBalance('170'), false);
  assert.equal(wallet.setBalance(NaN), false);
  assert.equal(wallet.balanceText.value, '170');                      // 型別不對的值不採用
});

test('a malformed wallet payload is ignored field by field', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: { total: 'many', turn_cost: { standard: -5 }, checkin: 'yes' } }] });
  assert.equal(await wallet.load(), false);
  assert.equal(wallet.balanceText.value, '—');
  assert.equal(wallet.turnCost.value, null);
});

test('a 401 on loading sends the player to the login page; other failures do not', async () => {
  const unauthorized = setup({ wallet: [rejects(serverFailure(401, 'unauthorized'))] });
  await unauthorized.wallet.load();
  assert.equal(unauthorized.logins.length, 1);
  const busy = setup({ wallet: [rejects(serverFailure(503, 'wallet_busy'))] });
  await busy.wallet.load();
  assert.equal(busy.logins.length, 0);
  assert.equal(busy.noted.length, 1);                                 // 失敗的原始資訊交給 game.js（辨識被鎖定等受限狀態）
});

test('a second load while one is running does not send a second request', async () => {
  const { wallet, calls } = setup({ wallet: [{ ok: true, wallet: WALLET(100) }] });
  await Promise.all([wallet.load(), wallet.load()]);
  assert.equal(calls.wallet.length, 1);
});

test('a known low balance only explains and offers the check-in; it never blocks sending', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: WALLET(10) }] });
  assert.equal(wallet.lowBalanceText.value, '');                      // 餘額還不知道：不提示
  await wallet.load();
  assert.equal(wallet.lowBalance.value, true);
  assert.match(wallet.lowBalanceText.value, /目前餘額 10 幣/);
  assert.match(wallet.lowBalanceText.value, /每一輪需要 30 幣/);
  assert.match(wallet.lowBalanceText.value, /仍可直接送出/);
  assert.equal(typeof wallet.lowBalance.value, 'boolean');            // 只有提示，沒有「阻止送出」的旗標
  wallet.setBalance(30);
  assert.equal(wallet.lowBalanceText.value, '');                      // 剛好夠一輪：不提示
});

test('insufficient coins opens the modal with the balance and the price, says nothing was charged, and offers check-in and close', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: WALLET(200) }] });
  await wallet.load();
  wallet.openInsufficient({ balance: 10, needed: 30 });
  assert.equal(wallet.coinModalOpen.value, true);
  assert.match(wallet.coinModalText.value, /目前餘額 10 幣/);
  assert.match(wallet.coinModalText.value, /每一輪需要 30 幣/);
  assert.match(wallet.coinModalText.value, /沒有扣除遊戲幣/);
  assert.match(wallet.coinModalText.value, /行動文字還保留在輸入框/);
  assert.equal(wallet.balanceText.value, '10');                       // 伺服器說的餘額覆寫過期的快取
  assert.equal(wallet.checkinFinished.value, false);                  // 還能簽到：Modal 會有「每日簽到」按鈕
  wallet.closeCoinModal();
  assert.equal(wallet.coinModalOpen.value, false);
});

test('insufficient coins without numbers still opens the modal with a plain explanation', () => {
  const { wallet } = setup();
  wallet.openInsufficient(null);
  assert.equal(wallet.coinModalOpen.value, true);
  assert.match(wallet.coinModalText.value, /^遊戲幣不足。這一輪沒有扣除遊戲幣/);
});

test('checking in from the modal updates the balance, says it can be sent again, and never resends anything', async () => {
  const { wallet, calls, toasts } = setup({
    wallet: [{ ok: true, wallet: WALLET(10) }],
    checkin: [{ ok: true, wallet: WALLET(210, { claimable: false }) }],
  });
  await wallet.load();
  wallet.openInsufficient({ balance: 10, needed: 30 });
  assert.equal(await wallet.claimCheckin(), true);
  assert.deepEqual(plain(calls.checkin), [{ confirm: false }]);       // 一般簽到：沒有 confirm_overflow
  assert.equal(wallet.balanceText.value, '210');
  assert.equal(wallet.checkinFinished.value, true);
  assert.match(wallet.checkinMessage.value, /簽到成功/);
  assert.match(wallet.checkinMessage.value, /再次送出行動/);
  assert.match(wallet.checkinMessage.value, /不會自動送出/);
  assert.equal(toasts.length, 1);
  assert.equal(calls.wallet.length, 1);                               // 簽到回應已帶錢包，不必重讀
  assert.equal(wallet.checkinBusy.value, false);                      // finally 恢復
});

test('a plain check-in from the input hint shows a toast with the new balance', async () => {
  const { wallet, toasts } = setup({ checkin: [{ ok: true, wallet: WALLET(200) }] });
  await wallet.claimCheckin();
  assert.equal(wallet.balanceText.value, '200');
  assert.match(toasts[0], /簽到成功/);
  assert.match(toasts[0], /200/);
});

test('a check-in response without a wallet reloads the wallet, so the balance still comes from the server', async () => {
  const { wallet, calls } = setup({ wallet: [{ ok: true, wallet: WALLET(400) }], checkin: [{ ok: true }] });
  await wallet.claimCheckin();
  assert.equal(calls.wallet.length, 1);
  assert.equal(wallet.balanceText.value, '400');
});

test('over the free-coin cap: the preview shows what would be credited and lost; cancel sends nothing, claim confirms', async () => {
  const overflow = rejects(serverFailure(409, 'checkin_overflow', { checkin: { would_gain: 50, would_lose: 150 } }));
  const { wallet, calls } = setup({ checkin: [overflow, overflow, { ok: true, wallet: WALLET(1000, { claimable: false }) }] });
  await wallet.claimCheckin();
  assert.deepEqual(plain(wallet.checkinPreview.value), { wouldGain: 50, wouldLose: 150 });
  assert.equal(wallet.coinModalOpen.value, true);                     // 從輸入區的提示簽到時，預覽也要有地方顯示
  assert.match(wallet.checkinMessage.value, /50/);
  assert.match(wallet.checkinMessage.value, /150/);
  assert.equal(calls.checkin.length, 1);

  assert.equal(wallet.cancelOverflow(), true);                        // 取消：不送第二次請求，不消耗當日簽到
  assert.equal(calls.checkin.length, 1);
  assert.equal(wallet.checkinPreview.value, null);
  assert.equal(wallet.coinModalOpen.value, false);

  await wallet.claimCheckin();                                        // 再簽一次（伺服器仍說會超過上限），這回選「領取」
  assert.equal(calls.checkin.length, 2);
  assert.equal(calls.checkin[1].confirm, false);
  assert.ok(wallet.checkinPreview.value);
  const sent = await wallet.confirmOverflow();
  assert.equal(sent, true);
  assert.equal(calls.checkin.length, 3);
  assert.equal(calls.checkin[2].confirm, true);                       // 只有「領取」才帶 confirm_overflow
  assert.equal(wallet.balanceText.value, '1000');
});

test('confirming without a preview never sends a confirmed check-in', async () => {
  const { wallet, calls } = setup({ checkin: [{ ok: true, wallet: WALLET(200) }] });
  assert.equal(await wallet.confirmOverflow(), false);
  assert.equal(calls.checkin.length, 0);
  assert.equal(wallet.cancelOverflow(), false);
});

test('while a preview waits for a choice, pressing check-in again sends nothing', async () => {
  const overflow = rejects(serverFailure(409, 'checkin_overflow', { checkin: { would_gain: 10, would_lose: 190 } }));
  const { wallet, calls } = setup({ checkin: [overflow] });
  await wallet.claimCheckin();
  await wallet.claimCheckin();
  assert.equal(calls.checkin.length, 1);
});

test('cancelling a preview that came from the insufficient-coins modal keeps the modal open with an explanation', async () => {
  const overflow = rejects(serverFailure(409, 'checkin_overflow', { checkin: { would_gain: 10, would_lose: 190 } }));
  const { wallet, calls } = setup({ checkin: [overflow] });
  wallet.openInsufficient({ balance: 1000, needed: 30 });
  await wallet.claimCheckin();
  assert.equal(wallet.cancelOverflow(), true);
  assert.equal(calls.checkin.length, 1);
  assert.equal(wallet.coinModalOpen.value, true);
  assert.match(wallet.checkinMessage.value, /還沒有使用/);
});

test('already checked in shows when the next check-in is possible (Taipei time) and leaves the balance alone', async () => {
  const already = rejects(serverFailure(409, 'already_checked_in'));
  const { wallet } = setup({ wallet: [{ ok: true, wallet: WALLET(77) }], checkin: [already] });
  await wallet.load();
  await wallet.claimCheckin();
  assert.equal(wallet.balanceText.value, '77');
  assert.equal(wallet.checkinFinished.value, true);                   // 不再提供簽到按鈕
  assert.match(wallet.checkinMessage.value, /今天已經簽到/);
  assert.match(wallet.checkinMessage.value, /2026-11-13 00:00（台灣時間）/);   // 16:00 UTC = 隔天 00:00 台灣時間
  assert.equal(wallet.coinModalOpen.value, true);
});

test('when the wallet already says today was claimed, the modal does not offer the check-in button', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: WALLET(5, { claimable: false }) }] });
  await wallet.load();
  wallet.openInsufficient({ balance: 5, needed: 30 });
  assert.equal(wallet.checkinFinished.value, true);
});

test('pressing check-in twice quickly sends exactly one request, and the button recovers afterwards', async () => {
  let release = null;
  const calls = [];
  const wallet = createWallet({
    Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch },
    api: { wallet: () => Promise.resolve({ wallet: WALLET(0) }), checkin: (confirm) => { calls.push(confirm); return new Promise((resolve) => { release = resolve; }); } },
  });
  const first = wallet.claimCheckin();
  assert.equal(wallet.checkinBusy.value, true);
  const second = wallet.claimCheckin();
  assert.equal(await second, false);
  assert.equal(calls.length, 1);
  release({ ok: true, wallet: WALLET(200) });
  await first;
  assert.equal(wallet.checkinBusy.value, false);
  assert.equal(calls.length, 1);
});

test('every failure path restores the button: network, timeout, server error and a throwing API', async () => {
  const failures = [
    rejects({ kind: 'network', status: 0, code: '', message: '', fields: {}, categories: [] }),
    rejects({ kind: 'timeout', status: 0, code: '', message: '', fields: {}, categories: [] }),
    rejects(serverFailure(503, 'wallet_busy')),
    rejects(serverFailure(500, 'internal_error')),
  ];
  for (const failure of failures) {
    const { wallet } = setup({ checkin: [failure] });
    await wallet.claimCheckin();
    assert.equal(wallet.checkinBusy.value, false);
    assert.notEqual(wallet.checkinMessage.value, '');                 // 有一句繁體中文的說明
    assert.equal(wallet.balanceText.value, '—');
  }
  const throwing = createWallet({
    Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch },
    api: { wallet: () => { throw new Error('boom'); }, checkin: () => { throw new Error('boom'); } },
  });
  assert.equal(await throwing.claimCheckin(), false);
  assert.equal(throwing.checkinBusy.value, false);
  assert.equal(await throwing.load(), false);
});

test('a 401 or a cancelled check-in does not open the modal or show an error', async () => {
  const unauthorized = setup({ checkin: [rejects(serverFailure(401, 'unauthorized'))] });
  await unauthorized.wallet.claimCheckin();
  assert.equal(unauthorized.logins.length, 1);
  assert.equal(unauthorized.wallet.coinModalOpen.value, false);
  const canceled = setup({ checkin: [rejects({ kind: 'canceled', status: 0, code: '', message: '', fields: {}, categories: [] })] });
  await canceled.wallet.claimCheckin();
  assert.equal(canceled.wallet.coinModalOpen.value, false);
  assert.equal(canceled.wallet.checkinMessage.value, '');
});

test('closing the modal with Esc or the overlay (v-model false) drops a waiting preview, so it does not reappear', async () => {
  const overflow = rejects(serverFailure(409, 'checkin_overflow', { checkin: { would_gain: 10, would_lose: 190 } }));
  const { wallet } = setup({ checkin: [overflow] });
  await wallet.claimCheckin();
  assert.ok(wallet.checkinPreview.value);
  wallet.coinModalOpen.value = false;                                 // Modal 元件以 update:modelValue 寫回 false
  assert.equal(wallet.checkinPreview.value, null);
  await settle();
  assert.equal(await wallet.confirmOverflow(), false);                // 沒有預覽了：不會送出確認
});

test('the module touches no browser storage, network or console, and never mentions player text', () => {
  const source = fs.readFileSync(path.join(ROOT, 'app/static/js/game/game-wallet.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'XMLHttpRequest', 'console.', 'v-html', 'innerHTML']) {
    assert.ok(!source.includes(word), word);
  }
});

// ---- 管理員不消耗遊戲幣（admin-unlimited-coins）-------------------------------------------------------------

const EXEMPT_WALLET = (total, extra) => Object.assign({}, WALLET(total), { turn_cost: { standard: 0 }, coin_exempt: true }, extra);

test('coin_exempt is read as a real boolean only: true is exempt, and a missing or mistyped value is the same as false', async () => {
  const cases = [[true, true], [false, false], [undefined, false], [null, false], ['true', false], [1, false], [0, false], [{}, false], [[], false]];
  for (const [value, expected] of cases) {
    const wallet = Object.assign(WALLET(10), value === undefined ? {} : { coin_exempt: value });
    const { wallet: target } = setup({ wallet: [{ ok: true, wallet }] });
    assert.equal(target.exempt.value, false);                         // 載入之前：一般玩家的行為
    await target.load();
    assert.equal(target.exempt.value, expected, JSON.stringify(value));
  }
});

test('an exempt account with a zero balance and a zero price gets no low-balance hint, and its balance is still shown as it is', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: EXEMPT_WALLET(0) }] });
  assert.equal(await wallet.load(), true);
  assert.equal(wallet.exempt.value, true);
  assert.equal(wallet.balanceText.value, '0');                        // 餘額照實顯示，不隱藏
  assert.equal(wallet.turnCost.value, null);                          // 幣價 0 仍視為缺值（沒有數字可顯示）
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(wallet.lowBalanceText.value, '');
});

test('exempt wins even when a price is known: a low balance is not a problem for an exempt account', async () => {
  const { wallet } = setup({ wallet: [{ ok: true, wallet: EXEMPT_WALLET(0, { turn_cost: { standard: 30 } }) }] });
  await wallet.load();
  assert.equal(wallet.turnCost.value, 30);
  assert.equal(wallet.exempt.value, true);
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(wallet.lowBalanceText.value, '');
  wallet.setBalance(5);                                               // 一輪結束後的餘額更新也不會出現提示
  assert.equal(wallet.lowBalanceText.value, '');
});

test('an ordinary account below the price still gets the hint, whether or not the response carries the flag', async () => {
  for (const extra of [{ coin_exempt: false }, {}]) {
    const { wallet } = setup({ wallet: [{ ok: true, wallet: Object.assign(WALLET(0), extra) }] });
    await wallet.load();
    assert.equal(wallet.exempt.value, false);
    assert.equal(wallet.lowBalance.value, true, JSON.stringify(extra));
    assert.match(wallet.lowBalanceText.value, /目前餘額 0 幣/);
    assert.match(wallet.lowBalanceText.value, /每一輪需要 30 幣/);
  }
});

test('when the wallet cannot be loaded the account is treated as an ordinary one, and nothing is thrown', async () => {
  const { wallet } = setup({ wallet: [rejects(serverFailure(503, 'wallet_busy'))] });
  assert.equal(await wallet.load(), false);
  assert.equal(wallet.exempt.value, false);
  assert.equal(wallet.lowBalance.value, false);                       // 餘額與幣價都不知道：沒有提示
  assert.equal(wallet.lowBalanceText.value, '');
});

test('every wallet snapshot overwrites the flag, including the one that comes back from a check-in', async () => {
  const { wallet } = setup({
    wallet: [{ ok: true, wallet: EXEMPT_WALLET(0) }, { ok: true, wallet: WALLET(0) }],
    checkin: [{ ok: true, wallet: EXEMPT_WALLET(200, { checkin: { claimable: false } }) }],
  });
  await wallet.load();
  assert.equal(wallet.exempt.value, true);
  await wallet.claimCheckin();
  assert.equal(wallet.exempt.value, true);                            // 簽到回的錢包也帶 coin_exempt
  assert.equal(wallet.balanceText.value, '200');
  await wallet.load({ force: true });                                // 之後讀到不再豁免的快照（例如角色被改變）：跟著伺服器（已讀到的錢包只有明確要求才重讀）
  assert.equal(wallet.exempt.value, false);
});

test('exempt is exposed next to the other wallet state and nothing in the exempt path calls the server again', async () => {
  const { wallet, calls } = setup({ wallet: [{ ok: true, wallet: EXEMPT_WALLET(0) }] });
  await wallet.load();
  wallet.setBalance(0);
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(calls.wallet.length, 1);
  assert.equal(calls.checkin.length, 0);
  assert.equal(typeof wallet.exempt.value, 'boolean');
});

// ---- 與全站的錢包狀態庫共用（wallet-hub 1.5）-----------------------------------------------------------------------

/* 共用的狀態庫（頂列標籤讀的就是它）加上用它建立的遊戲幣邏輯；`fetches` 記錄真正送出的錢包請求。 */
function sharedSetup({ wallet = [], checkin = [] } = {}) {
  const fetches = [];
  const respond = (queue) => {
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return next instanceof Error || (next && next.__reject) ? Promise.reject(next.__reject || next) : Promise.resolve(next);
  };
  const store = createWalletStore({
    reactive: Vue.reactive,
    fetcher: (settings) => { fetches.push(settings); return respond(wallet); },
  });
  const toasts = [];
  const logins = [];
  const noted = [];
  const checkins = [];
  const api = { wallet: () => { throw new Error('頁面不該再自己讀錢包'); }, checkin: (confirm) => { checkins.push(confirm); return respond(checkin); } };
  const target = createWallet({
    Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch }, api, store,
    toast: (message) => toasts.push(message), goLogin: () => logins.push(true), noteFailure: (info) => noted.push(info),
  });
  const label = () => walletLabelOf(store.state);
  return { wallet: target, store, label, fetches, checkins, toasts, logins, noted };
}

test('with a shared store the label and the page logic ask once and read the same number', async () => {
  const { wallet, store, label, fetches } = sharedSetup({ wallet: [{ ok: true, wallet: WALLET(230) }] });
  const fromLabel = store.load();                                     // 標籤的 onMounted 先到
  const fromPage = wallet.load({ signal: 'S' });                      // 頁面自己的載入合併進同一個請求
  assert.deepEqual(await Promise.all([fromLabel, fromPage]), [true, true]);
  assert.equal(fetches.length, 1);
  assert.equal(label().amount, '230');
  assert.equal(wallet.balanceText.value, '230');
  assert.equal(wallet.turnCost.value, 30);
  assert.equal(await wallet.load(), true);                            // 已讀到：再載入不會再請求
  assert.equal(fetches.length, 1);
});

test('done.coins updates the label and the low-balance hint from the same state without a request', async () => {
  const { wallet, label, fetches } = sharedSetup({ wallet: [{ ok: true, wallet: WALLET(230) }] });
  await wallet.load();
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(wallet.setBalance(20), true);
  assert.equal(label().amount, '20');
  assert.equal(wallet.balanceText.value, '20');
  assert.equal(wallet.lowBalance.value, true);
  assert.ok(wallet.lowBalanceText.value.includes('目前餘額 20 幣，每一輪需要 30 幣'));
  assert.equal(fetches.length, 1);
});

test('an exempt account with a zero balance: the label shows 0 and the note, the page shows no low-balance hint', async () => {
  const { wallet, label } = sharedSetup({ wallet: [{ ok: true, wallet: EXEMPT_WALLET(0) }] });
  await wallet.load();
  assert.equal(label().amount, '0');
  assert.equal(label().exempt, true);
  assert.equal(wallet.lowBalance.value, false);
  assert.equal(wallet.lowBalanceText.value, '');
});

test('a failed read shows a dash on the label, and the failure still reaches noteFailure and the login redirect when the label started it', async () => {
  const { wallet, store, label, noted, logins } = sharedSetup({ wallet: [rejects(serverFailure(401, 'unauthorized'))] });
  assert.equal(await store.load(), false);                            // 標籤發起，不是頁面
  assert.equal(label().amount, '—');
  assert.equal(noted.length, 1);
  assert.equal(noted[0].status, 401);
  assert.deepEqual(logins, [true]);
  assert.equal(wallet.balanceText.value, '—');
  const unavailable = sharedSetup({ wallet: [rejects(serverFailure(503, 'wallet_busy'))] });
  await unavailable.wallet.load();
  assert.equal(unavailable.label().amount, '—');
  assert.deepEqual(unavailable.logins, []);
  assert.equal(unavailable.noted.length, 1);
});

test('a successful check-in updates the label through the shared store without another wallet request', async () => {
  const { wallet, label, fetches, checkins } = sharedSetup({
    wallet: [{ ok: true, wallet: WALLET(10) }],
    checkin: [{ ok: true, wallet: WALLET(210, { checkin: { claimable: false } }) }],
  });
  await wallet.load();
  assert.equal(label().amount, '10');
  await wallet.claimCheckin();
  assert.deepEqual(checkins, [false]);
  assert.equal(label().amount, '210');
  assert.equal(wallet.checkinFinished.value, true);
  assert.equal(fetches.length, 1);
});

test('a check-in response without a wallet makes the page read the wallet once more (force), and the label follows', async () => {
  const { wallet, label, fetches } = sharedSetup({
    wallet: [{ ok: true, wallet: WALLET(10) }, { ok: true, wallet: WALLET(210, { checkin: { claimable: false } }) }],
    checkin: [{ ok: true }],
  });
  await wallet.load();
  await wallet.claimCheckin();
  assert.equal(fetches.length, 2);
  assert.equal(label().amount, '210');
});
