/* RPG 入口頁 `/game`（Vue 3 全域變數模式；見 rpg-entry-pages 規格）：選劇本與我的存檔兩個分頁。
 *
 * 規則：分頁與提示的顯示完全由 Vue 的響應式狀態接管（v-show／v-if），不使用 Bootstrap 的 JavaScript 與 data-bs-*；
 * 所有來自作者與玩家的文字以 {{ }} 插值顯示（純文字），不使用 v-html。清單內容不在初始狀態裡，由兩個唯讀端點取得。
 * 無論成功或失敗，一律在 finally 恢復「載入更多」與重試；失敗時已載入的內容不消失。本頁只讀不寫：不碰遊戲幣、不寫任何瀏覽器儲存。
 * 「開始」只導向 `/game/start/<版本識別碼>`；帳號閘門非空時禁用（後端的閘門仍是唯一的把關者）。
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
      var tab = Vue.ref('scripts');
      var restriction = Vue.ref(null);
      var contentMode = Vue.ref(null);
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
        if (info.status === 401) { window.location.assign(Auth.loginUrlFor(PAGE_PATH)); }
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

      function selectTab(name) {
        if (name !== 'scripts' && name !== 'saves') { return; }
        tab.value = name;
        if (name === 'saves' && !saves.loaded && !saves.error && !saves.loading) { loadSaves(); }
      }

      /* 「開始」：閘門非空時不導向（按鈕本身也是禁用的）。回傳是否導向，供測試使用。 */
      function start(card) {
        if (gate.value || !card || typeof card.id !== 'string') { return false; }
        window.location.assign(Game.startPath(card.id));
        return true;
      }

      Vue.onMounted(loadCatalog);

      return {
        user: user, gate: gate, tab: tab, restriction: restriction, contentMode: contentMode, catalog: catalog, saves: saves,
        gateNotice: gateNotice, modeNotice: modeNotice, canStart: canStart, memberText: memberText, preferenceText: preferenceText,
        loadCatalog: loadCatalog, loadSaves: loadSaves, selectTab: selectTab, start: start,
        sessionPath: Game.sessionPath, tierLabel: Game.tierLabel, audienceLabel: Game.audienceLabel, statusLabel: Game.statusLabel,
        formatTime: Game.formatTaipeiTime
      };
    }
  });
  UI.install(app);
  app.mount('#game-app');
})();
