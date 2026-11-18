/* 每日簽到的狀態機（與介面無關；見 wallet-hub 的 design D4 與 rpg-wallet-ui、wallet-hub-page 規格）。
 *
 * `UI.createCheckin(deps)` 回傳簽到流程的響應式狀態與動作。進行頁（game-wallet.js，簽到 Modal）與錢包頁（頁內的簽到卡）共用同一份流程，
 * 兩處的行為因此一致；兩者的差異只在「畫面怎麼呈現」，經 `deps.hooks` 交給呼叫端。這個檔案不碰 `window`、`document`、儲存與主控台，Node 可以用真的 Vue 與替身 API 測試。
 *   deps.Vue             `{ ref }`（Vue 3 全域）
 *   deps.api             `{ checkin(confirmOverflow) }`；`POST /api/wallet/checkin`，成功回 `{ ok, wallet }`，失敗以整理過的 info 拒絕
 *   deps.store           全站的錢包狀態庫（wallet-store.js）：讀 `state.total`、`state.checkin.claimable`、`state.checkin.nextResetAt`，成功時 `apply(wallet)`、沒有錢包時 `load({ force: true })`
 *   deps.describeFailure `(info, { nextResetAt }) => { kind, message, checkin }`；由呼叫端注入（遊戲頁與錢包頁都是 `Game.describeCheckinFailure`，不搬進這裡：它依賴
 *                        game-state.js 內部的失敗分類與 game-labels.js 的文字，搬動會牽動整個失敗分類）。kind：canceled｜login｜overflow｜already｜其他（網路、逾時、繁忙…）
 *   deps.hooks           選用，全部可省略，缺的就什麼都不做（hook 拋出的錯誤不吞掉，與原本的行為相同）：
 *     onFailure(info)      每個失敗的原始 info，在分類之前（遊戲頁用來辨識「被鎖定」之類的受限狀態）
 *     onLogin()            401：登入已過期（導向登入頁）
 *     onOverflow(failure)  409 `checkin_overflow`：在寫入預覽與說明之前（遊戲頁在這裡打開簽到 Modal）
 *     onAlready(failure)   409 `already_checked_in`：同上
 *     onError(failure)     其他失敗（網路、逾時、繁忙、未知）：同上
 *     onSuccess(current)   簽到成功、狀態已更新之後；`current` 是「目前餘額 N 幣。」（餘額未知時是空字串）。回傳字串就取代預設的成功說明（`簽到成功。` ＋ current）
 *     onCanceled(text)     玩家取消預覽之後；`text` 是取消的說明。回傳 `true` 表示頁面已用別的方式呈現（例如關掉視窗並以 Toast 顯示），就不再寫入 `message`
 *
 * 狀態與動作（回傳的物件）：
 *   busy        簽到請求進行中
 *   preview     `{ wouldGain, wouldLose }`｜null：會使免費幣超過上限時，等待玩家選「取消」或「領取」
 *   message     最近一次結果的說明（繁體中文）
 *   finished    今天已經簽到（成功、伺服器說已簽到，或錢包說今天不能簽）：不再提供簽到按鈕
 *   claim()           「每日簽到」；進行中或等待預覽選擇時不送請求，回傳 Promise<boolean>（成功為 true；失敗與未送出為 false）
 *   confirmOverflow() 預覽的「領取」：這時才帶 `confirm_overflow: true`；沒有預覽時什麼都不做（不會未經確認就以確認的方式簽到）
 *   cancelOverflow()  預覽的「取消」：不呼叫任何請求，也不消耗當日簽到
 *   reset()           清掉預覽與說明，並依錢包重新判斷 `finished`（呼叫端在重新打開簽到畫面時使用）
 *   dropPreview()     只清掉預覽（呼叫端的視窗被 Esc 或遮罩關掉時使用）
 *
 * 規則：
 * - **簽到進行中再按只會有一個請求**，`finally` 一定恢復 `busy`；任何失敗路徑都不留下卡住的按鈕。
 * - 取消預覽**不送請求**；只有「領取」才帶 `confirm_overflow: true`，一般簽到永遠是 `false`。
 * - 成功時狀態一律經狀態庫（`apply`），回應沒帶錢包才重讀一次（餘額以伺服器為準）；這裡不自己改餘額。
 * - 這個頁面看過「今天簽過了」（成功或伺服器說已簽到）之後，蓋過錢包的 `claimable`，避免舊快照又開放簽到。
 * - 這裡不記錄、不輸出任何內容，也不保存失敗訊息之外的東西。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UI = root.UI || {};
    root.UI.createCheckin = api.createCheckin;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var SUCCESS_TEXT = '簽到成功。';
  var CANCELED_TEXT = '已取消，今天的簽到還沒有使用。';
  var FALLBACK_TEXT = '簽到沒有完成，請稍後再試。';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  /* 呼叫端沒有注入 describeFailure 時的保底：不開任何特殊流程，只說明沒有完成。 */
  function fallbackFailure() { return { kind: 'unknown', message: FALLBACK_TEXT, checkin: null }; }

  function createCheckin(deps) {
    var ref = deps.Vue.ref;
    var api = deps.api;
    var store = deps.store;
    var describeFailure = typeof deps.describeFailure === 'function' ? deps.describeFailure : fallbackFailure;
    var hooks = isObject(deps.hooks) ? deps.hooks : {};

    var busy = ref(false);
    var preview = ref(null);
    var message = ref('');
    var finished = ref(false);
    var claimedToday = false;              // 這個頁面已經看到「今天簽過了」；為真時蓋過錢包的 `claimable`

    function call(name, argument) {
      var hook = hooks[name];
      return typeof hook === 'function' ? hook(argument) : undefined;
    }

    /* 錢包說今天還能不能簽到（`checkin.claimable`）；不知道時為 null */
    function claimable() { return claimedToday ? false : store.state.checkin.claimable; }

    function reset() {
      preview.value = null;
      message.value = '';
      finished.value = claimable() === false;                       // 錢包已說今天簽過了就不再提供簽到按鈕
    }

    function dropPreview() { preview.value = null; }

    function succeeded(data) {
      var applied = isObject(data) ? store.apply(data.wallet) : false;
      preview.value = null;
      finished.value = true;
      claimedToday = true;
      var total = store.state.total;
      var current = total !== null ? '目前餘額 ' + total + ' 幣。' : '';
      var custom = call('onSuccess', current);
      message.value = typeof custom === 'string' ? custom : SUCCESS_TEXT + current;
      if (!applied) { return store.load({ force: true }); }         // 回應沒帶錢包：重讀一次，餘額以伺服器為準
      return Promise.resolve(true);
    }

    function failed(info) {
      call('onFailure', info);
      var described = describeFailure(info, { nextResetAt: store.state.checkin.nextResetAt });
      var failure = isObject(described) ? described : fallbackFailure();
      if (failure.kind === 'canceled') { return false; }
      if (failure.kind === 'login') { call('onLogin'); return false; }
      if (failure.kind === 'overflow') {
        call('onOverflow', failure);
        preview.value = isObject(failure.checkin) ? failure.checkin : null;   // 取不到預覽（欄位缺漏）時為 null：只顯示說明，玩家重新簽到即可
        message.value = failure.message;
        return false;
      }
      call(failure.kind === 'already' ? 'onAlready' : 'onError', failure);
      preview.value = null;
      message.value = failure.message;
      if (failure.kind === 'already') { finished.value = true; claimedToday = true; }
      return false;
    }

    function submit(confirmOverflow) {
      if (busy.value) { return Promise.resolve(false); }            // 重複點擊：進行中只會有一個請求
      busy.value = true;
      return new Promise(function (resolve) { resolve(api.checkin(confirmOverflow === true)); }).then(succeeded, failed).finally(function () {
        busy.value = false;
      });
    }

    function claim() {
      if (busy.value || preview.value) { return Promise.resolve(false); }
      return submit(false);
    }

    function confirmOverflow() {
      if (busy.value || !preview.value) { return Promise.resolve(false); }
      return submit(true);
    }

    function cancelOverflow() {
      if (busy.value || !preview.value) { return false; }
      preview.value = null;
      if (call('onCanceled', CANCELED_TEXT) !== true) { message.value = CANCELED_TEXT; }
      return true;
    }

    return {
      busy: busy, preview: preview, message: message, finished: finished,
      claim: claim, confirmOverflow: confirmOverflow, cancelOverflow: cancelOverflow, reset: reset, dropPreview: dropPreview
    };
  }

  return { createCheckin: createCheckin };
});
