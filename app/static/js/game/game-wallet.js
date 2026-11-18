/* 遊戲頁的遊戲幣（餘額、不足時的處理、最小的每日簽到入口；見 rpg-wallet-ui 規格與 rpg-game-ui 的 design D8）。
 *
 * `Game.createWallet(deps)` 回傳一組響應式狀態與方法，由 game.js 注入依賴並接到樣板（這個檔案本身不碰 `window`、`document`、儲存與主控台，Node 可以用真的 Vue 與替身 API 測試）：
 *   deps.Vue        `{ ref, computed, watch }`（Vue 3 全域）
 *   deps.api        `{ wallet(options), checkin(confirmOverflow, options) }`（game-api.js；`GET /api/wallet`、`POST /api/wallet/checkin`）
 *   deps.store      選用；全站共用的錢包狀態庫（`UI.wallet`，wallet-store.js）。game.js 傳入它，頂列的餘額標籤與這裡讀寫同一份狀態、整頁只有一個 `GET /api/wallet`；
 *                   沒給時以 `UI.createWalletStore` 建一份本地的（讀取走 `deps.api.wallet`），Node 測試不必改。
 *   deps.toast      `(message) => void`（簽到成功的 Toast）
 *   deps.goLogin    登入已過期（401）時導向登入頁
 *   deps.noteFailure  選用；每個失敗的原始 info 都會先交給它（game.js 用來辨識「被鎖定」之類的受限狀態）
 *
 * 規則：
 * - **餘額以伺服器為準**：載入時讀 `GET /api/wallet` 的 `total`；每一輪結束用 `done.coins` 直接覆寫（不另外請求）；讀取失敗顯示「—」，不阻擋閱讀與輸入。
 *   餘額、每輪幣價與管理員旗標都是狀態庫的衍生值（`balance`、`turnCost`、`exempt` 是唯讀的 computed；下次簽到時間由簽到狀態機直接讀狀態庫），寫入一律經狀態庫（`apply`、`setTotal`），
 *   所以頂列標籤與輸入區的「餘額不足」提示永遠依同一份數字。對外的欄位與方法沒有改變。
 * - **送出前的餘額不足只是提示，不阻止送出**（`lowBalance`／`lowBalanceText`）：快取的餘額可能過期，真正的拒絕永遠在伺服器（409 `insufficient_coins`）。
 * - **管理員不消耗遊戲幣**（admin-unlimited-coins）：錢包回應的 `coin_exempt` 只認布林（缺值或型別不對視為 `false`），暴露為 `exempt`；為真時 `lowBalance` 為假、`lowBalanceText` 為空字串。
 *   畫面只讀這個欄位、不看角色或其他欄位自行推論；真正的豁免永遠在伺服器，這裡只負責說明文字（修訂的費用文字見 game-revision.js 與 game-labels.js）。每份錢包回應都是完整的快照，`exempt` 隨之覆寫。
 * - 409 `insufficient_coins` 開自製 Modal（`openInsufficient`）：說明目前餘額與每輪所需、這一輪沒有扣款，並提供「每日簽到」與「關閉」；玩家的行動文字由呼叫端保留。
 *   **簽到成功不會自動重送**被擋的行動。
 * - 簽到：`claimCheckin` 呼叫 `POST /api/wallet/checkin`。409 `checkin_overflow` 回預覽（會入帳多少、會作廢多少）：「取消」不再呼叫（不消耗當日簽到）、
 *   「領取」才以 `confirm_overflow: true` 再呼叫一次。409 `already_checked_in` 顯示今日已簽到與下次可簽到的時間（台灣時間）。簽到進行中再按只會有一個請求，`finally` 恢復。
 *   **這套狀態機在 `ui/wallet-checkin.js`（`UI.createCheckin`，錢包頁共用同一份）**；這裡只決定簽到 Modal 的開關與文字，經 hooks 接上：失敗的訊息仍由 `Game.describeCheckinFailure`
 *   整理（注入給 `createCheckin`，沒有搬走：它依賴 game-state.js 內部的失敗分類與 game-labels.js 的文字），所以進行頁與錢包頁的簽到流程一致、只有呈現不同。
 * - 這裡沒有儲值、訂閱與記錄（錢包頁 `/wallet` 提供記錄與示意的儲值畫面）。這裡不記錄、不輸出任何內容。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module.exports;
  var Game = isNode ? require('./game-state.js') : root.Game;
  var UI = isNode ? Object.assign({}, require('../ui/wallet-store.js'), require('../ui/wallet-checkin.js')) : root.UI;
  var api = factory(Game, UI);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.createWallet = api.createWallet;
  }
})(typeof self !== 'undefined' ? self : this, function (Game, UI) {
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
    // 沒有注入狀態庫時（測試）建本地的；Vue 只給 ref／computed／watch 時，以 ref 包住物件取得深層響應式
    var store = deps.store || UI.createWalletStore({
      reactive: typeof deps.Vue.reactive === 'function' ? deps.Vue.reactive : function (value) { return ref(value).value; },
      fetcher: function (settings) { return api.wallet(settings); }
    });
    var toast = typeof deps.toast === 'function' ? deps.toast : noop;
    var goLogin = typeof deps.goLogin === 'function' ? deps.goLogin : noop;
    var noteFailure = typeof deps.noteFailure === 'function' ? deps.noteFailure : noop;

    var balance = computed(function () { return store.state.total; });               // 合計餘額（免費＋付費）；null = 還不知道（顯示「—」）
    var turnCost = computed(function () { return store.state.turnCost; });           // 每輪幣價（`turn_cost.standard`）；缺值或不合理時為 null（不顯示幣價）
    var exempt = computed(function () { return store.state.exempt === true; });     // 這個帳號不消耗遊戲幣（`coin_exempt`，管理員）；缺值、型別不對或載入失敗時為 false
    var coinModalOpen = ref(false);
    var coinModalText = ref('');
    var openedFor = 'none';                // 'insufficient'｜'checkin'｜'none'：Modal 為何而開（取消預覽時決定要不要一併關掉）

    /* 簽到狀態機（ui/wallet-checkin.js，與錢包頁共用）。`busy`、`preview`、`message`、`finished` 與三個動作原樣對外；Modal 的開關與文字經 hooks 在這裡決定。 */
    var checkin = UI.createCheckin({
      Vue: { ref: ref }, api: api, store: store,
      describeFailure: function (info, options) { return Game.describeCheckinFailure(info, options); },
      hooks: {
        onFailure: function (info) { noteFailure(info); },
        onLogin: function () { goLogin(); },
        onOverflow: function () { openForCheckin(); },                   // 取不到預覽（欄位缺漏）時預覽為 null：只顯示說明，玩家重新簽到即可
        onAlready: function () { openForCheckin(); },
        onError: function () { openForCheckin(); },
        onSuccess: function (current) {
          toast('簽到成功。' + current);
          return '簽到成功。' + current + '你可以關閉這個視窗後再次送出行動；系統不會自動送出。';
        },
        onCanceled: function (text) {
          if (openedFor !== 'checkin') { return false; }                 // 從「餘額不足」的 Modal 來的：Modal 留著，說明顯示在裡面
          coinModalOpen.value = false;                                    // 從輸入區的提示來的：沒有別的內容，直接關掉並以 Toast 說明
          toast(text);
          return true;
        }
      }
    });

    var balanceText = computed(function () { return balance.value === null ? DASH : String(balance.value); });
    var lowBalance = computed(function () { return !exempt.value && balance.value !== null && turnCost.value !== null && balance.value < turnCost.value; });
    var lowBalanceText = computed(function () {
      if (!lowBalance.value) { return ''; }
      return '目前餘額 ' + balance.value + ' 幣，每一輪需要 ' + turnCost.value + ' 幣。可以先簽到領取免費遊戲幣；餘額以伺服器為準，仍可直接送出。';
    });

    // 玩家用 Esc 或點遮罩關掉 Modal（由 v-model 寫回 false）時，一併清掉等待中的預覽，下次打開不會殘留舊的選項
    watch(coinModalOpen, function (open) {
      if (!open) { checkin.dropPreview(); openedFor = 'none'; }
    }, { flush: 'sync' });

    function setBalance(value) { return store.setTotal(value); }

    /* 載入錢包（共用狀態庫；同一頁並行的請求會合併，已讀到就不重複請求）。永不拋出：失敗時餘額維持原狀（沒讀過就是「—」），時間軸與輸入照常可用。
       失敗的原始 info 由下面訂閱的 `onFailure` 處理（交給 noteFailure、登入過期時導向登入頁），不論請求是誰發起的。 */
    function load(options) { return store.load(options); }

    store.onFailure(function (info) {
      noteFailure(info);
      var failure = Game.describeRequestFailure(info);
      if (failure.kind === 'login') { goLogin(); }
    });

    // ---- 餘額不足 -----------------------------------------------------------------------------------
    function reset(reason) {
      checkin.reset();                                                // 清掉預覽與說明；錢包已說今天簽過了就不再提供簽到按鈕
      openedFor = reason;
    }

    /* 伺服器回 409 `insufficient_coins`（`failure` 是 `Game.describeHttpFailure` 的結果，帶 `balance`／`needed`）：開 Modal，並以伺服器說的餘額更新顯示。 */
    function openInsufficient(failure, options) {
      var known = isObject(failure) ? failure : {};
      if (isCount(known.balance)) { store.setTotal(known.balance); }
      var needed = isCount(known.needed) ? known.needed : turnCost.value;
      var head = balance.value !== null && needed !== null
        ? '遊戲幣不足：目前餘額 ' + balance.value + ' 幣，每一輪需要 ' + needed + ' 幣。'
        : '遊戲幣不足。';
      // 重新生成與編輯最後一輪（`options.revision`）：沒有行動文字留在輸入框，保留的是原本的回應
      coinModalText.value = head + (isObject(options) && options.revision === true ? '這次修改沒有扣除遊戲幣，原本的回應已保留。' : '這一輪沒有扣除遊戲幣，你的行動文字還保留在輸入框裡。');
      reset('insufficient');
      coinModalOpen.value = true;
    }

    function closeCoinModal() {
      coinModalOpen.value = false;
      checkin.dropPreview();
    }

    // ---- 簽到（狀態機在 ui/wallet-checkin.js；這裡只有 Modal 的開啟）-----------------------------------------------
    /* 簽到的結果要給玩家看（預覽、已簽到、失敗）時打開 Modal；已經開著（從「餘額不足」來的）就沿用，不重設。 */
    function openForCheckin() {
      if (coinModalOpen.value) { return; }
      coinModalText.value = balance.value !== null ? '目前餘額 ' + balance.value + ' 幣。每日簽到可以領取免費遊戲幣。' : '每日簽到可以領取免費遊戲幣。';
      reset('checkin');
      coinModalOpen.value = true;
    }

    return {
      balance: balance, turnCost: turnCost, exempt: exempt, balanceText: balanceText, lowBalance: lowBalance, lowBalanceText: lowBalanceText,
      coinModalOpen: coinModalOpen, coinModalText: coinModalText, checkinBusy: checkin.busy, checkinPreview: checkin.preview,
      checkinMessage: checkin.message, checkinFinished: checkin.finished,
      load: load, setBalance: setBalance, openInsufficient: openInsufficient, closeCoinModal: closeCoinModal,
      claimCheckin: checkin.claim, confirmOverflow: checkin.confirmOverflow, cancelOverflow: checkin.cancelOverflow
    };
  }

  return { createWallet: createWallet };
});
