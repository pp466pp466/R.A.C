/* 開存檔精靈 `/game/start/<版本識別碼>`（Vue 3 全域變數模式；見 rpg-creation-wizard 規格與 rpg-entry-ui 的 design D6）：序章、聯繫、創角三步驟。
 *
 * 規則：
 * - 步驟、提示、按鈕的禁用與三個 Modal 的顯示完全由 Vue 的響應式狀態接管（v-if／v-show），不使用 Bootstrap 的 JavaScript 與 data-bs-*；
 *   所有來自劇本作者與玩家的文字以 {{ }} 插值顯示（純文字），不使用 v-html。
 * - **玩家填寫的角色卡、自我介紹與聯繫只存在這個頁面的記憶體**：不寫任何瀏覽器儲存、不放網址、不輸出到主控台；重新整理就回到序章（刻意的）。
 * - 邏輯（前進條件、示範氣泡、請求本文、失敗對照）都在 wizard-state.js 的純函式；這裡只負責狀態、請求的時機，以及 finally 的恢復。
 *   合規與審查完全由伺服器判斷，這裡不重複實作規則。
 * - 兩個會呼叫模型的請求（生成預覽、開存檔）的逾時取自頁面初始狀態（由設定算出，這裡不寫死），各自有 AbortController，離開頁面（pagehide）時中止。
 *   無論成功、失敗或中斷，一律在 finally 恢復按鈕；任何失敗都保留玩家已填的內容。
 * - 開存檔成功（201）之後導向 `/game/<存檔識別碼>`，導向之前按鈕保持禁用，避免重複送出而建立兩個存檔。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);
  var HOME_PATH = '/game';
  var ANCHOR_KEYS = ['anchor', 'anchor_kind', 'anchor_ref', 'anchor_text'];
  var DRAFT_KEYS = Game.CARD_FIELDS.map(function (field) { return field.key; }).concat(['origin', 'intro']);

  function positive(value) { return typeof value === 'number' && isFinite(value) && value > 0 ? value : undefined; }

  /* 版本識別碼只在網址裡（頁面內容不含它）：`/game/start/<id>` 的最後一段。讀不出來回傳空字串。 */
  function versionIdFromPath(pathname) {
    var parts = String(pathname || '').split('/').filter(Boolean);
    var last = parts.length > 0 ? parts[parts.length - 1] : '';
    try { return decodeURIComponent(last); } catch (error) { return ''; }
  }

  function newController() { return typeof window.AbortController === 'function' ? new window.AbortController() : null; }

  var app = Vue.createApp({
    setup: function () {
      var versionId = versionIdFromPath(window.location.pathname);
      var gate = Vue.ref(typeof state.gate === 'string' && state.gate ? state.gate : null);
      var restriction = Vue.ref(null);
      var phase = Vue.ref('loading');
      var loadFailure = Vue.ref(null);
      var setupData = Vue.ref(null);
      var step = Vue.ref('prologue');
      var draft = Vue.reactive(Game.blankDraft());
      var anchor = Vue.reactive(Game.blankAnchor());
      var fieldErrors = Vue.reactive({});
      var notice = Vue.ref(null);
      var suggestMessage = Vue.ref('');
      var remaining = Vue.ref(0);
      var busy = Vue.ref(false);
      var suggesting = Vue.ref(false);
      var navigating = Vue.ref(false);
      var modal = Vue.reactive({ skip: false, overwrite: false, leave: false });
      var setupController = null;
      var suggestController = null;
      var startController = null;

      // ---- 計算 -------------------------------------------------------------------------------------------
      var limits = Vue.computed(function () { return setupData.value && typeof setupData.value.limits === 'object' && setupData.value.limits ? setupData.value.limits : {}; });
      var gateNotice = Vue.computed(function () { return Game.gateNotice(gate.value); });
      var stepIndex = Vue.computed(function () { return Game.stepIndex(step.value); });
      var counts = Vue.computed(function () { return Game.fieldCounts(draft, limits.value); });
      var anchorCounts = Vue.computed(function () { return Game.anchorCount(anchor, limits.value); });
      var targetGroups = Vue.computed(function () { return Game.targetGroups(setupData.value); });
      var bubbles = Vue.computed(function () { return Game.demoBubbles(draft); });
      var suggestionsRemaining = Vue.computed(function () { return remaining.value; });

      var originOptions = Vue.computed(function () {
        var given = setupData.value && Array.isArray(setupData.value.origin_options) ? setupData.value.origin_options : [];
        var known = Object.keys(Game.ORIGIN_LABELS);
        var options = given.filter(function (option) { return known.indexOf(option) >= 0; });
        return options.length > 0 ? options : known;
      });

      /* 聯繫選中的對象（取自 setup 的選項）；AI 預覽帶來的對象若不在選項裡就沒有（摘要退回顯示識別字）。 */
      var chosenTarget = Vue.computed(function () {
        if (!anchor.kind || !anchor.ref) { return null; }
        var groups = targetGroups.value;
        for (var i = 0; i < groups.length; i += 1) {
          if (groups[i].kind !== anchor.kind) { continue; }
          for (var j = 0; j < groups[i].options.length; j += 1) {
            if (groups[i].options[j].ref === anchor.ref) { return { kindLabel: Game.TARGET_KIND_LABELS[anchor.kind], label: groups[i].options[j].label }; }
          }
        }
        return null;
      });

      var anchorErrors = Vue.computed(function () {
        return ANCHOR_KEYS.map(function (key) { return fieldErrors[key]; }).filter(function (message) { return typeof message === 'string' && message; });
      });

      var anchorSummary = Vue.computed(function () {
        var current = Game.anchorState(anchor);
        if (current === 'skipped') { return '已略過（開場只依背景故事與您的角色卡展開）'; }
        if (current !== 'complete') { return '還沒有完成'; }
        var target = chosenTarget.value;
        var who = target ? target.kindLabel + '「' + target.label + '」' : '「' + anchor.ref + '」';
        return who + '：' + anchor.text.trim();
      });

      /* 角色卡或聯繫已超過長度上限（開始冒險因此禁用）；必要欄位沒填不在這裡：按下時會指出是哪一欄。 */
      var overLimit = Vue.computed(function () {
        return Game.blockers('create', draft, anchor, limits.value).some(function (item) { return item.code === 'over' || item.code === 'anchor_over'; });
      });
      var introOver = Vue.computed(function () { return counts.value.intro.over; });

      var canSuggest = Vue.computed(function () {
        return phase.value === 'ready' && !gate.value && !busy.value && !suggesting.value && !navigating.value && !introOver.value
          && remaining.value > 0 && !!setupData.value && setupData.value.suggestions_available !== false;
      });

      var startDisabled = Vue.computed(function () {
        return phase.value !== 'ready' || !!gate.value || busy.value || suggesting.value || navigating.value || overLimit.value;
      });

      /* 生成按鈕旁的說明：先看這次生成的結果，沒有時說明為什麼不能生成。 */
      var suggestNotice = Vue.computed(function () {
        if (suggestMessage.value) { return suggestMessage.value; }
        if (phase.value !== 'ready') { return ''; }
        if (setupData.value && setupData.value.suggestions_available === false) { return '目前暫時無法使用 AI 生成，請直接自己創角。'; }
        if (remaining.value <= 0) { return '今天的 AI 生成次數已經用完，可以改為自己創角，明天再試。'; }
        if (introOver.value) { return '自我介紹超過字數上限，請縮短後再生成。'; }
        return '';
      });

      // ---- 小工具 -----------------------------------------------------------------------------------------
      function clearErrors() { Object.keys(fieldErrors).forEach(function (key) { delete fieldErrors[key]; }); }
      function hasContent() { return Game.hasManualContent(draft, anchor) || draft.intro.trim() !== ''; }
      function goLogin() { window.location.assign(Auth.loginUrlFor(window.location.pathname)); }
      function jump(id) { if (Game.stepIndex(id) >= 0) { step.value = id; } }

      function noteRestriction(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
      }

      function showFailureNotice(failure) { notice.value = { text: failure.message, link: failure.link }; }

      /* 把伺服器的逐欄位說明放進表單（鍵見 Game.fieldKeyOf），並回到出錯的那一步（沒有對應步驟時不動）。 */
      function applyFieldErrors(failure) {
        clearErrors();
        Object.keys(failure.fields).forEach(function (key) { fieldErrors[key] = failure.fields[key]; });
        if (failure.step) { jump(failure.step); }
      }

      // 玩家修改了某個欄位，那個欄位上的舊錯誤就不再成立
      Vue.watch(function () { return DRAFT_KEYS.map(function (key) { return draft[key]; }); }, function (now, before) {
        DRAFT_KEYS.forEach(function (key, index) {
          if (now[index] === before[index]) { return; }
          delete fieldErrors[key];
          if (key !== 'intro') { delete fieldErrors.protagonist; }
        });
      }, { flush: 'sync' });
      Vue.watch(function () { return [anchor.kind, anchor.ref, anchor.text, anchor.skipped]; }, function () {
        ANCHOR_KEYS.forEach(function (key) { delete fieldErrors[key]; });
      }, { flush: 'sync' });

      // ---- 載入創角資料 -------------------------------------------------------------------------------------
      function loadSetup() {
        if (phase.value === 'loading' && setupController) { return Promise.resolve(false); }
        phase.value = 'loading';
        loadFailure.value = null;
        if (!versionId) {
          loadFailure.value = Game.describeSetupFailure({ kind: 'server', status: 404, code: 'not_found' });
          phase.value = 'failed';
          return Promise.resolve(false);
        }
        var controller = newController();
        setupController = controller || true;
        return Game.api.setup(versionId, { signal: controller ? controller.signal : undefined }).then(function (data) {
          if (!data || typeof data !== 'object' || typeof data.title !== 'string' || typeof data.background !== 'string') {
            loadFailure.value = Game.describeSetupFailure({ kind: 'server', status: 500, code: '' });        // 看不懂的成功回應：當成暫時失敗，可以重試
            phase.value = 'failed';
            return false;
          }
          setupData.value = data;
          remaining.value = typeof data.suggestions_remaining === 'number' && data.suggestions_remaining > 0 ? Math.floor(data.suggestions_remaining) : 0;
          step.value = 'prologue';
          phase.value = 'ready';
          return true;
        }, function (info) {
          noteRestriction(info);
          if (info.status === 401) { goLogin(); }
          // 被中止（離開頁面）也不留在「載入中」：頁面若還在就提供重試
          loadFailure.value = info.kind === 'canceled' ? { message: '載入已中止，請重試。', link: null, retry: true } : Game.describeSetupFailure(info);
          phase.value = 'failed';
          return false;
        }).finally(function () { setupController = null; });
      }

      // ---- 步驟 -------------------------------------------------------------------------------------------
      /* 把擋住的原因放到對應的欄位並回到最前面的那一步；回傳 true 代表有被擋住。 */
      function showBlockers(blocked) {
        clearErrors();
        var target = 'create';
        blocked.forEach(function (item) {
          if (!Object.prototype.hasOwnProperty.call(fieldErrors, item.field)) { fieldErrors[item.field] = item.message; }
          if (Game.stepIndex(item.step) < Game.stepIndex(target)) { target = item.step; }
        });
        jump(target);
        notice.value = { text: target === 'contact' ? '請先完成「聯繫」。' : '請先補齊標示的欄位。', link: null };
        return true;
      }

      function next() {
        if (busy.value || suggesting.value || navigating.value) { return false; }
        notice.value = null;
        if (step.value === 'prologue') { clearErrors(); step.value = 'contact'; return true; }
        if (step.value === 'contact') {
          var blocked = Game.blockers('contact', draft, anchor, limits.value);
          if (blocked.length > 0) { showBlockers(blocked); return false; }
          clearErrors();
          step.value = 'create';
          return true;
        }
        return false;
      }

      function back() {
        if (busy.value || suggesting.value || navigating.value) { return false; }
        var index = Game.stepIndex(step.value);
        if (index <= 0) { return false; }
        notice.value = null;
        step.value = Game.STEPS[index - 1].id;
        return true;
      }

      /* 只能回到目前或較前面的步驟（往後要經過每一步的前進條件）。 */
      function goTo(id) {
        if (busy.value || suggesting.value || navigating.value) { return false; }
        var target = Game.stepIndex(id);
        if (target < 0 || target > Game.stepIndex(step.value)) { return false; }
        notice.value = null;
        step.value = id;
        return true;
      }

      // ---- 聯繫 -------------------------------------------------------------------------------------------
      function chooseTarget(kind, ref) {
        if (busy.value || anchor.skipped) { return false; }
        anchor.kind = kind;
        anchor.ref = ref;
        return true;
      }
      function isChosen(kind, ref) { return !anchor.skipped && anchor.kind === kind && anchor.ref === ref; }
      function askSkip() { if (!busy.value && !anchor.skipped) { modal.skip = true; } }
      function confirmSkip() {
        modal.skip = false;
        if (busy.value) { return false; }
        Object.assign(anchor, Game.skippedAnchor());
        clearErrors();
        notice.value = null;
        return true;
      }
      function unskip() {
        if (busy.value) { return false; }
        Object.assign(anchor, Game.blankAnchor());
        return true;
      }

      // ---- AI 生成預覽 --------------------------------------------------------------------------------------
      function runSuggestion() {
        if (!canSuggest.value) { return Promise.resolve(false); }
        suggesting.value = true;
        suggestMessage.value = '';
        notice.value = null;
        delete fieldErrors.intro;
        var controller = newController();
        suggestController = controller || true;
        return Game.api.suggest(Game.buildSuggestPayload(versionId, draft), {
          timeout: positive(state.suggest_timeout_ms), signal: controller ? controller.signal : undefined
        }).then(function (data) {
          if (!data || typeof data !== 'object' || !data.protagonist || typeof data.protagonist !== 'object') {
            suggestMessage.value = Game.describeSuggestFailure({ kind: 'server', status: 500, code: '' }).message;     // 看不懂的成功回應：什麼都不填
            return false;
          }
          var applied = Game.applySuggestion(draft, anchor, data);
          clearErrors();
          Object.assign(draft, applied.draft);
          Object.assign(anchor, applied.anchor);
          if (typeof data.remaining === 'number') { remaining.value = Math.max(0, Math.floor(data.remaining)); }
          else { remaining.value = Math.max(0, remaining.value - 1); }
          UI.toast('已填入 AI 生成的預覽，可以修改後再開始冒險，也可以重新生成。');
          return true;
        }, function (info) {
          noteRestriction(info);
          var failure = Game.describeSuggestFailure(info);
          if (failure.kind === 'canceled') { return false; }
          if (failure.kind === 'login') { goLogin(); return false; }
          if (failure.code === 'suggestion_limit') { remaining.value = 0; }
          if (failure.kind === 'fields') { applyFieldErrors(failure); suggestMessage.value = failure.message; }
          else if (failure.kind === 'gate' || failure.kind === 'csrf' || failure.kind === 'not_found') { showFailureNotice(failure); }
          else { suggestMessage.value = failure.message; }
          return false;
        }).finally(function () { suggesting.value = false; suggestController = null; });
      }

      /* 已有手動填寫的內容時先以 Modal 確認會覆蓋；沒有就直接生成。 */
      function requestSuggestion() {
        if (!canSuggest.value) { return Promise.resolve(false); }
        if (Game.hasManualContent(draft, anchor)) { modal.overwrite = true; return Promise.resolve(false); }
        return runSuggestion();
      }
      function confirmSuggestion() { modal.overwrite = false; return runSuggestion(); }

      // ---- 開始冒險 -----------------------------------------------------------------------------------------
      function handleStartFailure(info) {
        noteRestriction(info);
        var failure = Game.describeStartFailure(info);
        if (failure.kind === 'canceled') { return; }
        if (failure.kind === 'login') { goLogin(); return; }
        if (failure.kind === 'fields') { applyFieldErrors(failure); }
        showFailureNotice(failure);
      }

      function start() {
        if (startDisabled.value) { return Promise.resolve(false); }
        notice.value = null;
        var blocked = Game.blockers('create', draft, anchor, limits.value);
        if (blocked.length > 0) { showBlockers(blocked); return Promise.resolve(false); }
        clearErrors();
        busy.value = true;
        var controller = newController();
        startController = controller || true;
        return Game.api.start(Game.buildStartPayload(versionId, draft, anchor), {
          timeout: positive(state.start_timeout_ms), signal: controller ? controller.signal : undefined
        }).then(function (data) {
          var saved = data && typeof data === 'object' && data.session && typeof data.session === 'object' ? data.session.id : null;
          if (typeof saved !== 'string' || !saved) {
            // 伺服器說成功卻沒給存檔：存檔可能已建立，不要讓玩家再送一次
            notice.value = { text: '存檔可能已經建立，但沒有收到完整的回應。請到「我的存檔」確認。', link: { href: HOME_PATH, text: '回到冒險' } };
            return false;
          }
          navigating.value = true;
          window.location.assign(Game.sessionPath(saved));
          return true;
        }, function (info) { handleStartFailure(info); return false; }).finally(function () { busy.value = false; startController = null; });
      }

      // ---- 離開 -------------------------------------------------------------------------------------------
      /* 頂列的「回到冒險」：已經填了東西時先問；什麼都沒填就照一般連結離開。 */
      function leaveClick(event) {
        if (navigating.value || !hasContent()) { return false; }
        if (event && typeof event.preventDefault === 'function') { event.preventDefault(); }
        modal.leave = true;
        return true;
      }
      function confirmLeave() {
        modal.leave = false;
        window.location.assign(HOME_PATH);
        return true;
      }

      window.addEventListener('pagehide', function () {
        [setupController, suggestController, startController].forEach(function (controller) {
          if (controller && typeof controller.abort === 'function') { controller.abort(); }
        });
      });
      Vue.onMounted(loadSetup);

      return {
        phase: phase, loadFailure: loadFailure, restriction: restriction, gateNotice: gateNotice, setup: setupData, steps: Game.STEPS, step: step,
        stepIndex: stepIndex, draft: draft, anchor: anchor, counts: counts, anchorCounts: anchorCounts, fieldErrors: fieldErrors, notice: notice,
        busy: busy, suggesting: suggesting, modal: modal,
        cardFields: Game.CARD_FIELDS, originOptions: originOptions, targetGroups: targetGroups, chosenTarget: chosenTarget, anchorErrors: anchorErrors,
        anchorSummary: anchorSummary, bubbles: bubbles, suggestionsRemaining: suggestionsRemaining, suggestNotice: suggestNotice,
        canSuggest: canSuggest, startDisabled: startDisabled,
        loadSetup: loadSetup, next: next, back: back, goTo: goTo, chooseTarget: chooseTarget, isChosen: isChosen, askSkip: askSkip,
        confirmSkip: confirmSkip, unskip: unskip, requestSuggestion: requestSuggestion, confirmSuggestion: confirmSuggestion, start: start,
        leaveClick: leaveClick, confirmLeave: confirmLeave, originLabel: Game.originLabel
      };
    }
  });
  UI.install(app);
  app.mount('#wizard-app');
})();
