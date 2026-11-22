/* 劇本詳情頁 `/game/script/<版本識別碼>`（Vue 3 全域變數模式；見 rpg-script-catalog 規格「詳情頁」與 rpg-entry-pages 規格「點選未綁定劇本時詢問是否修改安全模式」）。
 *
 * 規則：顯示與按鈕的禁用完全由 Vue 的響應式狀態接管（v-if／v-show），不使用 Bootstrap 的 JavaScript 與互動屬性；所有來自作者的文字（標題、簡介、世界名稱、標籤、筆名、
 * 背景、角色與地點）以 {{ }} 插值顯示（純文字），不使用 v-html。頁面外殼沒有劇本內容，也不含版本識別碼：識別碼只在網址裡，這裡自己讀取，
 * 資料來自唯讀的詳情端點（`Game.api.scriptDetail`；只有公開的白名單欄位，沒有隱藏特質、里程碑、結局說明、規則與初始對話）。本頁唯一的寫入是檢舉（`Game.api.reportScript`）：
 * 不碰遊戲幣、不呼叫模型、不寫任何瀏覽器儲存。
 * 階段（`phase`）：loading → ready｜notfound（404：下架、他人的草稿與不存在都是同一個 404，顯示「找不到這個劇本」與返回目錄）｜failed（其他失敗，附重試）。
 * 無論成功或失敗，一律在 finally 恢復載入狀態；401 導向登入並保留位置；被鎖定等受限狀態顯示對應的提示。
 * 檢舉（rpg-script-reports；script-reports 規格「劇本詳情頁提供檢舉入口」）：入口依詳情回應的 `report_state`——`available` 顯示「檢舉」、`reported` 顯示停用的「已檢舉」、
 * `unavailable`（內建、自己的劇本、不認得的值）不顯示。按下「檢舉」開自製 Modal（`REPORT_SCRIPT`；理由與文字的驗證、請求本文與失敗文字在 script-report.js，Esc 與遮罩視同取消，
 * 送出中不可關閉）。成功與 409 `already_reported` 都把入口改成已檢舉（成功另有 Toast）；404 與 403 `report_own_script` 改成不顯示；429 與其他失敗留在 Modal 內說明、可以再試；
 * 無論成功或失敗，一律在 finally 恢復 `submitting`；自寫文字只存在於表單狀態與請求本文，不寫任何瀏覽器儲存。
 * 「開始冒險」導向 `/game/start/<id>`；帳號閘門非空時禁用（後端的閘門仍是唯一的把關者）。安全模式開的玩家對**未綁定**安全模式的劇本按下時，
 * 先以與入口頁同一個自製 Modal 詢問是否修改安全模式（狀態機與文案在 mode-prompt.js，Modal 樣板是 game/_mode_prompt.html；Esc 與遮罩視同取消，留在本頁）。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var Report = Game.scriptReport;
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
      has_custom_ending: data.has_custom_ending === true,
      report_state: Report.stateOf(data.report_state)         // 只接受三個列舉，其他當 unavailable（不顯示入口）
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
      var reportState = Vue.ref('unavailable');       // 檢舉入口：available｜reported｜unavailable（詳情載入後依回應設定，送出後依結果改變）
      var reportShown = Vue.ref(false);               // 檢舉 Modal 是否開啟（由 `reportOpen` 包裝，送出中拒絕關閉）
      var reportPicked = Vue.ref([]);                 // 已勾選的理由（值）
      var reportText = Vue.ref('');                   // 選填的自寫文字（只存在於這個表單與請求本文）
      var reportError = Vue.ref('');                  // Modal 內的失敗說明
      var submitting = Vue.ref(false);                // 檢舉送出中
      var leaving = false;                            // 非響應式：已經導向登入頁
      var inflight = false;                           // 非響應式：詳情請求在途（重複呼叫不會再送一次）
      /* 與入口頁同一個詢問：狀態機與文案在 mode-prompt.js，Modal 樣板是 game/_mode_prompt.html。 */
      var prompt = Game.modePrompt.create(Vue, {
        mode: function () { return contentMode.value; },
        navigate: function (path) { window.location.assign(path); },
        startPath: Game.startPath,
        accountLink: Game.accountLink
      });

      var reportCopy = Report.copy();
      var reportReasons = Report.reasons();
      var reportCheck = Vue.computed(function () { return Report.validate(reportPicked.value, reportText.value); });
      var canSubmitReport = Vue.computed(function () { return reportState.value === 'available' && reportCheck.value.ok && !submitting.value; });
      /* 給 `<ui-modal v-model>` 的可寫計算屬性：Esc 與點遮罩把它設為 false；送出中不可關閉（結果出來之前不讓表單消失）。 */
      var reportOpen = Vue.computed({
        get: function () { return reportShown.value; },
        set: function (value) { if (value || !submitting.value) { reportShown.value = value === true; } }
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
        return kind;
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
          reportState.value = normalized.report_state;
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

      /* 開啟檢舉 Modal：只有 `available` 才開（其他狀態沒有入口）；每次開啟都是乾淨的表單。回傳是否開啟。 */
      function openReport() {
        if (reportState.value !== 'available' || submitting.value) { return false; }
        reportPicked.value = [];
        reportText.value = '';
        reportError.value = '';
        reportShown.value = true;
        return true;
      }

      /* 關閉檢舉 Modal（取消鈕）：送出中不可關閉。表單內容隨 Modal 關閉而清除（下次開啟時重設）。回傳是否關閉。 */
      function closeReport() {
        if (submitting.value) { return false; }
        reportShown.value = false;
        return true;
      }

      /* 送出檢舉：沒有通過驗證（沒選理由或文字太長）不送請求；送出中再按不會再送一次。識別碼取自伺服器回的詳情，不是自己拼的。
         成功與 409 `already_reported` 都改成已檢舉並關閉 Modal（成功另有 Toast）；404 與 403 `report_own_script` 改成不顯示入口；401 導向登入；
         被鎖定等受限狀態關閉 Modal 並顯示申訴捷徑；其餘（400、429、網路）留在 Modal 內說明，玩家可以再試。`finally` 一律恢復 `submitting`。 */
      function submitReport() {
        if (submitting.value || reportState.value !== 'available' || detail.value === null) { return Promise.resolve(false); }
        var check = reportCheck.value;
        if (!check.ok) {
          reportError.value = check.errors.reasons || check.errors.text || '';
          return Promise.resolve(false);
        }
        submitting.value = true;
        reportError.value = '';
        return Game.api.reportScript(detail.value.id, Report.requestBody(reportPicked.value, reportText.value)).then(function () {
          reportState.value = Report.stateAfter(null);
          reportShown.value = false;
          UI.toast(reportCopy.success);
          return true;
        }, function (info) {
          var restricted = applyFailure(info);
          var next = Report.stateAfter(info);
          if (next) { reportState.value = next; }
          if (restricted || next === 'reported') {
            reportShown.value = false;
            if (next === 'reported') { UI.toast(Report.failureText(info)); }
          } else if (info.status !== 401) {
            reportError.value = Report.failureText(info);
          }
          return false;
        }).finally(function () { submitting.value = false; });
      }

      Vue.onMounted(loadDetail);

      return {
        user: user, gate: gate, restriction: restriction, phase: phase, detail: detail, failureText: failureText, contentMode: contentMode,
        gateNotice: gateNotice, canStart: canStart, memberText: memberText, publishedText: publishedText,
        loadDetail: loadDetail, start: start, homePath: HOME_PATH,
        reportState: reportState, reportOpen: reportOpen, reportPicked: reportPicked, reportText: reportText, reportError: reportError, submitting: submitting,
        reportCopy: reportCopy, reportReasons: reportReasons, reportCheck: reportCheck, canSubmitReport: canSubmitReport, textMax: Report.TEXT_MAX,
        openReport: openReport, closeReport: closeReport, submitReport: submitReport,
        askCard: prompt.target, askOpen: prompt.open, modePrompt: prompt.copy, goToSettings: prompt.goToSettings, keepSafe: prompt.keepSafe, cancelAsk: prompt.cancel,
        tierLabel: Game.tierLabel, audienceLabel: Game.audienceLabel
      };
    }
  });
  UI.install(app);
  app.mount('#script-app');
})();
