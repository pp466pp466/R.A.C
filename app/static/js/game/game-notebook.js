/* 遊戲頁的記事簿面板（檢視全文與用量、已滿提示、玩家編輯與儲存、版本衝突、錯誤對照、已完結唯讀；見 rpg-notebook-panel 規格與 rpg-game-ui 的 design D9、D11）。
 *
 * `Game.createNotebook(deps)` 回傳一組響應式狀態與方法，由 game.js 注入依賴並接到樣板（這個檔案本身不碰 `window`、`document`、儲存與主控台，Node 可以用真的 Vue 與替身 API 測試）：
 *   deps.Vue         `{ ref, computed }`（Vue 3 全域）
 *   deps.api         `{ notebook(id, options), saveNotebook(id, text, expectedVersion, options) }`（game-api.js；`GET`／`PUT /api/game/sessions/<id>/notebook`）
 *   deps.sessionId   存檔識別碼
 *   deps.summary     響應式變數：快照裡的記事簿摘要 `{ version, length, capacity, full }`｜null（`session.notebookSummary`；沒有全文）
 *   deps.ended       響應式變數：存檔是否已完結（`session.ended`）；已完結時只能檢視
 *   deps.onEnded     選用；儲存時伺服器回 409 `session_ended`，通知畫面「存檔已完結」
 *   deps.toast、deps.goLogin、deps.noteFailure（選用，每個失敗的原始 info 都先交給它，game.js 用來辨識「被鎖定」之類的受限狀態）、deps.AbortController（選用）
 *
 * 規則：
 * - **全文只在玩家開啟面板時才讀**（`open()`；不在載入頁面時請求），只存在記憶體：不進初始狀態、不進 IndexedDB、不進主控台，也不送往記事簿端點以外的任何請求。
 * - **已滿**＝長度大於容量（剛好用完容量、只是用到預備空間都不算）。提示同時出現在面板與遊戲頁的入口（`full`，由面板讀到的檢視與快照的摘要取版本較新者判斷）。
 * - **編輯**：進入編輯時記下版本；字數即時顯示（NFC 之後以 Unicode 字元計，與後端同一個算法），超過容量時禁用儲存並說明超出多少（玩家編輯沒有預備空間）；
 *   儲存只有玩家按「儲存」才會送出（**從不自動儲存**），進行中再按只會有一個請求，`finally` 恢復。有未儲存的變更時按「取消」會先問（`discardOpen`）。
 * - **版本衝突**（409 `notebook_conflict`）：以後端附上的最新記事簿更新全文、用量與版本，**玩家的草稿原封不動保留**（`conflict` 另外顯示後端最新的內容供對照），
 *   不自動合併；之後的儲存以最新的版本為準，所以是玩家看過之後明確地再按一次。
 * - **其他失敗**以固定的繁體中文顯示（`Game.describeNotebookFailure`）：太長、規則層命中（只有類別）、已完結（轉唯讀）、找不到存檔、登入過期、網路與逾時；訊息不含記事簿內容；
 *   所有失敗都保留草稿。
 * - 已完結的存檔沒有「編輯」；編輯到一半才完結時，草稿留在唯讀的輸入框裡供複製。
 */
