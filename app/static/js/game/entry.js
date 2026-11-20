/* RPG 入口頁 `/game`（Vue 3 全域變數模式；見 rpg-entry-pages 與 rpg-script-catalog 規格）：選劇本與我的存檔兩個分頁。
 *
 * 規則：分頁與提示的顯示完全由 Vue 的響應式狀態接管（v-show／v-if），不使用 Bootstrap 的 JavaScript 與 data-bs-*；
 * 所有來自作者與玩家的文字以 {{ }} 插值顯示（純文字），不使用 v-html。清單內容不在初始狀態裡，由唯讀端點取得。
 * 無論成功或失敗，一律在 finally 恢復「載入更多」與重試；失敗時已載入的內容不消失。本頁只讀不寫：不碰遊戲幣、不寫任何瀏覽器儲存。
 * 預設分頁：掛載時並行載入兩個清單的第一頁與常用標籤；有存檔（清單非空）而玩家還沒手動切換時，自動切到「我的存檔」，否則維持「選劇本」。
 * 「選劇本」是公開目錄：關鍵字搜尋（按 Enter 或「搜尋」才送出）、常用標籤與卡片上的標籤、讀者頻道、清除篩選；條件改變就清空清單並從第一頁重載，
 * 「載入更多」沿用條件與 `next_cursor`，過時的回應由請求序號丟掉（狀態與規則在 catalog-state.js，Node 可測；這裡只接 Vue）。篩選條件只在頁面記憶體。
 * 卡片標題連到劇本詳情頁 `/game/script/<版本識別碼>`；「開始」只導向 `/game/start/<版本識別碼>`；帳號閘門非空時禁用（後端的閘門仍是唯一的把關者）。
 * 「我的存檔」仍以位移分頁（只有自己的存檔，數量小）。
 * 安全模式開的玩家點選未綁定安全模式的劇本時，先以自製 Modal 詢問是否修改安全模式（前往設定、維持安全模式繼續；Esc 與遮罩視同取消，留在目錄）；
 * 這只是詢問，不會改變任何模式，也不會自動調高。狀態機與文案在 mode-prompt.js（與劇本詳情頁共用），Modal 樣板在 game/_mode_prompt.html。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);
  var PAGE_PATH = '/game';
  var Catalog = Game.catalogState;

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
      /* 「要不要修改安全模式」的詢問：狀態機與文案與劇本詳情頁共用（mode-prompt.js；Modal 樣板是 game/_mode_prompt.html）。 */
      var prompt = Game.modePrompt.create(Vue, {
        mode: function () { return contentMode.value; },
        navigate: function (path) { window.location.assign(path); },
        startPath: Game.startPath,
        accountLink: Game.accountLink
      });
      var catalog = Vue.reactive(Catalog.blank());   // 公開目錄：清單、游標、套用中的條件、請求序號與常用標籤（catalog-state.js）
      var saves = Vue.reactive(blankList('sessions'));
      var searchText = Vue.ref('');                  // 搜尋框裡的文字（按 Enter 或「搜尋」才成為套用中的條件 `catalog.q`）
      var audienceOptions = [{ value: '', label: '全部頻道' }].concat(Catalog.AUDIENCE_VALUES.map(function (value) { return { value: value, label: Game.audienceLabel(value) }; }));

      var gateNotice = Vue.computed(function () { return Game.gateNotice(gate.value); });
      var modeNotice = Vue.computed(function () { return Game.contentModeNotice(contentMode.value); });
      var canStart = Vue.computed(function () { return !gate.value; });
      var memberText = Vue.computed(function () { return Game.memberLabel(user.member_tier); });
      var preferenceText = Vue.computed(function () { return user.story_preference && user.story_preference !== 'all' ? Game.audienceLabel(user.story_preference) : ''; });
      var hasFilters = Vue.computed(function () { return Catalog.hasFilters(catalog); });
      var emptyKind = Vue.computed(function () { return Catalog.emptyKind(catalog); });
      /* 標籤晶片：常用標籤；從卡片點選的標籤若不在常用標籤裡，也要在最前面顯示成選取狀態，玩家才能再點一次取消。 */
      var tagChips = Vue.computed(function () {
        var chips = catalog.tags.map(function (row) { return { tag: row.tag }; });
        if (catalog.tag !== '' && !chips.some(function (chip) { return Catalog.sameTag(chip.tag, catalog.tag); })) { chips.unshift({ tag: catalog.tag }); }
        return chips;
      });

      function applyFailure(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
        if (info.status === 401 && !leaving) {                // 掛載時有兩個並行的請求，登入過期時兩個都會 401：只導向一次
          leaving = true;
          window.location.assign(Auth.loginUrlFor(PAGE_PATH));
        }
      }

      /* 把一頁資料接在「我的存檔」清單後面。以 id 去重（兩次請求之間若有新存檔，位移會錯開，同一筆可能出現兩次）；
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

      /* 載入「我的存檔」的下一頁（重試也是同一個函式：失敗時位移沒有前進，所以會重試同一頁）。載入中再呼叫不會再送一次請求。 */
      function loadNext(list, fetchPage) {
        if (list.loading) { return Promise.resolve(false); }
        list.loading = true;
        list.error = '';
        return fetchPage({ offset: list.offset }).then(function (data) {
          var rows = data && Array.isArray(data[list.key]) ? data[list.key] : [];
          append(list, rows);
          list.hasMore = !!data && data.has_more === true && rows.length > 0;        // 空頁不可能還有下一頁：避免壞回應造成無止境的載入
          list.loaded = true;
          return true;
        }, function (info) {
          applyFailure(info);
          list.error = info.kind === 'canceled' ? '' : (info.message || '無法載入，請稍後再試。');
          return false;
        }).finally(function () { list.loading = false; });
      }

      /* 目錄的下一頁（第一頁、載入更多與重試都是它）：玩家目前的安全模式取自每一頁的 `content_mode`。 */
      function loadCatalog() {
        return Catalog.load(catalog, Game.api.catalog, {
          onPage: function (data) { if (data && data.content_mode && typeof data.content_mode === 'object') { contentMode.value = data.content_mode; } },
          onFailure: applyFailure
        });
      }
      function loadSaves() { return loadNext(saves, Game.api.sessions); }
      function loadTags() { return Catalog.loadTags(catalog, Game.api.catalogTags); }

      /* 套用新的條件：清空清單並從第一頁重載（過時的回應由請求序號丟掉）。 */
      function applyFilters(filters) {
        Catalog.reset(catalog, filters);
        return loadCatalog();
      }
      function submitSearch() {
        searchText.value = Catalog.normalizeFilters({ q: searchText.value }).q;
        return applyFilters(Catalog.withQuery(catalog, searchText.value));
      }
      function chooseTag(tag) { return applyFilters(Catalog.withTag(catalog, tag)); }
      function chooseAudience(value) { return applyFilters(Catalog.withAudience(catalog, value)); }
      function clearFilters() {
        searchText.value = '';
        return applyFilters({});
      }
      /* 從卡片點選標籤：條件在清單的最上方，所以套用之後回到頂端（沒有 scrollTo 的環境就略過）。 */
      function chooseCardTag(tag) {
        var loading = chooseTag(tag);
        try { if (typeof window.scrollTo === 'function') { window.scrollTo(0, 0); } } catch (error) { /* 捲動只是方便，失敗不影響篩選 */ }
        return loading;
      }
      function isTagOn(tag) { return Catalog.sameTag(catalog.tag, tag); }

      /* 玩家自己切換分頁（點分頁按鈕）：記下「玩家選過了」，之後不會再被預設分頁的判斷自動切走。 */
      function selectTab(name) {
        if (name !== 'scripts' && name !== 'saves') { return; }
        userPickedTab = true;
        tab.value = name;
        if (name === 'saves' && !saves.loaded && !saves.error && !saves.loading) { loadSaves(); }
      }

      /* 掛載：並行載入「選劇本」與「我的存檔」的第一頁與常用標籤，請求互不阻擋（標籤失敗只是不顯示晶片）。預設分頁依玩家有沒有存檔決定——存檔載入完成、非空，
         而且玩家還沒有手動切換過，就切到「我的存檔」；沒有存檔、存檔請求失敗（「我的存檔」分頁自己顯示錯誤與重試）、或玩家已經選過分頁，都維持現狀。 */
      function mountPage() {
        var catalogLoading = loadCatalog();
        var tagsLoading = loadTags();
        var savesLoading = loadSaves().then(function (loaded) {
          if (loaded && saves.items.length > 0 && !userPickedTab) { tab.value = 'saves'; }
          return loaded;
        });
        return Promise.all([catalogLoading, savesLoading, tagsLoading]);
      }

      /* 「開始」：閘門非空時不導向（按鈕本身也是禁用的）；其餘交給共用的詢問狀態機——需要詢問（安全模式開、未綁定的劇本）時只開啟詢問 Modal、不導向。
         回傳是否導向，供測試使用。 */
      function start(card) {
        if (gate.value) { return false; }
        return prompt.begin(card);
      }

      Vue.onMounted(mountPage);

      return {
        user: user, gate: gate, tab: tab, restriction: restriction, contentMode: contentMode, catalog: catalog, saves: saves,
        gateNotice: gateNotice, modeNotice: modeNotice, canStart: canStart, memberText: memberText, preferenceText: preferenceText,
        loadCatalog: loadCatalog, loadSaves: loadSaves, selectTab: selectTab, start: start,
        searchText: searchText, audienceOptions: audienceOptions, qMax: Catalog.Q_MAX_CHARS, hasFilters: hasFilters, emptyKind: emptyKind, tagChips: tagChips,
        submitSearch: submitSearch, chooseTag: chooseTag, chooseCardTag: chooseCardTag, chooseAudience: chooseAudience, clearFilters: clearFilters, isTagOn: isTagOn,
        askCard: prompt.target, askOpen: prompt.open, modePrompt: prompt.copy, goToSettings: prompt.goToSettings, keepSafe: prompt.keepSafe, cancelAsk: prompt.cancel,
        sessionPath: Game.sessionPath, detailPath: Game.scriptPath, tierLabel: Game.tierLabel, modeLabel: Game.modeLabel, audienceLabel: Game.audienceLabel, statusLabel: Game.statusLabel,
        formatTime: Game.formatTaipeiTime
      };
    }
  });
  UI.install(app);
  app.mount('#game-app');
})();
