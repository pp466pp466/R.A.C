/* RPG 入口頁 `/game`（Vue 3 全域變數模式；見 rpg-entry-pages 規格）：選劇本與我的存檔兩個分頁。
 *
 * 規則：分頁與提示的顯示完全由 Vue 的響應式狀態接管（v-show／v-if），不使用 Bootstrap 的 JavaScript 與 data-bs-*；
 * 所有來自作者與玩家的文字以 {{ }} 插值顯示（純文字），不使用 v-html。清單內容不在初始狀態裡，由兩個唯讀端點取得。
 * 無論成功或失敗，一律在 finally 恢復「載入更多」與重試；失敗時已載入的內容不消失。本頁只讀不寫：不碰遊戲幣、不寫任何瀏覽器儲存。
 * 預設分頁：掛載時並行載入兩個清單的第一頁；有存檔（清單非空）而玩家還沒手動切換時，自動切到「我的存檔」，否則維持「選劇本」。
 * 「開始」只導向 `/game/start/<版本識別碼>`；帳號閘門非空時禁用（後端的閘門仍是唯一的把關者）。
 * 安全模式開的玩家點選未綁定安全模式的劇本時，先以自製 Modal 詢問是否修改安全模式（前往設定、維持安全模式繼續；Esc 與遮罩視同取消，留在目錄）；
 * 這只是詢問，不會改變任何模式，也不會自動調高。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);
  var PAGE_PATH = '/game';

  function blankList(key) {
    return { key: key, items: [], offset: 0, hasMore: false, loading: false, loaded: false, error: '' };
  }

  var app = Vue.createApp({
    setup: function () {
      var user = state.user || {};
      var gate = Vue.ref(typeof state.gate === 'string' && state.gate ? state.gate : null);
      var tab = Vue.ref('scripts');                  // 預設「選劇本」；有存檔時掛載後會自動切到「我的存檔」（玩家沒有手動切換過的話）
      var userPickedTab = false;                     // 非響應式：玩家手動切換過分頁
      var leaving = false;                           // 非響應式：已經導向登入頁
      var restriction = Vue.ref(null);
      var contentMode = Vue.ref(null);               // 目錄回應的 `content_mode`：{ effective, forced }（玩家目前的安全模式與緊急煞車）
      var askCard = Vue.ref(null);                   // 點選了未綁定安全模式的劇本、正在等玩家回答「要不要修改安全模式」的卡片
      var modePrompt = Game.modePromptCopy();
      var catalog = Vue.reactive(blankList('scripts'));
      var saves = Vue.reactive(blankList('sessions'));

      var gateNotice = Vue.computed(function () { return Game.gateNotice(gate.value); });
      var modeNotice = Vue.computed(function () { return Game.contentModeNotice(contentMode.value); });
      var canStart = Vue.computed(function () { return !gate.value; });
      var memberText = Vue.computed(function () { return Game.memberLabel(user.member_tier); });
      var preferenceText = Vue.computed(function () { return user.story_preference && user.story_preference !== 'all' ? Game.audienceLabel(user.story_preference) : ''; });

      function applyFailure(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
        if (info.status === 401 && !leaving) {                // 掛載時有兩個並行的請求，登入過期時兩個都會 401：只導向一次
          leaving = true;
          window.location.assign(Auth.loginUrlFor(PAGE_PATH));
        }
      }

      /* 把一頁資料接在清單後面。以 id 去重（兩次請求之間若有新劇本上架，位移會錯開，同一筆可能出現兩次）；
         位移依伺服器實際回的筆數前進，不依去重後的筆數。 */
      function append(list, rows) {
        var known = {};
        list.items.forEach(function (item) { known[item.id] = true; });
        rows.forEach(function (row) {
          if (row && typeof row === 'object' && typeof row.id === 'string' && !known[row.id]) {
            known[row.id] = true;
            list.items.push(row);
          }
        });
        list.offset += rows.length;
      }

      /* 載入下一頁（重試也是同一個函式：失敗時位移沒有前進，所以會重試同一頁）。載入中再呼叫不會再送一次請求。 */
      function loadNext(list, fetchPage) {
        if (list.loading) { return Promise.resolve(false); }
        list.loading = true;
        list.error = '';
        return fetchPage({ offset: list.offset }).then(function (data) {
          var rows = data && Array.isArray(data[list.key]) ? data[list.key] : [];
          append(list, rows);
          list.hasMore = !!data && data.has_more === true && rows.length > 0;        // 空頁不可能還有下一頁：避免壞回應造成無止境的載入
          list.loaded = true;
          if (list.key === 'scripts' && data && data.content_mode && typeof data.content_mode === 'object') { contentMode.value = data.content_mode; }
          return true;
        }, function (info) {
          applyFailure(info);
          list.error = info.kind === 'canceled' ? '' : (info.message || '無法載入，請稍後再試。');
          return false;
        }).finally(function () { list.loading = false; });
      }

      function loadCatalog() { return loadNext(catalog, Game.api.catalog); }
      function loadSaves() { return loadNext(saves, Game.api.sessions); }

      /* 玩家自己切換分頁（點分頁按鈕）：記下「玩家選過了」，之後不會再被預設分頁的判斷自動切走。 */
      function selectTab(name) {
        if (name !== 'scripts' && name !== 'saves') { return; }
        userPickedTab = true;
        tab.value = name;
        if (name === 'saves' && !saves.loaded && !saves.error && !saves.loading) { loadSaves(); }
      }

      /* 掛載：並行載入「選劇本」與「我的存檔」的第一頁，兩個請求互不阻擋。預設分頁依玩家有沒有存檔決定——存檔載入完成、非空，而且玩家還沒有手動切換過，
         就切到「我的存檔」；沒有存檔、存檔請求失敗（「我的存檔」分頁自己顯示錯誤與重試）、或玩家已經選過分頁，都維持現狀。 */
      function mountPage() {
        var catalogLoading = loadCatalog();
        var savesLoading = loadSaves().then(function (loaded) {
          if (loaded && saves.items.length > 0 && !userPickedTab) { tab.value = 'saves'; }
          return loaded;
        });
        return Promise.all([catalogLoading, savesLoading]);
      }

      /* 安全模式開（生效模式為 safe）、緊急煞車未啟用的玩家點選**未綁定**安全模式的劇本（卡片 `content_tier` 為 adult）時，先詢問要不要修改安全模式。
         綁定的劇本、已關閉安全模式、緊急煞車、或沒有拿到模式資料時都不詢問（真正的生效模式永遠由伺服器現算，這裡只決定要不要先問一句）。 */
      function needsModeAsk(card) {
        var mode = contentMode.value;
        return !!mode && mode.effective === 'safe' && mode.forced !== true && card.content_tier === 'adult';
      }

      /* 「開始」：閘門非空時不導向（按鈕本身也是禁用的）；需要詢問時只開啟詢問 Modal、不導向。回傳是否導向，供測試使用。 */
      function start(card) {
        if (gate.value || !card || typeof card.id !== 'string') { return false; }
        if (needsModeAsk(card)) { askCard.value = card; return false; }
        window.location.assign(Game.startPath(card.id));
        return true;
      }

      /* 詢問 Modal 的顯示完全由 Vue 狀態接管（`askCard` 有值就是開啟）；Esc 與點遮罩由 ui-modal 設為 false，視同取消：留在目錄、沒有導向。 */
      var askOpen = Vue.computed({
        get: function () { return askCard.value !== null; },
        set: function (open) { if (!open) { askCard.value = null; } }
      });

      /* 「前往設定」：到帳號設定頁，改完回到這個劇本的精靈頁（返回位置是 /game/start/<版本識別碼>）。 */
      function goToSettings() {
        var card = askCard.value;
        if (!card) { return false; }
        askCard.value = null;
        window.location.assign(Game.accountLink(Game.startPath(card.id)));
        return true;
      }

      /* 「維持安全模式繼續」：安全模式設定不變，以安全模式進入精靈頁。 */
      function keepSafe() {
        var card = askCard.value;
        if (!card) { return false; }
        askCard.value = null;
        window.location.assign(Game.startPath(card.id));
        return true;
      }

      function cancelAsk() { askCard.value = null; }

      Vue.onMounted(mountPage);

      return {
        user: user, gate: gate, tab: tab, restriction: restriction, contentMode: contentMode, catalog: catalog, saves: saves,
        gateNotice: gateNotice, modeNotice: modeNotice, canStart: canStart, memberText: memberText, preferenceText: preferenceText,
        loadCatalog: loadCatalog, loadSaves: loadSaves, selectTab: selectTab, start: start,
        askCard: askCard, askOpen: askOpen, modePrompt: modePrompt, goToSettings: goToSettings, keepSafe: keepSafe, cancelAsk: cancelAsk,
        sessionPath: Game.sessionPath, tierLabel: Game.tierLabel, modeLabel: Game.modeLabel, audienceLabel: Game.audienceLabel, statusLabel: Game.statusLabel,
        formatTime: Game.formatTaipeiTime
      };
    }
  });
  UI.install(app);
  app.mount('#game-app');
})();