(function (root, factory) {
  'use strict';
  var Game;
  if (typeof module === 'object' && module.exports) {
    Game = Object.assign({}, require('./game-labels.js'), require('./game-state.js'));
  } else {
    Game = root.Game;
  }
  var api = factory(Game);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.createNotebook = api.createNotebook;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var DASH = '—';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function noop() {}
  function assign(target, source) {
    if (source) { Object.keys(source).forEach(function (key) { target[key] = source[key]; }); }
    return target;
  }

  function createNotebook(deps) {
    var ref = deps.Vue.ref;
    var computed = deps.Vue.computed;
    var api = deps.api;
    var sessionId = typeof deps.sessionId === 'string' ? deps.sessionId : '';
    var summary = isObject(deps.summary) && 'value' in deps.summary ? deps.summary : ref(null);
    var ended = isObject(deps.ended) && 'value' in deps.ended ? deps.ended : ref(false);
    var onEnded = typeof deps.onEnded === 'function' ? deps.onEnded : noop;
    var toast = typeof deps.toast === 'function' ? deps.toast : noop;
    var goLogin = typeof deps.goLogin === 'function' ? deps.goLogin : noop;
    var noteFailure = typeof deps.noteFailure === 'function' ? deps.noteFailure : noop;

    var view = ref(null);                  // Game.notebookView 的形狀（有全文）｜null：還沒讀過
    var loading = ref(false);
    var loadError = ref(null);             // { message, link, retry }｜null
    var editing = ref(false);
    var draft = ref('');                   // 玩家正在編輯的內容（只在記憶體；離開編輯就清掉）
    var editBase = ref('');                // 進入編輯時的內容：和草稿不同才算「有未儲存的變更」
    var saving = ref(false);
    var notice = ref(null);                // { tone: 'error'｜'info', text }｜null（編輯中的儲存結果與失敗）
    var conflict = ref(null);              // 衝突時後端最新的記事簿（供對照）｜null
    var discardOpen = ref(false);

    var baseVersion = 0;                   // 非響應式：下一次儲存要帶的 expected_version
    var loadingPromise = null;
    var loadController = null;
    var saveController = null;

    // ---- 計算 ---------------------------------------------------------------------------------------
    var current = computed(function () { return Game.notebookCurrent(view.value, summary.value); });
    var full = computed(function () { return Game.notebookIsFull(current.value); });
    var usage = computed(function () { return current.value ? Game.notebookUsage(current.value.length, current.value.capacity) : DASH; });
    var reserveNote = computed(function () { return view.value ? Game.notebookReserveNote(view.value.reserve) : ''; });
    var empty = computed(function () { return !!view.value && view.value.text === ''; });
    /* 快照說記事簿比面板讀到的更新（AI 剛寫入）：提示重新載入，不自動覆蓋畫面。 */
    var stale = computed(function () {
      return !!view.value && !!summary.value && summary.value.version > view.value.version;
    });

    var editState = computed(function () { return Game.notebookEditState(draft.value, view.value ? view.value.capacity : 0); });
    var draftCount = computed(function () {
      var capacity = view.value ? view.value.capacity : 0;
      return capacity > 0 ? editState.value.length + ' / ' + capacity : String(editState.value.length);
    });
    var over = computed(function () { return editState.value.over; });
    var overNote = computed(function () { return Game.notebookOverNote(editState.value.overBy); });
    var dirty = computed(function () { return editing.value && draft.value !== editBase.value; });
    var locked = computed(function () { return saving.value || ended.value; });
    var canEdit = computed(function () { return !!view.value && !ended.value && !editing.value && !saving.value; });
    var canSave = computed(function () { return editing.value && !saving.value && !ended.value && !over.value && dirty.value; });
    /* 長度已超過容量：玩家要一次刪到容量以內才能儲存，AI 才會恢復更新。 */
    var editHint = computed(function () { return editing.value && view.value && Game.notebookIsFull(view.value) ? Game.notebookText('full_edit') : ''; });

    // ---- 小工具 -------------------------------------------------------------------------------------
    function newController() { return typeof deps.AbortController === 'function' ? new deps.AbortController() : null; }
    function signalOf(controller) { return controller ? controller.signal : undefined; }
    function abort(controller) { if (controller && typeof controller.abort === 'function') { try { controller.abort(); } catch (error) { /* 忽略 */ } } }

    function exitEdit() {
      editing.value = false;
      draft.value = '';
      editBase.value = '';
      conflict.value = null;
      notice.value = null;
    }

    // ---- 讀取 ---------------------------------------------------------------------------------------
    /* 讀記事簿全文與用量。永不拋出；同時只有一個請求（再呼叫回傳同一個 Promise）。回傳是否讀到。 */
    function load() {
      if (loadingPromise) { return loadingPromise; }
      loading.value = true;
      loadError.value = null;
      var controller = newController();
      loadController = controller;
      loadingPromise = new Promise(function (resolve) { resolve(api.notebook(sessionId, { signal: signalOf(controller) })); }).then(function (data) {
        var fresh = Game.notebookView(isObject(data) ? data.notebook : null);
        if (!fresh) {
          loadError.value = { message: Game.notebookText('unavailable'), link: null, retry: true };
          return false;
        }
        view.value = fresh;
        return true;
      }, function (info) {
        noteFailure(info);
        var failure = Game.describeNotebookFailure(info);
        if (failure.kind === 'canceled') { return false; }
        if (failure.kind === 'login') { goLogin(); return false; }
        loadError.value = {
          message: failure.message, link: failure.link || null,
          retry: failure.kind !== 'not_found' && failure.kind !== 'gate' && failure.kind !== 'csrf'
        };
        return false;
      }).finally(function () {
        loading.value = false;
        loadingPromise = null;
        if (loadController === controller) { loadController = null; }
      });
      return loadingPromise;
    }

    /* 玩家開啟記事簿面板（切到「記事簿」分頁或在該分頁開啟側欄）：每次開啟都讀最新的。編輯中不重讀，草稿與版本不能被覆蓋。 */
    function open() {
      if (editing.value) { return Promise.resolve(false); }
      return load();
    }

    // ---- 編輯 ---------------------------------------------------------------------------------------
    function startEdit() {
      if (!canEdit.value) { return false; }
      draft.value = view.value.text;
      editBase.value = view.value.text;
      baseVersion = view.value.version;
      conflict.value = null;
      notice.value = null;
      editing.value = true;
      return true;
    }

    /* 「取消」：沒有未儲存的變更就直接離開；有的話先問（確認後才丟棄）。儲存進行中不處理。 */
    function cancelEdit() {
      if (!editing.value || saving.value) { return false; }
      if (dirty.value) { discardOpen.value = true; return true; }
      exitEdit();
      return true;
    }

    function confirmDiscard() {
      discardOpen.value = false;
      if (!editing.value || saving.value) { return false; }
      exitEdit();
      return true;
    }

    // ---- 儲存 ---------------------------------------------------------------------------------------
    function succeeded(data) {
      var fresh = Game.notebookView(isObject(data) ? data.notebook : null);
      exitEdit();
      loadError.value = null;
      toast(Game.notebookText('saved'));
      if (fresh) { view.value = fresh; return true; }
      return load().then(function () { return true; });                      // 回應不是記事簿的形狀：儲存已成功，重讀一次，內容以伺服器為準
    }

    function failed(info) {
      noteFailure(info);
      var failure = Game.describeNotebookFailure(info);
      if (failure.kind === 'canceled') { return false; }
      if (failure.kind === 'login') { goLogin(); return false; }
      if (failure.kind === 'ended') {
        onEnded();
        notice.value = { tone: 'info', text: failure.message };
        return false;
      }
      notice.value = { tone: 'error', text: failure.message };
      if (failure.kind === 'conflict') {
        if (failure.notebook) {
          view.value = failure.notebook;
          conflict.value = failure.notebook;
          baseVersion = failure.notebook.version;
        } else if (failure.reload) {
          return load().then(function () {
            if (view.value) { conflict.value = view.value; baseVersion = view.value.version; }
            return false;
          });
        }
      } else if (failure.kind === 'too_long' && view.value && failure.capacity !== null && failure.capacity > 0) {
        view.value = assign(assign({}, view.value), { capacity: failure.capacity });            // 伺服器說的容量為準（例如剛降級）
      }
      return false;
    }

    /* 「儲存」：只有玩家按下才會送出。草稿在任何失敗之後都還在。 */
    function save() {
      if (saving.value || !canSave.value) { return Promise.resolve(false); }   // 進行中再按只會有一個請求
      saving.value = true;
      notice.value = null;
      var controller = newController();
      saveController = controller;
      var text = draft.value;
      var version = baseVersion;
      return new Promise(function (resolve) { resolve(api.saveNotebook(sessionId, text, version, { signal: signalOf(controller) })); })
        .then(succeeded, failed).finally(function () {
          saving.value = false;
          if (saveController === controller) { saveController = null; }
        });
    }

    /* 離開頁面（pagehide）：中止進行中的讀取與儲存請求。不會有任何自動儲存。 */
    function dispose() {
      abort(loadController);
      abort(saveController);
    }

    return {
      notebook: view, notebookLoading: loading, notebookError: loadError, notebookEmpty: empty, notebookUsage: usage, notebookReserveNote: reserveNote,
      notebookFull: full, notebookFullText: Game.notebookText('full'), notebookEmptyText: Game.notebookText('empty'), notebookStale: stale,
      notebookEditing: editing, notebookDraft: draft, notebookDraftCount: draftCount, notebookOver: over, notebookOverNote: overNote,
      notebookEditHint: editHint, notebookCanEdit: canEdit, notebookCanSave: canSave, notebookLocked: locked, notebookSaving: saving,
      notebookNotice: notice, notebookConflict: conflict, notebookDiscardOpen: discardOpen,
      open: open, startEdit: startEdit, save: save, cancelEdit: cancelEdit, confirmDiscard: confirmDiscard, dispose: dispose
    };
  }

  return { createNotebook: createNotebook };
});
