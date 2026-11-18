/* 全站共用的錢包狀態庫（與框架無關；見 shared-wallet-balance 規格與 wallet-hub 的 design D1、D2）。
 *
 * `createWalletStore({ reactive, fetcher })` 回傳 `{ state, load, apply, setTotal, onFailure }`。風格同 `toast-store.js`：
 * 瀏覽器由 ui-components.js 注入 `Vue.reactive` 並建立全站單例 `UI.wallet`；Node 測試用一般物件與替身的 fetcher。
 *
 * 狀態（`state`）：
 *   status     'idle'｜'loading'｜'ready'｜'failed'
 *   total      合計餘額（免費＋付費）；非負整數｜null（還不知道，標籤顯示「—」）
 *   free、paid 免費與付費餘額；非負整數｜null
 *   exempt     這個帳號不消耗遊戲幣（`coin_exempt`，管理員）；只認布林 true，缺值、型別不對與載入失敗都是 false。畫面只讀這個欄位，不自行依角色推論
 *   freeCap    免費幣上限；正整數｜null
 *   turnCost   每輪幣價（`turn_cost.standard`）；正整數｜null（管理員的 0 不採用，所以不顯示幣價）
 *   checkin    { claimable（布林｜null）, grant, wouldGain, wouldLose（非負整數｜null）, nextResetAt（字串｜null）}
 *
 * 規則：
 * - `load()` 永不拋出，回傳 Promise<boolean>（有套用到伺服器的錢包資料為 true）。**並行的呼叫合併**成同一個請求；`ready` 之後直接回傳、不重複請求
 *   （換頁才重讀；倒數歸零與「重試」由呼叫端明確要求 `load({ force: true })`）；失敗時 `status` 為 `failed`，之後的 `load()` 可以重試。
 *   `failed` 與 `loading` 的失敗不會洗掉已經讀到的數字：重新讀取失敗時餘額維持原狀。
 * - `apply(wallet)`：套用 `GET /api/wallet` 與簽到成功回的 `wallet`（完整快照）。**只接受非負整數與布林**，型別不對的欄位忽略（不覆蓋原值）；
 *   `coin_exempt` 每次都依這份快照覆寫（缺值或型別不對視為 false）。回傳是否採用了合計餘額。套用成功代表已經讀到，`status` 變 `ready`。
 * - `setTotal(n)`：回合結束的 `done.coins`；非負整數才採用。
 * - `onFailure(listener)`：每次載入失敗時，以 fetcher 拒絕的原始值呼叫 `listener`（不論是誰發起的請求；回傳取消訂閱的函式）。
 *   遊戲頁用它辨識「登入已過期」與「被鎖定」之類的受限狀態；標籤本身不使用。狀態庫自己不保存、不輸出失敗內容；listener 拋出的錯誤被吞掉。
 * - `walletLabelOf(state)`（純函式，餘額標籤用）：回傳 `{ amount, exempt, aria }`。`amount` 只在合計是非負整數時為數字字串，其餘（載入中、失敗）是「—」；
 *   `exempt` 只認布林 true；`aria` 是完整的無障礙名稱（例如「遊戲幣餘額 170，前往錢包」）。標籤以 `{{ }}` 純文字插值顯示這些字串，不解析任何標記。
 * - **只存在頁面記憶體**：不寫 localStorage、sessionStorage、IndexedDB 與 cookie，不輸出主控台，不保存錯誤訊息；換頁後重新讀取。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UI = root.UI || {};
    root.UI.createWalletStore = api.createWalletStore;
    root.UI.walletLabelOf = api.walletLabelOf;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  function createWalletStore(options) {
    options = options || {};
    var reactive = typeof options.reactive === 'function' ? options.reactive : function (value) { return value; };
    var fetcher = typeof options.fetcher === 'function' ? options.fetcher : function () { return Promise.reject(new Error('no fetcher')); };

    var state = reactive({
      status: 'idle', total: null, free: null, paid: null, exempt: false, freeCap: null, turnCost: null,
      checkin: { claimable: null, grant: null, wouldGain: null, wouldLose: null, nextResetAt: null }
    });
    var pending = null;                      // 進行中的載入（並行合併）
    var listeners = [];                      // onFailure 的訂閱者

    function applyCheckin(raw) {
      if (!isObject(raw)) { return; }
      var target = state.checkin;
      if (typeof raw.claimable === 'boolean') { target.claimable = raw.claimable; }
      if (isCount(raw.grant)) { target.grant = raw.grant; }
      if (isCount(raw.would_gain)) { target.wouldGain = raw.would_gain; }
      if (isCount(raw.would_lose)) { target.wouldLose = raw.would_lose; }
      if (typeof raw.next_reset_at === 'string' && raw.next_reset_at) { target.nextResetAt = raw.next_reset_at; }
    }

    function apply(wallet) {
      if (!isObject(wallet)) { return false; }
      var adopted = false;
      if (isCount(wallet.total)) { state.total = wallet.total; adopted = true; }
      if (isCount(wallet.free_balance)) { state.free = wallet.free_balance; }
      if (isCount(wallet.paid_balance)) { state.paid = wallet.paid_balance; }
      if (isCount(wallet.free_cap) && wallet.free_cap > 0) { state.freeCap = wallet.free_cap; }
      state.exempt = wallet.coin_exempt === true;                           // 只認布林 true；每份快照都覆寫
      var cost = isObject(wallet.turn_cost) ? wallet.turn_cost.standard : undefined;
      if (isCount(cost) && cost > 0) { state.turnCost = cost; }
      applyCheckin(wallet.checkin);
      if (adopted) { state.status = 'ready'; }
      return adopted;
    }

    function setTotal(value) {
      if (!isCount(value)) { return false; }
      state.total = value;
      if (state.status !== 'ready') { state.status = 'ready'; }
      return true;
    }

    /* `settings.force` 為真時，即使已經 `ready` 也重讀一次（倒數歸零、重試）；進行中的請求仍會合併。 */
    function load(settings) {
      if (pending) { return pending; }
      var force = isObject(settings) && settings.force === true;
      if (state.status === 'ready' && !force) { return Promise.resolve(true); }
      if (state.status !== 'ready') { state.status = 'loading'; }
      var request = new Promise(function (resolve) { resolve(fetcher(isObject(settings) ? settings : undefined)); }).then(function (data) {
        var adopted = isObject(data) ? apply(data.wallet) : false;
        if (!adopted && state.status !== 'ready') { state.status = 'failed'; }
        return adopted;
      }, function (failure) {
        if (state.status !== 'ready') { state.status = 'failed'; }          // 已經讀過的數字不因為重讀失敗而消失
        listeners.slice().forEach(function (listener) { try { listener(failure); } catch (ignored) { /* 訂閱者的錯誤不影響狀態庫 */ } });
        return false;
      }).then(function (adopted) {
        pending = null;
        return adopted;
      });
      pending = request;
      return request;
    }

    function onFailure(listener) {
      if (typeof listener !== 'function') { return function () {}; }
      listeners.push(listener);
      return function () { listeners = listeners.filter(function (item) { return item !== listener; }); };
    }

    return { state: state, load: load, apply: apply, setTotal: setTotal, onFailure: onFailure };
  }

  var DASH = '—';

  function walletLabelOf(state) {
    var known = isObject(state) && isCount(state.total);
    var exempt = isObject(state) && state.exempt === true;
    var aria;
    if (known) { aria = '遊戲幣餘額 ' + state.total + (exempt ? '（不消耗遊戲幣）' : '') + '，前往錢包'; }
    else if (isObject(state) && state.status === 'failed') { aria = '遊戲幣餘額無法讀取，前往錢包'; }
    else { aria = '遊戲幣餘額讀取中，前往錢包'; }
    return { amount: known ? String(state.total) : DASH, exempt: exempt, aria: aria };
  }

  return { createWalletStore: createWalletStore, walletLabelOf: walletLabelOf };
});
