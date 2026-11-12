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
