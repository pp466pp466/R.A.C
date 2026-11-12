/* 遊戲頁的遊戲幣（餘額、不足時的處理、最小的每日簽到入口；見 rpg-wallet-ui 規格與 rpg-game-ui 的 design D8）。
 *
 * `Game.createWallet(deps)` 回傳一組響應式狀態與方法，由 game.js 注入依賴並接到樣板（這個檔案本身不碰 `window`、`document`、儲存與主控台，Node 可以用真的 Vue 與替身 API 測試）：
 *   deps.Vue        `{ ref, computed, watch }`（Vue 3 全域）
 *   deps.api        `{ wallet(options), checkin(confirmOverflow, options) }`（game-api.js；`GET /api/wallet`、`POST /api/wallet/checkin`）
 *   deps.toast      `(message) => void`（簽到成功的 Toast）
 *   deps.goLogin    登入已過期（401）時導向登入頁
 *   deps.noteFailure  選用；每個失敗的原始 info 都會先交給它（game.js 用來辨識「被鎖定」之類的受限狀態）
 *
 * 規則：
 * - **餘額以伺服器為準**：載入時讀 `GET /api/wallet` 的 `total`；每一輪結束用 `done.coins` 直接覆寫（不另外請求）；讀取失敗顯示「—」，不阻擋閱讀與輸入。
 * - **送出前的餘額不足只是提示，不阻止送出**（`lowBalance`／`lowBalanceText`）：快取的餘額可能過期，真正的拒絕永遠在伺服器（409 `insufficient_coins`）。
 * - 409 `insufficient_coins` 開自製 Modal（`openInsufficient`）：說明目前餘額與每輪所需、這一輪沒有扣款，並提供「每日簽到」與「關閉」；玩家的行動文字由呼叫端保留。
 *   **簽到成功不會自動重送**被擋的行動。
 * - 簽到：`claimCheckin` 呼叫 `POST /api/wallet/checkin`。409 `checkin_overflow` 回預覽（會入帳多少、會作廢多少）：「取消」不再呼叫（不消耗當日簽到）、
 *   「領取」才以 `confirm_overflow: true` 再呼叫一次。409 `already_checked_in` 顯示今日已簽到與下次可簽到的時間（台灣時間）。簽到進行中再按只會有一個請求，`finally` 恢復。
 * - 沒有儲值、訂閱與記錄頁（屬後續的 change）。這裡不記錄、不輸出任何內容。
 */
