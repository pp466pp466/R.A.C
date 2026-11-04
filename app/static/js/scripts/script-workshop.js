/* 劇本創作頁（Vue 3 全域變數模式；見 script-authoring 與 appeal-shortcut 規格）。
 *
 * 規則：Modal、Toast 與區塊的顯示與隱藏完全由 Vue 的響應式狀態接管（v-if／v-show），不使用 Bootstrap 的 JavaScript；
 * 所有使用者文字以 {{ }} 插值顯示（純文字），不使用 v-html。伺服器是唯一的驗證者：這裡只顯示字數與伺服器回傳的逐欄位錯誤。
 * 無論成功、失敗或中斷，一定在 finally 恢復按鈕；送審（會呼叫審查的模型層，最長約 30 秒）用 AbortController 於離開頁面時中止。
 * 草稿以內容雜湊偵測衝突：儲存帶開始編輯時的雜湊，別處已更新就不覆寫（`draft_changed`），由作者決定重新載入。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Scripts = window.Scripts;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);

  var app = Vue.createApp({
    setup: function () {
      var user = state.user || {};
      var spec = Vue.ref({});
      var limits = Vue.ref(null);
      var scripts = Vue.ref([]);
      var ready = Vue.ref(false);
      var listError = Vue.ref('');
      var view = Vue.ref('list');
      var restriction = Vue.ref(UI.restrictionOf(user));
      var gate = Vue.ref(state.gate || null);

      var editor = Vue.reactive({
        version: null, draft: Scripts.blankDraft(), baseline: '', saving: false, submitting: false, working: false, errors: {}, general: ''
      });
      var open = Vue.reactive({ catalog: true, background: true, characters: true, locations: false, milestones: true, ending: false });
      var modal = Vue.reactive({ submit: false, conflict: false, leave: false, example: false, remove: false, removeTarget: null });
      var controller = null;
      var pendingLeave = null;

      var dirty = Vue.computed(function () { return Scripts.snapshot(editor.draft) !== editor.baseline; });
      var readOnly = Vue.computed(function () { return !!editor.version && editor.version.status !== 'draft'; });
      var locked = Vue.computed(function () { return restriction.value === 'locked'; });
      var suspended = Vue.computed(function () { return restriction.value === 'suspended'; });
      var busy = Vue.computed(function () { return editor.saving || editor.submitting || editor.working; });
      var canEdit = Vue.computed(function () { return !readOnly.value && !locked.value && !busy.value && !gate.value; });
      var canSubmit = Vue.computed(function () { return canEdit.value && !suspended.value; });
      var errorList = Vue.computed(function () { return Scripts.errorList(editor.errors); });
      var preview = Vue.computed(function () {
        return Scripts.previewMilestones(editor.draft.milestones, { start_year: editor.draft.calendar_start_year });
      });
      var tiers = Vue.computed(function () { return Scripts.allowedTiers(limits.value ? limits.value.max_content_tier : 'safe'); });
      var total = Vue.computed(function () { return Scripts.totalChars(editor.draft); });
      var scriptsLeft = Vue.computed(function () { return limits.value ? limits.value.scripts.limit - limits.value.scripts.used : 0; });
      var submitsLeft = Vue.computed(function () { return limits.value ? limits.value.submissions_today.limit - limits.value.submissions_today.used : 0; });
      var penName = Vue.computed(function () {
        var script = editor.version && editor.version.script;
        return script && script.author_name ? script.author_name + '#' + script.author_code : '';
      });

      // ---- 錯誤的集中處理 ------------------------------------------------------------------------
      function err(path) { return editor.errors[path] || ''; }

      function applyFailure(info) {
        var kind = UI.restrictionOf(info);
        if (kind) { restriction.value = kind; }
        if (info.status === 401) { window.location.assign(Auth.loginUrlFor('/scripts')); return; }
        if (info.code === 'age_confirmation_required') { gate.value = 'age_confirmation_required'; return; }
        if (info.code === 'draft_changed') { modal.conflict = true; return; }
        if (info.code === 'script_auto_rejected') { editor.general = Scripts.rejectionMessage(info.categories); return; }
        if (info.kind === 'server' && info.status === 400 && Object.keys(info.fields || {}).length) {
          editor.errors = info.fields;
          editor.general = '請修正標示的欄位。';
          return;
        }
        if (kind) { editor.general = ''; return; }
        editor.general = info.message || '發生錯誤，請稍後再試。';
      }

      // ---- 載入 ---------------------------------------------------------------------------------
      function refreshList() {
        return Scripts.api.list().then(function (data) { scripts.value = data.scripts; listError.value = ''; }, function (info) {
          applyFailure(info);
          listError.value = info.message || '無法載入劇本清單。';
        });
      }

      function refreshLimits() {
        return Scripts.api.limits().then(function (data) {
          limits.value = data.limits;
          spec.value = data.limits.spec || {};
          if (data.limits.publish_suspended && !restriction.value) { restriction.value = 'suspended'; }
        }, applyFailure);
      }

      function load() {
        if (gate.value) { ready.value = true; return Promise.resolve(); }
        return Promise.all([refreshLimits(), refreshList()]).finally(function () { ready.value = true; });
      }

      // ---- 編輯器的狀態 ----------------------------------------------------------------------------
      function clearFeedback() { editor.errors = {}; editor.general = ''; }

      function showVersion(version) {
        editor.version = version;
        editor.draft = Scripts.toDraft(version.content);
        editor.baseline = Scripts.snapshot(editor.draft);
        clearFeedback();
        view.value = 'edit';
      }

      function startNew() {
        if (limits.value && scriptsLeft.value <= 0) {
          UI.toast('劇本數量已達上限，請先刪除不需要的草稿。', { type: 'error' });
          return;
        }
        if (limits.value && !limits.value.has_nickname) {
          UI.toast('創作劇本前，請先到帳號設定設定暱稱。', { type: 'error' });
          return;
        }
        editor.version = null;
        editor.draft = Scripts.blankDraft();
        editor.baseline = Scripts.snapshot(editor.draft);
        clearFeedback();
        view.value = 'edit';
      }

      function openVersion(versionId) {
        editor.working = true;
        return Scripts.api.get(versionId).then(function (data) { showVersion(data.version); }, applyFailure)
          .finally(function () { editor.working = false; });
      }

      function leave(next) {
        view.value = 'list';
        editor.version = null;
        clearFeedback();
        if (typeof next === 'function') { next(); }
      }

      function backToList() {
        if (dirty.value && !readOnly.value) { pendingLeave = leave; modal.leave = true; return; }
        leave();
        refreshList();
      }

      function confirmLeave() { var next = pendingLeave; pendingLeave = null; modal.leave = false; if (next) { next(); refreshList(); } }

      function reloadFromServer() {
        modal.conflict = false;
        if (!editor.version) { return Promise.resolve(); }
        return openVersion(editor.version.id);
      }

      // ---- 範例 -------------------------------------------------------------------------------------
      function askExample() {
        if (!canEdit.value) { return; }
        if (dirty.value) { modal.example = true; return; }
        loadExample();
      }

      function loadExample() {
        modal.example = false;
        editor.working = true;
        return Scripts.api.example().then(function (data) {
          var draft = Scripts.toDraft(data.content);
          if (tiers.value.indexOf(draft.content_tier) < 0) { draft.content_tier = tiers.value[tiers.value.length - 1]; }
          editor.draft = draft;
          clearFeedback();
          UI.toast('已帶入範例，可以自由修改。');
        }, applyFailure).finally(function () { editor.working = false; });
      }

      // ---- 儲存與送審 -----------------------------------------------------------------------------------
      function save() {
        if (!canEdit.value) { return Promise.resolve(false); }
        clearFeedback();
        editor.saving = true;
        var content = Scripts.toContent(editor.draft);
        var sent = JSON.stringify(content);
        var request = editor.version
          ? Scripts.api.save(editor.version.id, content, editor.version.content_hash)
          : Scripts.api.create(content);
        return request.then(function (data) {
          editor.version = data.version;
          editor.baseline = sent;                       // 輸入在儲存期間是停用的，所以送出的就是目前的內容
          UI.toast('草稿已儲存。');
          refreshList();
          refreshLimits();
          return true;
        }, function (info) { applyFailure(info); return false; }).finally(function () { editor.saving = false; });
      }

      function askSubmit() { if (canSubmit.value) { modal.submit = true; } }

      function confirmSubmit() {
        var saved = dirty.value || !editor.version ? save() : Promise.resolve(true);
        return saved.then(function (ok) {
          if (!ok || !editor.version) { return false; }
          editor.submitting = true;
          clearFeedback();
          controller = typeof window.AbortController === 'function' ? new window.AbortController() : null;
          return Scripts.api.submit(editor.version.id, {
            timeout: state.submit_timeout_ms, signal: controller ? controller.signal : undefined
          }).then(function (data) {
            editor.version = data.version;
            editor.baseline = Scripts.snapshot(Scripts.toDraft(data.version.content));
            editor.draft = Scripts.toDraft(data.version.content);
            UI.toast('已送審，等待管理員審核。審核期間內容會被凍結，需要修改可以先撤回。');
            refreshList();
            refreshLimits();
            return true;
          }, function (info) {
            if (info.kind === 'canceled') { return false; }
            applyFailure(info);
            refreshLimits();
            return false;
          }).finally(function () { editor.submitting = false; controller = null; });
        });
      }

      // ---- 列表上的操作 -----------------------------------------------------------------------------------
      function withdraw(versionId) {
        editor.working = true;
        return Scripts.api.withdraw(versionId).then(function (data) {
          UI.toast('已撤回，回到草稿。');
          if (editor.version && editor.version.id === versionId) { showVersion(data.version); }
          return refreshList();
        }, applyFailure).finally(function () { editor.working = false; });
      }

      function askRemove(version) { modal.removeTarget = version; modal.remove = true; }

      function confirmRemove() {
        var target = modal.removeTarget;
        modal.remove = false;
        if (!target) { return Promise.resolve(); }
        editor.working = true;
        return Scripts.api.remove(target.id).then(function () {
          UI.toast('草稿已刪除。');
          if (editor.version && editor.version.id === target.id) { leave(); }
          return Promise.all([refreshList(), refreshLimits()]);
        }, applyFailure).finally(function () { editor.working = false; modal.removeTarget = null; });
      }

      function newVersion(scriptId) {
        editor.working = true;
        return Scripts.api.newVersion(scriptId).then(function (data) { showVersion(data.version); refreshList(); }, applyFailure)
          .finally(function () { editor.working = false; });
      }

      function refreshAuthorName(scriptId) {
        editor.working = true;
        return Scripts.api.refreshAuthorName(scriptId).then(function () {
          UI.toast('已改用目前的暱稱作為筆名。');
          if (editor.version && editor.version.script && editor.version.script.id === scriptId) { return openVersion(editor.version.id); }
          return refreshList();
        }, applyFailure).finally(function () { editor.working = false; });
      }

      // ---- 清單類欄位 ----------------------------------------------------------------------------------------
      function addCharacter() { editor.draft.characters.push(Scripts.toDraft({ characters: [{}] }).characters[0]); }
      function removeCharacter(index) { editor.draft.characters.splice(index, 1); }
      function addLocation() { editor.draft.locations.push(Scripts.toDraft({ locations: [''] }).locations[0]); }
      function removeLocation(index) { editor.draft.locations.splice(index, 1); }
      function addMilestone() { editor.draft.milestones.push(Scripts.toDraft({ milestones: [{}] }).milestones[0]); }
      function removeMilestone(index) { editor.draft.milestones.splice(index, 1); }
      function toggle(name) { open[name] = !open[name]; }
      // 劇本已有進行中的版本（草稿或待審）時不能再建立新草稿
      function hasOpenVersion(script) {
        return script.versions.some(function (version) { return version.status === 'draft' || version.status === 'pending_review'; });
      }

      // ---- 生命週期 ---------------------------------------------------------------------------------------------
      window.addEventListener('beforeunload', function (event) {
        if (view.value === 'edit' && dirty.value && !readOnly.value) { event.preventDefault(); event.returnValue = ''; }
      });
      window.addEventListener('pagehide', function () { if (controller) { controller.abort(); } });
      Vue.onMounted(load);

      return {
        user: user, spec: spec, limits: limits, scripts: scripts, ready: ready, listError: listError, view: view, gate: gate,
        restriction: restriction, editor: editor, open: open, modal: modal,
        dirty: dirty, readOnly: readOnly, locked: locked, suspended: suspended, busy: busy, canEdit: canEdit, canSubmit: canSubmit,
        errorList: errorList, preview: preview, tiers: tiers, total: total, scriptsLeft: scriptsLeft, submitsLeft: submitsLeft, penName: penName,
        tierLabels: Scripts.TIER_LABELS, statusLabels: Scripts.STATUS_LABELS, count: Scripts.charCount, err: err,
        startNew: startNew, openVersion: openVersion, backToList: backToList, confirmLeave: confirmLeave, reloadFromServer: reloadFromServer,
        askExample: askExample, loadExample: loadExample, save: save, askSubmit: askSubmit, confirmSubmit: confirmSubmit,
        withdraw: withdraw, askRemove: askRemove, confirmRemove: confirmRemove, newVersion: newVersion, refreshAuthorName: refreshAuthorName,
        addCharacter: addCharacter, removeCharacter: removeCharacter, addLocation: addLocation, removeLocation: removeLocation,
        addMilestone: addMilestone, removeMilestone: removeMilestone, toggle: toggle, hasOpenVersion: hasOpenVersion
      };
    }
  });
  UI.install(app);
  app.mount('#script-app');
})();
