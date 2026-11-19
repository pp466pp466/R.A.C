/* 錢包頁 `/wallet` 的 Vue 掛載與接線（Vue 3 全域變數模式；見 wallet-hub-page 規格與 wallet-hub 的 design D4、D6、D7、D8、D10）。
 *
 * 這是唯一直接碰 `window`、`document` 的錢包頁腳本：規則都在別的模組裡（wallet-hub-state.js 的純函式與計時器管理、ui/wallet-store.js 的全站狀態庫、
 * ui/wallet-checkin.js 的簽到狀態機、game/game-state.js 的簽到失敗訊息），這裡只負責建立它們、把依賴接上，並提供只屬於畫面的東西：
 * - **錢包只有一份**：餘額、付費與贈送、免費幣上限、簽到狀態都讀全站的 `UI.wallet`（與其他頁面頂列的餘額標籤是同一個狀態庫）；本頁沒有標籤，所以由這裡在掛載時呼叫
 *   `load()`（整頁一個 `GET /api/wallet`，並行合併、不快取、不寫任何瀏覽器儲存）。讀取失敗時顯示「—」與「重試」，不顯示不確定的數字。
 * - **簽到**與遊戲進行頁是同一個狀態機（`UI.createCheckin`）：重複點擊只一個請求、`finally` 恢復、取消不送請求、領取才帶 `confirm_overflow`、成功寫回狀態庫並顯示 Toast。
 *   這個頁面沒有 Modal，預覽與說明直接顯示在簽到卡內；失敗訊息仍由 `Game.describeCheckinFailure` 整理（所以載入 game-labels.js 與 game-state.js，見 design D4）。
 *   整個頁面**唯一的寫入請求就是 `POST /api/wallet/checkin`**，沒有任何增加餘額的路徑。
 * - **倒數**：今天已簽到時，簽到卡每秒以「下次可簽到時間減目前時間」重算（不是每秒減一），只在看得到倒數時才有計時器（離開免費分頁、進入紀錄、卸載與 `pagehide` 都停止）。
 *   歸零時重讀一次錢包（`load({ force: true })`），若重讀後仍不能簽到（本機時鐘不準），兩次重讀至少相隔 60 秒。
 * - **分頁與檢視**由 Vue 的響應式變數接管（`tab`、`view`）：不使用 Bootstrap 的 JavaScript 與 data-bs-*、不新增路由、不記住（不寫任何瀏覽器儲存）；`tablist` 支援方向鍵與 Home／End。
 * - **儲值（示意）**：`topup` 由 wallet-demo-packages.js 的資料經 `Wallet.topupViewOf` 組成卡片（design D9）；沒有任何請求，樣板裡的按鈕一律停用，這裡也沒有任何購買的方法。
 * - **紀錄**：第一次進入才讀 `GET /api/wallet/entries` 的第一頁，「載入更多」用 `next_before` 當游標；清單留在頁內記憶體，返回再進入不重新請求（失敗時等玩家按「重試」）；
 *   這個頁面簽到成功後清單作廢（`resetRecords`，帳本多了一筆），仍在途中的舊請求回來時因世代不同被丟棄。紀錄只用 GET；整個頁面唯一的寫入請求仍是簽到。
 * - **使用說明**：頂列「使用說明」按鈕開啟共用的 `ui-modal`（`guideOpen`，開關由 Vue 狀態接管），內容由 `Wallet.guideOf` 依錢包組成。
 * 所有文字由樣板以 {{ }} 插值顯示（純文字），這裡不輸出到主控台、不放網址、不寫瀏覽器儲存。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var Wallet = window.Wallet;
  var WalletDemo = window.WalletDemo;                          // 示意幣包資料（wallet-demo-packages.js，唯一的位置）；沒載入時儲值分頁只說「尚未開放」
  var state = Auth.readInitialState(document);
  var PAGE_PATH = '/wallet';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  var app = Vue.createApp({
    setup: function () {
      var ref = Vue.ref;
      var computed = Vue.computed;
      var store = UI.wallet;                                    // 全站共用的錢包狀態庫（ui-components.js 建立）

      var memberText = computed(function () { return Game.memberLabel(isObject(state) ? state.member_tier : undefined); });
      var restriction = ref(null);
      var tab = ref('free');                                    // 預設「免費」：玩家唯一能「做事」的分頁（簽到）
      var view = ref('main');                                   // 'main'｜'records'（紀錄是頁內檢視，沒有獨立網址）
      var now = ref(Date.now());                                // 倒數用的目前時間（只由計時器更新）
      var checkinFailed = ref(false);                           // 最近一次簽到是因為連線或伺服器問題沒有完成（說明用警示呈現）
      var leaving = false;                                      // 非響應式：已經導向登入頁
      var reloading = false;                                    // 非響應式：倒數歸零的重讀進行中
      var lastReloadAt = null;                                  // 非響應式：上次倒數歸零重讀開始的時間

      function goLogin() {
        if (leaving) { return; }                                // 掛載時的讀取與簽到都可能 401：只導向一次
        leaving = true;
        window.location.assign(Auth.loginUrlFor(PAGE_PATH));
      }

      /* 失敗的原始 info：被鎖定或暫停發佈時顯示申訴捷徑，登入過期時導向登入頁。狀態庫的每個失敗與簽到的每個失敗都經過這裡。 */
      function applyFailure(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
        if (isObject(info) && info.status === 401) { goLogin(); }
      }
      store.onFailure(applyFailure);

      // ---- 簽到（與遊戲進行頁共用的狀態機）------------------------------------------------------------
      var checkin = UI.createCheckin({
        Vue: { ref: ref }, api: UI.walletApi, store: store,
        describeFailure: function (info, options) { return Game.describeCheckinFailure(info, options); },
        hooks: {
          onFailure: applyFailure,
          onLogin: goLogin,
          onError: function () { checkinFailed.value = true; },       // 網路、逾時、繁忙與未知：說明用警示呈現（預覽與已簽到不是錯誤）
          onSuccess: function (current) { resetRecords(); UI.toast('簽到成功。' + current); }       // 不回字串：卡片內顯示預設的成功說明；帳本多了一筆，已載入的紀錄清單作廢
        }
      });

      function claimCheckin() { checkinFailed.value = false; return checkin.claim(); }
      function confirmOverflow() { checkinFailed.value = false; return checkin.confirmOverflow(); }
      function cancelOverflow() { return checkin.cancelOverflow(); }

      /* 讀取錢包（共用狀態庫；永不拋出）。讀到之後讓簽到狀態機依錢包判斷今天能不能簽；`force`（重試、倒數歸零）重讀的是剛從伺服器來的快照，
         比「這個頁面看過今天簽過了」更新，所以一併清掉那個記憶（新的一天又可以簽到）。 */
      function loadWallet(settings) {
        var force = isObject(settings) && settings.force === true;
        return store.load(force ? { force: true } : undefined).then(function (loaded) {
          if (loaded) { checkin.reset({ authoritative: force }); }
          return loaded;
        });
      }

      function retryWallet() { return loadWallet({ force: true }); }

      // ---- 畫面的衍生值 -------------------------------------------------------------------------------
      var balance = computed(function () { return Wallet.balanceView(store.state); });
      var checkinCard = computed(function () {
        var current = store.state.checkin;
        return Wallet.checkinCardOf({
          status: store.state.status, claimable: current.claimable, finished: checkin.finished.value,
          hasPreview: checkin.preview.value !== null, grant: current.grant
        });
      });
      var countdownText = computed(function () { return Wallet.countdownText(store.state.checkin.nextResetAt, now.value); });
      var signupGift = computed(function () { return Wallet.signupGiftText(store.state); });
      var capNote = computed(function () { return Wallet.freeCapNote(store.state.freeCap); });

      // ---- 分頁與檢視 -----------------------------------------------------------------------------------
      function currentView() { return { tab: tab.value, view: view.value }; }
      function applyView(next) { tab.value = next.tab; view.value = next.view; }
      function selectTab(name) { applyView(Wallet.selectTab(currentView(), name)); }
      function closeRecords() { applyView(Wallet.closeRecords(currentView())); }

      // ---- 紀錄檢視（帳本）---------------------------------------------------------------------------------
      // 清單保留在頁內記憶體（design D10）：第一次進入才請求，返回再進入不重新請求（除非玩家按「重試」）；離開頁面就丟棄，不寫任何瀏覽器儲存。
      // 唯一例外：這個頁面簽到成功後，帳本多了一筆，已載入的清單作廢，下次進入再讀（resetRecords）。
      var recordItems = ref([]);                                // 已載入的列（Wallet.entryViewOf 的形狀），由新到舊
      var recordsPhase = ref('idle');                           // 第一頁：idle｜loading｜ready｜failed
      var recordsError = ref('');                               // 第一頁失敗的說明
      var nextBefore = ref(null);                               // 下一頁的游標；null＝沒有更舊的了
      var moreBusy = ref(false);                                // 正在載入更多
      var moreError = ref('');                                  // 載入更多失敗的說明（已載入的清單保留）
      var recordsGeneration = 0;                                // 非響應式：清單作廢後，舊請求的回應一律丟棄

      /* 失敗：登入過期導向登入頁、被鎖定顯示申訴捷徑（applyFailure），回傳給玩家看的說明。 */
      function recordFailure(info) { applyFailure(info); return Wallet.entriesFailureText(info); }

      function loadFirstRecords() {
        if (recordsPhase.value === 'loading') { return Promise.resolve(false); }
        var generation = recordsGeneration;
        recordsPhase.value = 'loading';
        recordsError.value = '';
        return UI.walletApi.entries().then(function (response) {
          if (generation !== recordsGeneration) { return false; }
          var page = Wallet.entriesPageOf(response, Game.formatTaipeiTime);
          if (page === null) { throw new Error('malformed entries'); }
          recordItems.value = page.items;
          nextBefore.value = page.nextBefore;
          recordsPhase.value = 'ready';
          return true;
        }).then(undefined, function (info) {
          if (generation !== recordsGeneration) { return false; }
          recordsPhase.value = 'failed';
          recordsError.value = recordFailure(info);
          return false;
        });
      }

      function loadMoreRecords() {
        if (recordsPhase.value !== 'ready' || moreBusy.value || nextBefore.value === null) { return Promise.resolve(false); }
        var generation = recordsGeneration;
        moreBusy.value = true;
        moreError.value = '';
        return UI.walletApi.entries(nextBefore.value).then(function (response) {
          if (generation !== recordsGeneration) { return false; }
          var page = Wallet.entriesPageOf(response, Game.formatTaipeiTime);
          if (page === null) { throw new Error('malformed entries'); }
          recordItems.value = Wallet.mergeEntries(recordItems.value, page.items);
          nextBefore.value = page.nextBefore;
          return true;
        }).then(undefined, function (info) {
          if (generation !== recordsGeneration) { return false; }
          moreError.value = recordFailure(info);                // 已載入的清單保留
          return false;
        }).then(function (loaded) {
          if (generation === recordsGeneration) { moreBusy.value = false; }
          return loaded;
        });
      }

      /* 「重試」：第一頁失敗就重讀第一頁，否則（載入更多失敗）用同一個游標再載入一次。 */
      function retryRecords() { return recordsPhase.value === 'failed' ? loadFirstRecords() : loadMoreRecords(); }

      /* 清單作廢（回到沒讀過的狀態）；進行中的請求回來時因為世代不同而被丟棄。 */
      function resetRecords() {
        recordsGeneration += 1;
        recordItems.value = [];
        recordsPhase.value = 'idle';
        recordsError.value = '';
        nextBefore.value = null;
        moreBusy.value = false;
        moreError.value = '';
      }

      function openRecords() {
        applyView(Wallet.openRecords(currentView()));
        return recordsPhase.value === 'idle' ? loadFirstRecords() : Promise.resolve(false);       // 讀過（或正在讀、失敗等玩家按重試）就不再請求
      }

      var recordsView = computed(function () {
        return Wallet.recordsViewOf({
          phase: recordsPhase.value, items: recordItems.value, error: recordsError.value,
          hasMore: nextBefore.value !== null, moreBusy: moreBusy.value, moreError: moreError.value
        });
      });

      // ---- 儲值（示意）與使用說明 ----------------------------------------------------------------------------
      // 儲值分頁沒有任何請求與增加餘額的路徑：卡片的資料只來自 wallet-demo-packages.js，按鈕在樣板裡一律停用。
      var topup = computed(function () { return Wallet.topupViewOf(WalletDemo, store.state.turnCost); });
      var guide = computed(function () { return Wallet.guideOf(store.state); });
      var guideOpen = ref(false);                               // 使用說明 Modal 的開關（頂列「使用說明」按鈕開啟；由 Vue 狀態接管）
      function openGuide() { guideOpen.value = true; }

      /* `tablist` 的鍵盤操作：方向鍵、Home、End 切換分頁並把焦點移到新的分頁（沒有 DOM 時只切換）。 */
      function onTabKeydown(event) {
        var next = Wallet.tabFromKey(tab.value, event && event.key);
        if (next === null) { return; }
        if (typeof event.preventDefault === 'function') { event.preventDefault(); }
        selectTab(next);
        Vue.nextTick(function () {
          var element = document.getElementById('wh-tab-' + next);
          if (element && typeof element.focus === 'function') { element.focus(); }
        });
      }

      // ---- 倒數 -----------------------------------------------------------------------------------------
      /* 倒數歸零：重讀一次錢包；進行中不重複，重讀之後仍不能簽到時兩次重讀至少相隔 60 秒（wallet-hub-state.js 的 shouldReloadAtZero）。 */
      function reloadAtZero(nowMs) {
        lastReloadAt = nowMs;
        reloading = true;
        function done() { reloading = false; }
        return loadWallet({ force: true }).then(done, done);
      }

      function onTick(nowMs) {
        now.value = nowMs;
        var remaining = Wallet.remainingMs(store.state.checkin.nextResetAt, nowMs);
        var reload = Wallet.shouldReloadAtZero({
          remaining: remaining, claimable: store.state.checkin.claimable, reloading: reloading, lastReloadAt: lastReloadAt, nowMs: nowMs
        });
        if (reload) { reloadAtZero(nowMs); }
      }

      var ticker = Wallet.createTicker({
        now: function () { return Date.now(); },
        setInterval: function (handler, ms) { return window.setInterval(handler, ms); },
        clearInterval: function (handle) { window.clearInterval(handle); },
        onTick: onTick
      });
      // 只有「看得到倒數」時才有計時器：主檢視的免費分頁、今天已簽到、而且下次可簽到的時間可用。
      // 不用同步的 watch：狀態庫的 `apply` 是一欄一欄寫入的，同步觸發會在寫到一半（例如已簽到但下次時間還是舊的）時啟動計時器並誤判「歸零」；等這一輪寫完才判斷。
      var counting = computed(function () {
        return view.value === 'main' && tab.value === 'free' && checkinCard.value.mode === 'done' && countdownText.value !== '';
      });
      Vue.watch(counting, function (on) { if (on) { ticker.start(); } else { ticker.stop(); } });
      Vue.onBeforeUnmount(function () { ticker.stop(); });
      window.addEventListener('pagehide', function () { ticker.stop(); });

      Vue.onMounted(function () { return loadWallet(); });

      return {
        memberText: memberText, restriction: restriction, balance: balance, view: view, tab: tab,
        checkinCard: checkinCard, checkinBusy: checkin.busy, checkinMessage: checkin.message, checkinFailed: checkinFailed,
        countdownText: countdownText, signupGift: signupGift, capNote: capNote, topup: topup, guide: guide, guideOpen: guideOpen, recordsView: recordsView,
        retryWallet: retryWallet, openGuide: openGuide, openRecords: openRecords, closeRecords: closeRecords, selectTab: selectTab, onTabKeydown: onTabKeydown,
        loadMoreRecords: loadMoreRecords, retryRecords: retryRecords,
        claimCheckin: claimCheckin, confirmOverflow: confirmOverflow, cancelOverflow: cancelOverflow
      };
    }
  });
  UI.install(app);
  app.mount('#wallet-app');
})();
