'use strict';
/* 錢包頁的純函式與計時器管理（app/static/js/wallet/wallet-hub-state.js）：wallet-hub 2.4。
 * 倒數格式與逐秒遞減、背景分頁被節流後恢復仍正確、歸零只重讀一次、本機時鐘不準時 60 秒內不重複請求、分頁與檢視切換、離開時清除計時器，
 * 以及頂部餘額、簽到卡與兩張說明卡的文字對照（餘額只認非負整數、載入中與失敗顯示「—」）。
 * 純函式沒有請求與 DOM；頁面層的流程（簽到、重試、倒數與請求次數）在 wallet-hub-template.test.js。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('../../app/static/js/wallet/wallet-hub-state.js');

const T0 = Date.UTC(2026, 10, 18, 4, 0, 0);
const iso = (ms) => new Date(ms).toISOString();

// ---- 倒數的格式與計算 -------------------------------------------------------------------------------------------

test('the countdown is shown as hours:minutes:seconds, two digits each', () => {
  assert.equal(H.formatCountdown(((16 * 60 + 43) * 60 + 26) * 1000), '16:43:26');
  assert.equal(H.formatCountdown(1000), '00:00:01');
  assert.equal(H.formatCountdown(3600 * 1000), '01:00:00');
  assert.equal(H.formatCountdown(24 * 3600 * 1000), '24:00:00');
  assert.equal(H.formatCountdown(59 * 60 * 1000 + 59 * 1000), '00:59:59');
});

test('the countdown rounds up to the second, so only a true zero shows 00:00:00', () => {
  assert.equal(H.formatCountdown(1), '00:00:01');
  assert.equal(H.formatCountdown(400), '00:00:01');
  assert.equal(H.formatCountdown(1001), '00:00:02');
  assert.equal(H.formatCountdown(0), '00:00:00');
});

test('a negative, missing or broken duration shows 00:00:00 instead of a broken string', () => {
  for (const value of [-5000, NaN, Infinity, -Infinity, null, undefined, '1000', {}]) {
    assert.equal(H.formatCountdown(value), '00:00:00', String(value));
  }
});

test('the remaining time is the next reset minus now, never negative, and null when either time is not usable', () => {
  assert.equal(H.remainingMs(iso(T0 + 60000), T0), 60000);
  assert.equal(H.remainingMs(iso(T0), T0), 0);
  assert.equal(H.remainingMs(iso(T0 - 5000), T0), 0);                 // 已經過了
  assert.equal(H.remainingMs('2026-11-18T16:00:00+00:00', Date.UTC(2026, 10, 18, 4, 0, 0)), 12 * 3600 * 1000);   // 伺服器的 ISO 格式（帶 +00:00）
  for (const bad of [null, undefined, '', 'not a date', 12345, {}]) { assert.equal(H.remainingMs(bad, T0), null, String(bad)); }
  for (const bad of [NaN, Infinity, null, undefined, '5']) { assert.equal(H.remainingMs(iso(T0), bad), null, String(bad)); }
});

test('the countdown text counts down second by second because it is recomputed from the clock', () => {
  const target = iso(T0 + ((16 * 60 + 43) * 60 + 26) * 1000);
  assert.equal(H.countdownText(target, T0), '16:43:26');
  assert.equal(H.countdownText(target, T0 + 1000), '16:43:25');
  assert.equal(H.countdownText(target, T0 + 26 * 1000), '16:43:00');
  assert.equal(H.countdownText(target, T0 + 27 * 1000), '16:42:59');
  assert.equal(H.countdownText('壞掉', T0), '');                         // 時間不合法：不顯示倒數
});

test('a throttled background tab that wakes up much later still shows the right remaining time', () => {
  const target = iso(T0 + 10 * 3600 * 1000);
  assert.equal(H.countdownText(target, T0), '10:00:00');
  assert.equal(H.countdownText(target, T0 + 3 * 3600 * 1000 + 17 * 1000), '06:59:43');     // 中間完全沒有計時：以時鐘相減，不靠逐秒累減
  assert.equal(H.countdownText(target, T0 + 11 * 3600 * 1000), '00:00:00');                   // 睡過頭：直接歸零
});

// ---- 歸零要不要重讀 ---------------------------------------------------------------------------------------------

const AT_ZERO = { remaining: 0, claimable: false, reloading: false, lastReloadAt: null, nowMs: T0 };

test('reaching zero for the first time reloads the wallet', () => {
  assert.equal(H.shouldReloadAtZero(AT_ZERO), true);
});

test('before zero, while a reload is running, or once the wallet says claimable, nothing is reloaded', () => {
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, remaining: 1 }), false);
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, remaining: 5000 }), false);
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, remaining: null }), false);       // 時間不可用：沒有倒數就沒有歸零
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, reloading: true }), false);
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, claimable: true }), false);
});

test('after a reload that still cannot check in (clock off), nothing is reloaded for 60 seconds, then once again', () => {
  const afterReload = { ...AT_ZERO, lastReloadAt: T0 };
  assert.equal(H.RELOAD_GAP_MS, 60000);
  for (const seconds of [0, 1, 30, 59]) { assert.equal(H.shouldReloadAtZero({ ...afterReload, nowMs: T0 + seconds * 1000 }), false, `${seconds} 秒`); }
  assert.equal(H.shouldReloadAtZero({ ...afterReload, nowMs: T0 + 59999 }), false);
  assert.equal(H.shouldReloadAtZero({ ...afterReload, nowMs: T0 + 60000 }), true);
  assert.equal(H.shouldReloadAtZero({ ...afterReload, nowMs: T0 + 61000 }), true);
});

test('a missing or broken input never asks for a reload', () => {
  for (const bad of [undefined, null, {}, 'x', [], { remaining: '0' }, { remaining: false }]) { assert.equal(H.shouldReloadAtZero(bad), false, JSON.stringify(bad)); }
  assert.equal(H.shouldReloadAtZero({ ...AT_ZERO, lastReloadAt: 'yesterday' }), false);   // 上次時間壞掉：寧可不重讀（失敗時關閉，不空轉）
});

// ---- 計時器 -----------------------------------------------------------------------------------------------------

function fakeTimers() {
  const log = { set: [], cleared: [], ticks: [] };
  let now = T0;
  return {
    log, setNow: (value) => { now = value; },
    deps: {
      now: () => now,
      setInterval: (handler, ms) => { log.set.push(ms); log.handler = handler; return 42; },
      clearInterval: (handle) => { log.cleared.push(handle); },
      onTick: (value) => log.ticks.push(value),
    },
  };
}

test('the ticker ticks once at start and then once a second, and stops cleanly', () => {
  const timers = fakeTimers();
  const ticker = H.createTicker(timers.deps);
  assert.equal(ticker.isRunning(), false);
  ticker.start();
  assert.equal(ticker.isRunning(), true);
  assert.deepEqual(timers.log.set, [1000]);
  assert.deepEqual(timers.log.ticks, [T0]);                           // 立即一次：畫面不必等一秒才有數字
  timers.setNow(T0 + 1000);
  timers.log.handler();
  assert.deepEqual(timers.log.ticks, [T0, T0 + 1000]);
  ticker.stop();
  assert.equal(ticker.isRunning(), false);
  assert.deepEqual(timers.log.cleared, [42]);
});

test('starting twice opens only one timer, and stopping twice clears only once', () => {
  const timers = fakeTimers();
  const ticker = H.createTicker(timers.deps);
  ticker.start();
  ticker.start();
  assert.equal(timers.log.set.length, 1);
  assert.equal(timers.log.ticks.length, 1);
  ticker.stop();
  ticker.stop();
  assert.deepEqual(timers.log.cleared, [42]);
});

test('stopping a ticker that never ran does nothing, and it can start again after a stop', () => {
  const timers = fakeTimers();
  const ticker = H.createTicker(timers.deps);
  ticker.stop();
  assert.deepEqual(timers.log.cleared, []);
  ticker.start();
  ticker.stop();
  ticker.start();
  assert.equal(timers.log.set.length, 2);
  assert.equal(ticker.isRunning(), true);
});

// ---- 分頁與檢視 -------------------------------------------------------------------------------------------------

test('the page opens on the free tab in the main view', () => {
  assert.deepEqual(H.initialView(), { tab: 'free', view: 'main' });
  assert.deepEqual(H.TABS, ['free', 'topup']);
});

test('selecting a tab switches it, and an unknown tab changes nothing', () => {
  assert.deepEqual(H.selectTab({ tab: 'free', view: 'main' }, 'topup'), { tab: 'topup', view: 'main' });
  assert.deepEqual(H.selectTab({ tab: 'topup', view: 'main' }, 'free'), { tab: 'free', view: 'main' });
  for (const bad of ['records', '', null, undefined, 'FREE', '__proto__', 1]) {
    assert.deepEqual(H.selectTab({ tab: 'free', view: 'main' }, bad), { tab: 'free', view: 'main' }, String(bad));
  }
});

test('the records view hides the tabs: entering remembers the tab, tabs cannot change inside, and going back returns to it', () => {
  const inRecords = H.openRecords({ tab: 'topup', view: 'main' });
  assert.deepEqual(inRecords, { tab: 'topup', view: 'records' });
  assert.deepEqual(H.selectTab(inRecords, 'free'), { tab: 'topup', view: 'records' });     // 分頁列隱藏：不能切
  assert.deepEqual(H.closeRecords(inRecords), { tab: 'topup', view: 'main' });             // 返回後回到先前的分頁
  assert.deepEqual(H.closeRecords(H.openRecords(H.initialView())), { tab: 'free', view: 'main' });
});

test('the view functions return new objects and fall back to the defaults for a broken input', () => {
  const given = { tab: 'free', view: 'main' };
  assert.notEqual(H.selectTab(given, 'topup'), given);
  assert.deepEqual(given, { tab: 'free', view: 'main' });             // 不修改傳入的物件
  for (const bad of [undefined, null, 'x', 5, [], { tab: 'nope', view: 'nope' }]) {
    assert.deepEqual(H.openRecords(bad), { tab: 'free', view: 'records' }, JSON.stringify(bad));
    assert.deepEqual(H.closeRecords(bad), { tab: 'free', view: 'main' }, JSON.stringify(bad));
  }
});

test('the keyboard moves between the tabs: arrows wrap around, Home and End jump, other keys are ignored', () => {
  assert.equal(H.tabFromKey('free', 'ArrowRight'), 'topup');
  assert.equal(H.tabFromKey('topup', 'ArrowRight'), 'free');          // 循環
  assert.equal(H.tabFromKey('free', 'ArrowLeft'), 'topup');
  assert.equal(H.tabFromKey('topup', 'ArrowLeft'), 'free');
  assert.equal(H.tabFromKey('topup', 'Home'), 'free');
  assert.equal(H.tabFromKey('free', 'End'), 'topup');
  for (const key of ['Enter', ' ', 'Tab', 'a', 'ArrowUp', 'ArrowDown', '', undefined, null]) { assert.equal(H.tabFromKey('free', key), null, String(key)); }
  assert.equal(H.tabFromKey('nonsense', 'ArrowRight'), 'topup');      // 不認得的目前分頁當成第一個
});

// ---- 頂部餘額 ---------------------------------------------------------------------------------------------------

const READY = (extra) => ({ status: 'ready', total: 850, free: 850, paid: 0, exempt: false, freeCap: 1000, turnCost: 30, ...extra });

test('a normal player sees the total and the paid and free split', () => {
  const view = H.balanceView(READY());
  assert.equal(view.amountText, '850');
  assert.equal(view.splitText, '付費 0｜贈送 850');
  assert.equal(view.exempt, false);
  assert.equal(view.failed, false);
  assert.equal(view.loading, false);
  assert.equal(view.aria, '遊戲幣餘額 850');
  assert.equal(H.balanceView(READY({ total: 1230, free: 800, paid: 430 })).splitText, '付費 430｜贈送 800');
});

test('an admin sees the real balance and the not-consumed note', () => {
  const view = H.balanceView(READY({ exempt: true, total: 0, free: 0, paid: 0 }));
  assert.equal(view.amountText, '0');
  assert.equal(view.exempt, true);
  assert.equal(view.aria, '遊戲幣餘額 0（不消耗遊戲幣）');
});

test('loading and failed show a dash instead of a number, and failed asks for a retry', () => {
  const loading = H.balanceView({ status: 'loading', total: null, free: null, paid: null, exempt: false });
  assert.deepEqual([loading.amountText, loading.splitText, loading.failed, loading.loading], ['—', '', false, true]);
  assert.equal(loading.aria, '遊戲幣餘額讀取中');
  const idle = H.balanceView({ status: 'idle', total: null });
  assert.deepEqual([idle.amountText, idle.failed, idle.loading], ['—', false, true]);
  const failed = H.balanceView({ status: 'failed', total: null, free: null, paid: null, exempt: false });
  assert.deepEqual([failed.amountText, failed.splitText, failed.failed, failed.loading], ['—', '', true, false]);
  assert.equal(failed.aria, '遊戲幣餘額無法讀取');
});

test('only a non-negative integer is a balance; anything else is shown as a dash and the split is hidden', () => {
  for (const bad of [-1, 1.5, NaN, Infinity, '850', null, undefined, {}, [], true]) {
    const view = H.balanceView(READY({ total: bad }));
    assert.equal(view.amountText, '—', String(bad));
    assert.equal(view.splitText, '', String(bad));
  }
  assert.equal(H.balanceView(READY({ paid: -3 })).splitText, '');     // 付費或贈送其中之一不可用：整行不顯示，不顯示不確定的數字
  assert.equal(H.balanceView(READY({ free: '5' })).splitText, '');
  for (const bad of [undefined, null, 'x', 5, []]) { assert.equal(H.balanceView(bad).amountText, '—'); }
});

test('only the boolean true makes the account not-consumed', () => {
  for (const bad of ['true', 1, 'yes', null, undefined, {}, []]) { assert.equal(H.balanceView(READY({ exempt: bad })).exempt, false, String(bad)); }
  assert.equal(H.balanceView(READY({ exempt: true })).exempt, true);
});

// ---- 簽到卡 -----------------------------------------------------------------------------------------------------

const CARD = (extra) => H.checkinCardOf({ status: 'ready', claimable: true, finished: false, hasPreview: false, grant: 200, ...extra });

test('the check-in card offers the claim with the amount when the wallet says it can be claimed', () => {
  assert.deepEqual(CARD(), { mode: 'claimable', headline: '每日簽到可領取 200 遊戲幣。' });
  assert.deepEqual(CARD({ grant: null }), { mode: 'claimable', headline: '每日簽到可以領取免費遊戲幣。' });     // 不知道簽到數：不猜
  assert.deepEqual(CARD({ grant: 0 }), { mode: 'claimable', headline: '每日簽到可以領取免費遊戲幣。' });
  assert.deepEqual(CARD({ grant: -5 }), { mode: 'claimable', headline: '每日簽到可以領取免費遊戲幣。' });
});

test('the check-in card shows the preview choice while a preview waits', () => {
  assert.equal(CARD({ hasPreview: true }).mode, 'preview');
  assert.equal(CARD({ hasPreview: 'yes' }).mode, 'claimable');       // 只認布林 true
});

test('once checked in today (by the state machine or by the wallet) the card is done, whatever the stale snapshot says', () => {
  assert.equal(CARD({ finished: true }).mode, 'done');
  assert.equal(CARD({ finished: true, claimable: true }).mode, 'done');          // 這個頁面剛簽到：蓋過舊快照
  assert.equal(CARD({ claimable: false }).mode, 'done');
  assert.equal(CARD({ claimable: false }).headline, '今天已經簽到過了。');
});

test('the check-in card is loading, failed or unavailable instead of guessing', () => {
  assert.equal(CARD({ status: 'idle' }).mode, 'loading');
  assert.equal(CARD({ status: 'loading' }).mode, 'loading');
  assert.equal(CARD({ status: 'failed' }).mode, 'failed');
  assert.match(CARD({ status: 'failed' }).headline, /重試/);
  assert.equal(CARD({ claimable: null }).mode, 'unavailable');
  assert.equal(CARD({ claimable: 'true' }).mode, 'unavailable');     // 只認布林
  assert.equal(H.checkinCardOf(undefined).mode, 'loading');
  assert.equal(H.checkinCardOf(null).mode, 'loading');
});

// ---- 註冊贈幣卡與上限說明 ---------------------------------------------------------------------------------------

test('the sign-up gift card only explains the automatic grant: no button text, no amount', () => {
  const text = H.signupGiftText(READY());
  assert.match(text, /自動發放/);
  assert.doesNotMatch(text, /\d/);                                    // 贈幣的數量不在畫面上（錢包 API 沒有這個欄位）
  assert.doesNotMatch(text, /領取按鈕|點擊|按下/);
});

test('an admin is told that no sign-up gift is given, and nothing is shown before the wallet is read', () => {
  assert.equal(H.signupGiftText(READY({ exempt: true })), '管理員帳號不發放註冊贈幣。');
  for (const status of ['idle', 'loading', 'failed']) { assert.equal(H.signupGiftText(READY({ status, exempt: true })), '', status); }   // 還沒讀到就不說（避免管理員先看到一般說明）
  assert.equal(H.signupGiftText(undefined), '');
});

test('the free-coin cap note comes from the wallet and is never invented', () => {
  assert.match(H.freeCapNote(1000), /1000/);
  assert.match(H.freeCapNote(1500), /1500/);
  assert.match(H.freeCapNote(1000), /作廢/);
  for (const bad of [0, -1, 1.5, NaN, null, undefined, '1000', {}]) { assert.equal(H.freeCapNote(bad), '', String(bad)); }
});

test('every text of the page is plain: no markup characters in any fixed sentence', () => {
  const texts = [
    CARD().headline, CARD({ hasPreview: true }).headline, CARD({ finished: true }).headline, CARD({ status: 'failed' }).headline,
    CARD({ claimable: null }).headline, H.signupGiftText(READY()), H.signupGiftText(READY({ exempt: true })), H.freeCapNote(1000),
  ];
  for (const text of texts) { assert.doesNotMatch(text, /[<>]/); }
});
