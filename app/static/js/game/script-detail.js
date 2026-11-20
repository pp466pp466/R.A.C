/* 劇本詳情頁 `/game/script/<版本識別碼>`（Vue 3 全域變數模式；見 rpg-script-catalog 規格「詳情頁」與 rpg-entry-pages 規格「點選未綁定劇本時詢問是否修改安全模式」）。
 *
 * 規則：顯示與按鈕的禁用完全由 Vue 的響應式狀態接管（v-if／v-show），不使用 Bootstrap 的 JavaScript 與互動屬性；所有來自作者的文字（標題、簡介、世界名稱、標籤、筆名、
 * 背景、角色與地點）以 {{ }} 插值顯示（純文字），不使用 v-html。頁面外殼沒有劇本內容，也不含版本識別碼：識別碼只在網址裡，這裡自己讀取，
 * 資料來自唯讀的詳情端點（`Game.api.scriptDetail`；只有公開的白名單欄位，沒有隱藏特質、里程碑、結局說明、規則與初始對話）。本頁只讀不寫：不碰遊戲幣、不寫任何瀏覽器儲存。
 * 階段（`phase`）：loading → ready｜notfound（404：下架、他人的草稿與不存在都是同一個 404，顯示「找不到這個劇本」與返回目錄）｜failed（其他失敗，附重試）。
 * 無論成功或失敗，一律在 finally 恢復載入狀態；401 導向登入並保留位置；被鎖定等受限狀態顯示對應的提示。
 * 「開始冒險」導向 `/game/start/<id>`；帳號閘門非空時禁用（後端的閘門仍是唯一的把關者）。安全模式開的玩家對**未綁定**安全模式的劇本按下時，
 * 先以與入口頁同一個自製 Modal 詢問是否修改安全模式（狀態機與文案在 mode-prompt.js，Modal 樣板是 game/_mode_prompt.html；Esc 與遮罩視同取消，留在本頁）。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);
  var HOME_PATH = '/game';
  var LOAD_FAILED = '暫時無法載入這個劇本，請稍後再試。';

  function text(value) { return typeof value === 'string' ? value : ''; }
  function strings(value) { return Array.isArray(value) ? value.filter(function (item) { return typeof item === 'string' && item !== ''; }) : []; }

  /* 版本識別碼只在網址裡（頁面內容不含它）：`/game/script/<id>` 裡 `script` 後面的那一段。讀不出來（沒有那一段、編碼壞掉）回傳空字串。 */
  function versionIdFromPath(pathname) {
    var parts = String(pathname || '').split('/').filter(Boolean);
    var at = parts.indexOf('script');
    var segment = at >= 0 && at + 1 < parts.length ? parts[at + 1] : '';
    try { return decodeURIComponent(segment); } catch (error) { return ''; }
  }

  /* 把詳情回應整理成畫面用的形狀：每個欄位都是預期的型別（陣列一定是陣列、文字一定是字串），角色只取姓名與職業。
     沒有字串的 `id` 與 `title` 的回應看不懂，回傳 null（畫面當成暫時失敗，可以重試）。 */
  function normalizeDetail(data) {
    if (!data || typeof data !== 'object' || typeof data.id !== 'string' || data.id === '' || typeof data.title !== 'string') { return null; }
    return {
      id: data.id,
      title: data.title,
      summary: text(data.summary),
      world_name: text(data.world_name),
      tags: strings(data.tags),
      author_name: text(data.author_name),
      author_code: text(data.author_code),
      content_tier: text(data.content_tier),
      audience: text(data.audience),
      version_no: typeof data.version_no === 'number' && isFinite(data.version_no) ? data.version_no : 0,
      published_at: text(data.published_at),
      background: text(data.background),
      characters: (Array.isArray(data.characters) ? data.characters : []).filter(function (item) { return item && typeof item === 'object'; })
        .map(function (item) { return { name: text(item.name), occupation: text(item.occupation) }; })
        .filter(function (item) { return item.name !== '' || item.occupation !== ''; }),
      locations: strings(data.locations),
      has_custom_ending: data.has_custom_ending === true
    };
  }

  var app = Vue.createApp({
    setup: function () {
      var versionId = versionIdFromPath(window.location.pathname);
      var user = state.user && typeof state.user === 'object' ? state.user : {};
      var gate = Vue.ref(typeof state.gate === 'string' && state.gate ? state.gate : null);
      var restriction = Vue.ref(null);
      var phase = Vue.ref('loading');                 // loading｜ready｜notfound｜failed
      var detail = Vue.ref(null);
      var failureText = Vue.ref('');
      var contentMode = Vue.ref(null);                // 詳情回應的 `content_mode`：{ effective, forced }（玩家目前的安全模式與緊急煞車）
      var leaving = false;                            // 非響應式：已經導向登入頁
      var inflight = false;                           // 非響應式：詳情請求在途（重複呼叫不會再送一次）
      /* 與入口頁同一個詢問：狀態機與文案在 mode-prompt.js，Modal 樣板是 game/_mode_prompt.html。 */
      var prompt = Game.modePrompt.create(Vue, {
        mode: function () { return contentMode.value; },
        navigate: function (path) { window.location.assign(path); },
        startPath: Game.startPath,
        accountLink: Game.accountLink
      });

      var gateNotice = Vue.computed(function () { return Game.gateNotice(gate.value); });
      var canStart = Vue.computed(function () { return !gate.value && phase.value === 'ready' && detail.value !== null; });
      var memberText = Vue.computed(function () { return Game.memberLabel(user.member_tier); });
      var publishedText = Vue.computed(function () { return detail.value && detail.value.published_at ? Game.formatTaipeiTime(detail.value.published_at) : ''; });

      function applyFailure(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
        if (info.status === 401 && !leaving) {
          leaving = true;
          window.location.assign(Auth.loginUrlFor(window.location.pathname));
        }
      }

      /* 載入詳情（重試也是同一個函式）。載入中再呼叫不會再送一次請求；網址讀不出識別碼就直接當成找不到（不送請求）。 */
      function loadDetail() {
        if (inflight) { return Promise.resolve(false); }
        phase.value = 'loading';
        failureText.value = '';
        if (!versionId) { phase.value = 'notfound'; return Promise.resolve(false); }
        inflight = true;
        return Game.api.scriptDetail(versionId).then(function (data) {
          var body = data && typeof data === 'object' ? data.script : null;       // 回應是 { ok, script: { …詳情… } }
          var normalized = normalizeDetail(body);
          if (!normalized) {                          // 看不懂的成功回應：當成暫時失敗，可以重試
            failureText.value = LOAD_FAILED;
            phase.value = 'failed';
            return false;
          }
          detail.value = normalized;
          if (body.content_mode && typeof body.content_mode === 'object') { contentMode.value = body.content_mode; }
          phase.value = 'ready';
          return true;
        }, function (info) {
          applyFailure(info);
          if (info.status === 404) {
            phase.value = 'notfound';
          } else {
            failureText.value = info.message || LOAD_FAILED;
            phase.value = 'failed';
          }
          return false;
        }).finally(function () { inflight = false; });
      }

      /* 「開始冒險」：閘門非空或還沒載入完成時不導向（按鈕本身也是禁用的）；其餘交給共用的詢問狀態機——需要詢問（安全模式開、未綁定的劇本）時只開啟詢問 Modal、不導向。
         導向的識別碼取自伺服器回的詳情，不是自己拼的。回傳是否導向，供測試使用。 */
      function start() {
        if (!canStart.value) { return false; }
        return prompt.begin({ id: detail.value.id, content_tier: detail.value.content_tier });
      }

      Vue.onMounted(loadDetail);

      return {
        user: user, gate: gate, restriction: restriction, phase: phase, detail: detail, failureText: failureText, contentMode: contentMode,
        gateNotice: gateNotice, canStart: canStart, memberText: memberText, publishedText: publishedText,
        loadDetail: loadDetail, start: start, homePath: HOME_PATH,
        askCard: prompt.target, askOpen: prompt.open, modePrompt: prompt.copy, goToSettings: prompt.goToSettings, keepSafe: prompt.keepSafe, cancelAsk: prompt.cancel,
        tierLabel: Game.tierLabel, audienceLabel: Game.audienceLabel
      };
    }
  });
  UI.install(app);
  app.mount('#script-app');
})();