(function (root, factory) {
  'use strict';
  var Game = (typeof module === 'object' && module.exports) ? require('./game-state.js') : root.Game;
  var api = factory(Game);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.createWallet = api.createWallet;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var DASH = '—';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function noop() {}

  function createWallet(deps) {
    var ref = deps.Vue.ref;
    var computed = deps.Vue.computed;
    var watch = deps.Vue.watch;
    var api = deps.api;
    var toast = typeof deps.toast === 'function' ? deps.toast : noop;
    var goLogin = typeof deps.goLogin === 'function' ? deps.goLogin : noop;
    var noteFailure = typeof deps.noteFailure === 'function' ? deps.noteFailure : noop;

    var balance = ref(null);               // 合計餘額（免費＋付費）；null = 還不知道（顯示「—」）
    var turnCost = ref(null);              // 每輪幣價（`turn_cost.standard`）；缺值或不合理時為 null（不顯示幣價）
    var nextResetAt = ref(null);           // 下次可簽到的時間（UTC 的 ISO 字串）；只用來說明「今天已簽到」
    var coinModalOpen = ref(false);
    var coinModalText = ref('');
    var checkinBusy = ref(false);
    var checkinPreview = ref(null);        // { wouldGain, wouldLose }｜null：超過免費幣上限時等待玩家選「取消」或「領取」
    var checkinMessage = ref('');
    var checkinFinished = ref(false);      // 今天已經簽到（成功或伺服器說已簽到）：Modal 不再提供簽到按鈕
    var claimable = null;                  // 錢包說今天還能不能簽到（`checkin.claimable`）；不知道時為 null
    var openedFor = 'none';                // 'insufficient'｜'checkin'｜'none'：Modal 為何而開（取消預覽時決定要不要一併關掉）
    var loading = false;

    var balanceText = computed(function () { return balance.value === null ? DASH : String(balance.value); });
    var lowBalance = computed(function () { return balance.value !== null && turnCost.value !== null && balance.value < turnCost.value; });
    var lowBalanceText = computed(function () {
      if (!lowBalance.value) { return ''; }
      return '目前餘額 ' + balance.value + ' 幣，每一輪需要 ' + turnCost.value + ' 幣。可以先簽到領取免費遊戲幣；餘額以伺服器為準，仍可直接送出。';
    });

    // 玩家用 Esc 或點遮罩關掉 Modal（由 v-model 寫回 false）時，一併清掉等待中的預覽，下次打開不會殘留舊的選項
    watch(coinModalOpen, function (open) {
      if (!open) { checkinPreview.value = null; openedFor = 'none'; }
    }, { flush: 'sync' });

    function setBalance(value) {
      if (isCount(value)) { balance.value = value; return true; }
      return false;
    }

    /* 套用伺服器的錢包視圖（`GET /api/wallet` 的 `wallet`，以及簽到成功回的 `wallet`）。型別不對的欄位一律忽略。 */
    function applyWallet(wallet) {
      if (!isObject(wallet)) { return false; }
      var changed = setBalance(wallet.total);
      var cost = isObject(wallet.turn_cost) ? wallet.turn_cost.standard : undefined;
      if (isCount(cost) && cost > 0) { turnCost.value = cost; }
      var checkin = isObject(wallet.checkin) ? wallet.checkin : null;
      if (checkin && typeof checkin.next_reset_at === 'string') { nextResetAt.value = checkin.next_reset_at; }
      if (checkin && typeof checkin.claimable === 'boolean') { claimable = checkin.claimable; }
      return changed;
    }

    /* 載入錢包。永不拋出：失敗時餘額維持原狀（沒讀過就是「—」），時間軸與輸入照常可用。 */
    function load(options) {
      if (loading) { return Promise.resolve(false); }
      loading = true;
      return new Promise(function (resolve) { resolve(api.wallet(options)); }).then(function (data) {
        return isObject(data) ? applyWallet(data.wallet) : false;
      }, function (info) {
        noteFailure(info);
        var failure = Game.describeRequestFailure(info);
        if (failure.kind === 'login') { goLogin(); }
        return false;
      }).finally(function () { loading = false; });
    }

    // ---- 餘額不足 -----------------------------------------------------------------------------------
    function reset(reason) {
      checkinPreview.value = null;
      checkinMessage.value = '';
      checkinFinished.value = claimable === false;                    // 錢包已說今天簽過了就不再提供簽到按鈕
      openedFor = reason;
    }

    /* 伺服器回 409 `insufficient_coins`（`failure` 是 `Game.describeHttpFailure` 的結果，帶 `balance`／`needed`）：開 Modal，並以伺服器說的餘額更新顯示。 */
    function openInsufficient(failure) {
      var known = isObject(failure) ? failure : {};
      if (isCount(known.balance)) { balance.value = known.balance; }
      var needed = isCount(known.needed) ? known.needed : turnCost.value;
      var head = balance.value !== null && needed !== null
        ? '遊戲幣不足：目前餘額 ' + balance.value + ' 幣，每一輪需要 ' + needed + ' 幣。'
        : '遊戲幣不足。';
      coinModalText.value = head + '這一輪沒有扣除遊戲幣，你的行動文字還保留在輸入框裡。';
      reset('insufficient');
      coinModalOpen.value = true;
    }

    function closeCoinModal() {
      coinModalOpen.value = false;
      checkinPreview.value = null;
    }

    // ---- 簽到 -------------------------------------------------------------------------------------------
    function openForCheckin() {
      if (coinModalOpen.value) { return; }
      coinModalText.value = balance.value !== null ? '目前餘額 ' + balance.value + ' 幣。每日簽到可以領取免費遊戲幣。' : '每日簽到可以領取免費遊戲幣。';
      reset('checkin');
      coinModalOpen.value = true;
    }

    function succeeded(data) {
      var applied = isObject(data) ? applyWallet(data.wallet) : false;
      checkinPreview.value = null;
      checkinFinished.value = true;
      claimable = false;
      var current = balance.value !== null ? '目前餘額 ' + balance.value + ' 幣。' : '';
      checkinMessage.value = '簽到成功。' + current + '你可以關閉這個視窗後再次送出行動；系統不會自動送出。';
      toast('簽到成功。' + current);
      if (!applied) { return load(); }                               // 回應沒帶錢包：重讀一次，餘額以伺服器為準
      return Promise.resolve(true);
    }

    function failed(info) {
      noteFailure(info);
      var failure = Game.describeCheckinFailure(info, { nextResetAt: nextResetAt.value });
      if (failure.kind === 'canceled') { return false; }
      if (failure.kind === 'login') { goLogin(); return false; }
      if (failure.kind === 'overflow') {
        openForCheckin();
        checkinPreview.value = failure.checkin;                      // 取不到預覽（欄位缺漏）時為 null：只顯示說明，玩家重新簽到即可
        checkinMessage.value = failure.message;
        return false;
      }
      openForCheckin();
      checkinPreview.value = null;
      checkinMessage.value = failure.message;
      if (failure.kind === 'already') { checkinFinished.value = true; claimable = false; }
      return false;
    }

    function submit(confirmOverflow) {
      if (checkinBusy.value) { return Promise.resolve(false); }      // 重複點擊：進行中只會有一個請求
      checkinBusy.value = true;
      return new Promise(function (resolve) { resolve(api.checkin(confirmOverflow === true)); }).then(succeeded, failed).finally(function () {
        checkinBusy.value = false;
      });
    }

    /* 「每日簽到」按鈕（輸入區的提示與 Modal 共用）。等待預覽選擇時不會重送。 */
    function claimCheckin() {
      if (checkinBusy.value || checkinPreview.value) { return Promise.resolve(false); }
      return submit(false);
    }

    /* 預覽的「領取」：這時才帶 `confirm_overflow: true`。沒有預覽時什麼都不做（不會未經確認就以確認的方式簽到）。 */
    function confirmOverflow() {
      if (checkinBusy.value || !checkinPreview.value) { return Promise.resolve(false); }
      return submit(true);
    }

    /* 預覽的「取消」：不呼叫任何請求，也不消耗當日簽到。 */
    function cancelOverflow() {
      if (checkinBusy.value || !checkinPreview.value) { return false; }
      checkinPreview.value = null;
      if (openedFor === 'checkin') {
        coinModalOpen.value = false;
        toast('已取消，今天的簽到還沒有使用。');
      } else {
        checkinMessage.value = '已取消，今天的簽到還沒有使用。';
      }
      return true;
    }

    return {
      balance: balance, turnCost: turnCost, balanceText: balanceText, lowBalance: lowBalance, lowBalanceText: lowBalanceText,
      coinModalOpen: coinModalOpen, coinModalText: coinModalText, checkinBusy: checkinBusy, checkinPreview: checkinPreview,
      checkinMessage: checkinMessage, checkinFinished: checkinFinished,
      load: load, setBalance: setBalance, openInsufficient: openInsufficient, closeCoinModal: closeCoinModal,
      claimCheckin: claimCheckin, confirmOverflow: confirmOverflow, cancelOverflow: cancelOverflow
    };
  }

  return { createWallet: createWallet };
});
