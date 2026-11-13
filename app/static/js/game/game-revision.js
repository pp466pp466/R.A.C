/* 遊戲頁的「重新生成」與「編輯」最後一輪（按鈕可用性、兩個自製 Modal、串流流程、失敗還原與通知；見 rpg-revision-ui 規格與 rpg-turn-revision 的 design D8、D9）。
 *
 * `Game.createRevision(deps)` 回傳一組響應式狀態與方法，由 game.js 注入依賴並接到樣板（這個檔案本身不碰 `window`、`document`、瀏覽器儲存與主控台，
 * Node 可以用真的 Vue 與替身的串流、打字機、儲存來測試完整流程）。依賴：
 *   deps.Vue               `{ ref, computed }`
 *   deps.state             頁面初始狀態（`revision_limits`：理由字數範圍與分類列舉；`limits.action_max`；`stream` 逾時）——數字全部來自這裡，不寫死
 *   deps.host              game-session.js 的 `revisionHost`：最後一輪資訊、歷史、各種狀態旗標、氣泡覆蓋層，以及 `applyEffects`、`showFailure`、`refresh`
 *   deps.wallet            createWallet 的結果（餘額、每輪幣價；餘額不足的 Modal 與簽到）
 *   deps.stream            `{ run(options) }`（turn-stream.js；重用既有的串流客戶端，只換網址與本文）
 *   deps.store             `{ setInflight, clearInflight, putTurns }`（game-store.js；在途紀錄，與成功之後先換上新內容的快取）
 *   deps.TypewriterBuffer、deps.toast、deps.goLogin、deps.csrf、deps.now、deps.AbortController
 *
 * 規則：
 * - **按鈕只在最後一輪**（快照的 `last_turn`，且與歷史裡最後一個完成回合是同一輪）的氣泡下方：樣板以 `reviseKey`（該回合時間軸項目的 key，空字串＝不顯示）判斷。
 *   已完結、閘門非空、後端說不可修訂（`ended`、`not_last`）時不顯示；離線、串流中或修訂中、有一筆還沒確認的行動、`in_progress`、`no_snapshot` 時停用並說明原因。
 * - **重新生成 Modal**：必須選分類並填理由（字數範圍取自初始狀態，以 Unicode 字元計）才能送出；說明「理由會提供營運團隊檢視，不會送給 AI」。
 *   **理由與分類只存在記憶體**（Modal 的狀態與請求本文）：不進 IndexedDB、不進任何瀏覽器儲存、不進主控台、不進通知與錯誤訊息；取消不送出任何請求。
 * - **編輯 Modal**：以原行動預填，提醒會再次消耗遊戲幣、原本那輪已扣的幣不退、免費重新生成次數會歸零，並顯示餘額與費用；餘額不足、空白、超過字數、與原行動相同時不能送出
 *   （餘額不足時說明並可用既有的每日簽到）。字數與空白檢查與送出新行動一致（`Game.actionProblem`）。
 * - **流程**：先寫在途紀錄（種類與 `expected_revision`，編輯另含新行動；**不含理由與分類**）-> `TurnStream.run`（`…/turns/<client_msg_id>/regenerate|edit/stream`）->
 *   事件到氣泡（新文字以打字機顯示在最後一輪的覆蓋層，舊內容原封不動留在歷史裡）-> `finally`：不論結果都從最後一輪的前一個序號起重讀、重讀快照、恢復按鈕。
 *   成功（`done` 為 complete／ended）以後端內容覆寫；被擋、錯誤、逾時、中斷、取消都拿掉覆蓋層就還原舊內容，並以 Toast 說明（確定的失敗一律說「已保留原本的回應，沒有扣遊戲幣、
 *   沒有用掉免費次數」；前端中止不代表後端中止，所以取消、中斷、逾時要等重讀之後依後端的 `revision` 決定說「已保留」「已同步」或「還在處理」）。
 *   **從不自動重送，也沒有「重送」**。開始前被拒：欄位或內容規範的問題把 Modal 開回來並保留玩家填的內容，其餘交給既有的失敗顯示（餘額不足開簽到 Modal）。
 * - 修訂進行中不得送出新行動（`host.revising` 與 session 共用），離開頁面（`pagehide`）時中止請求。
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
    root.Game.createRevision = api.createRevision;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var KIND_REGENERATE = 'regenerate';
  var KIND_EDIT = 'edit';
  var CATEGORY_PATTERN = /^[a-z_]{1,40}$/;

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function noop() {}
  function trim(value) { return typeof value === 'string' ? value.replace(/^\s+|\s+$/g, '') : ''; }

  /* 初始狀態的 `revision_limits` 整理成固定形狀：字數範圍要是正整數且最小不大於最大，分類要有至少一個合格的識別字；否則 `ok` 為假（重新生成停用，失敗時關閉）。 */
  function limitsOf(state) {
    var raw = isObject(state) && isObject(state.revision_limits) ? state.revision_limits : {};
    var categories = [];
    (Array.isArray(raw.categories) ? raw.categories : []).forEach(function (value) {
      if (typeof value === 'string' && CATEGORY_PATTERN.test(value) && categories.indexOf(value) < 0) { categories.push(value); }
    });
    var min = isCount(raw.reason_min) && raw.reason_min > 0 ? raw.reason_min : 0;
    var max = isCount(raw.reason_max) && raw.reason_max > 0 ? raw.reason_max : 0;
    return { reasonMin: min, reasonMax: max, categories: categories, ok: min > 0 && max >= min && categories.length > 0 };
  }

  function createRevision(deps) {
    var ref = deps.Vue.ref;
    var computed = deps.Vue.computed;
    var host = deps.host;
    var wallet = deps.wallet;
    var stream = deps.stream;
    var store = deps.store;
    var toast = typeof deps.toast === 'function' ? deps.toast : noop;
    var goLogin = typeof deps.goLogin === 'function' ? deps.goLogin : noop;
    var initial = isObject(deps.state) ? deps.state : {};
    var streamLimits = isObject(initial.stream) ? initial.stream : {};
    var actionMax = isObject(initial.limits) && isCount(initial.limits.action_max) ? initial.limits.action_max : 0;   // 0 = 不知道：不在前端檢查長度
    var limits = limitsOf(initial);
    var sessionId = host.sessionId;

    // ---- 狀態 ---------------------------------------------------------------------------------------
    var regenerateOpen = ref(false);
    var regenerateCategory = ref('');
    var regenerateReason = ref('');
    var regenerateRejection = ref(null);   // { message, category, reason }｜null：伺服器在開始前退回欄位時的說明（玩家改動欄位之後自動不顯示）
    var editOpen = ref(false);
    var editDraft = ref('');
    var editRejection = ref(null);         // { message, draft }｜null
    var revisionBusy = host.revising;      // 與 session 共用：修訂進行中不得送出新行動

    var currentRun = null;                 // 非響應式：{ controller, aborted, … }

    // ---- 目標：最後一輪 -----------------------------------------------------------------------------
    /* 與快照的 `last_turn` 是同一輪才算（否則快取或快照其中一個過期：不顯示按鈕）。`revision` 取畫面上這份內容的修訂次數（沒有時取快照的）。 */
    var target = computed(function () {
      var last = host.lastTurn.value;
      if (!last) { return null; }
      var turn = Game.lastCompleteTurn(host.history.value);
      if (!turn || turn.client_msg_id !== last.clientMsgId || turn.seq !== last.seq) { return null; }
      return { clientMsgId: turn.client_msg_id, seq: turn.seq, action: turn.action, revision: isCount(turn.revision) ? turn.revision : last.revision };
    });

    var visible = computed(function () {
      var last = host.lastTurn.value;
      if (!target.value || !last) { return false; }
      if (host.phase.value !== 'ready' || host.ended.value || host.gate.value) { return false; }
      return last.revisable || last.reason === 'in_progress' || last.reason === 'no_snapshot';
    });
    var reviseKey = computed(function () { return visible.value ? 'turn:' + target.value.clientMsgId : ''; });

    /* 按鈕為什麼不能按（可以按回傳空字串）。順序固定：修訂中、離線、串流中、有一筆還沒確認的行動、後端說的原因。 */
    var reviseHint = computed(function () {
      if (!visible.value) { return ''; }
      if (host.revising.value) { return Game.revisionText('hint_revising'); }
      if (host.offline.value) { return Game.revisionText('hint_offline'); }
      if (host.sending.value) { return Game.revisionText('hint_sending'); }
      if (host.inflight.value) { return Game.revisionText('hint_inflight'); }
      var last = host.lastTurn.value;
      if (!last.revisable) { return last.reason === 'no_snapshot' ? Game.revisionText('hint_no_snapshot') : Game.revisionText('hint_in_progress'); }
      return '';
    });
    var reviseDisabled = computed(function () {
      return reviseHint.value !== '' || !host.historyReady.value || !host.openingReady.value;
    });

    /* 一輪的幣價：優先取錢包回應的每輪幣價（不寫死），沒有時取快照的；都沒有就是 null（不顯示數字）。 */
    function priceOf(kind) {
      var base = wallet.turnCost.value;
      if (isCount(base) && base > 0) { return base; }
      var last = host.lastTurn.value;
      var fallback = last ? (kind === KIND_EDIT ? last.editCost : last.regenerateCost) : null;
      return isCount(fallback) && fallback > 0 ? fallback : null;
    }
    function freeLeft() { var last = host.lastTurn.value; return last ? last.freeLeft : null; }

    var regenerateNote = computed(function () { return visible.value ? Game.revisionRegenerateNote(freeLeft(), priceOf(KIND_REGENERATE)) : ''; });
    var editNote = computed(function () { return visible.value ? Game.revisionEditNote(priceOf(KIND_EDIT)) : ''; });

    // ---- 重新生成 Modal -----------------------------------------------------------------------------
    var regenerateOptions = computed(function () {
      return limits.categories.map(function (value) { return { value: value, label: Game.revisionCategoryLabel(value) }; });
    });
    var reasonCount = computed(function () { return Game.reasonLength(regenerateReason.value); });
    var regenerateCount = computed(function () { return Game.revisionReasonCount(reasonCount.value, limits.reasonMax); });
    var regenerateRule = computed(function () { return Game.revisionReasonRule(limits.reasonMin, limits.reasonMax); });
    var regenerateProblem = computed(function () { return Game.reasonProblem(regenerateReason.value, limits.reasonMin, limits.reasonMax); });
    /* 說明：伺服器退回的欄位說明（欄位沒改動時）、或理由超過上限；太短不主動罵人（送出鈕停用與字數提示已經說明）。 */
    var regenerateError = computed(function () {
      var rejection = regenerateRejection.value;
      if (rejection && rejection.category === regenerateCategory.value && rejection.reason === regenerateReason.value) { return rejection.message; }
      return limits.reasonMax > 0 && reasonCount.value > limits.reasonMax ? regenerateProblem.value : '';
    });
    var regenerateCanSubmit = computed(function () {
      return limits.ok && !host.revising.value && limits.categories.indexOf(regenerateCategory.value) >= 0 && regenerateProblem.value === '';
    });
    var regenerateCostLine = computed(function () { return Game.revisionRegenerateCostLine(freeLeft(), priceOf(KIND_REGENERATE), wallet.balance.value); });

    // ---- 編輯 Modal ---------------------------------------------------------------------------------
    var editCount = computed(function () {
      var count = Game.characterCount(trim(editDraft.value));
      return actionMax > 0 ? count + ' / ' + actionMax : String(count);
    });
    /* 送出前的檢查與送出新行動一致（空白、超長），再加上「與原行動相同」。 */
    var editProblem = computed(function () {
      var problem = Game.actionProblem(editDraft.value, actionMax);
      if (problem) { return problem; }
      var now = target.value;
      return now && trim(editDraft.value) === trim(now.action) ? Game.revisionText('edit_unchanged') : '';
    });
    var editInsufficient = computed(function () {
      var price = priceOf(KIND_EDIT);
      var balance = wallet.balance.value;
      return isCount(price) && isCount(balance) && balance < price;
    });
    var editCanSubmit = computed(function () { return !host.revising.value && editProblem.value === '' && !editInsufficient.value; });
    var editHint = computed(function () { return editProblem.value; });
    var editError = computed(function () {
      var rejection = editRejection.value;
      return rejection && rejection.draft === editDraft.value ? rejection.message : '';
    });
    var editCostLine = computed(function () { return Game.revisionEditCostLine(priceOf(KIND_EDIT), wallet.balance.value, editInsufficient.value); });

    // ---- 小工具 -------------------------------------------------------------------------------------
    function newController() { return typeof deps.AbortController === 'function' ? new deps.AbortController() : null; }
    function signalOf(controller) { return controller ? controller.signal : undefined; }
    function abort(controller) { if (controller && typeof controller.abort === 'function') { try { controller.abort(); } catch (error) { /* 忽略 */ } } }
    function nowIso() { return new Date(typeof deps.now === 'function' ? deps.now() : Date.now()).toISOString(); }

    function canStart(kind) {
      if (!visible.value || reviseDisabled.value || !host.lastTurn.value.revisable || !target.value) { return false; }
      return kind === KIND_REGENERATE ? limits.ok : true;
    }

    // ---- 開啟與關閉 Modal ---------------------------------------------------------------------------
    function openRegenerate() {
      if (!canStart(KIND_REGENERATE) || host.revising.value) { return false; }
      regenerateCategory.value = '';
      regenerateReason.value = '';
      regenerateRejection.value = null;
      regenerateOpen.value = true;
      return true;
    }

    function openEdit() {
      if (!canStart(KIND_EDIT) || host.revising.value) { return false; }
      editDraft.value = target.value.action;                                 // 以原行動預填
      editRejection.value = null;
      editOpen.value = true;
      return true;
    }

    function closeRegenerate() {
      regenerateOpen.value = false;
      regenerateCategory.value = '';
      regenerateReason.value = '';
      regenerateRejection.value = null;
      return true;
    }

    function closeEdit() {
      editOpen.value = false;
      editDraft.value = '';
      editRejection.value = null;
      return true;
    }

    function selectRegenerateCategory(value) {
      if (limits.categories.indexOf(value) < 0) { return false; }
      regenerateCategory.value = value;
      return true;
    }

    // ---- 串流 ---------------------------------------------------------------------------------------
    function makeTypewriter(ctx) {
      return new deps.TypewriterBuffer({
        onUpdate: function (value) {
          if (host.overlay.value && host.overlay.value.clientMsgId === ctx.id) { host.overlay.value.story = value; }
          host.notifyContent(false);
        },
        onDone: function () { ctx.typed = true; tryDrop(ctx); }
      });
    }

    function dropOverlay(ctx) {
      if (host.overlay.value && host.overlay.value.clientMsgId === ctx.id) { host.overlay.value = null; }
    }

    /* 成功之後：打字機打完、重讀也完成，才把覆蓋層交給後端的內容（不閃爍、不截斷）。 */
    function tryDrop(ctx) {
      if (ctx.typed && ctx.settled) { dropOverlay(ctx); }
    }

    function adapterFor(ctx) {
      return {
        meta: noop,
        push: function (value) { ctx.raw += value; ctx.typewriter.push(value); },
        discard: function () {
          ctx.typewriter.cancel();
          ctx.raw = '';
          if (host.overlay.value) { host.overlay.value.story = ''; }
          ctx.typewriter = makeTypewriter(ctx);
        },
        transition: function (line, willRetry) {
          if (host.overlay.value) { host.overlay.value.notes.push(line); host.overlay.value.streaming = willRetry === true; }
        },
        finish: function () { ctx.typewriter.finish(); if (host.overlay.value) { host.overlay.value.streaming = false; } },
        hint: noop,                                                         // 被擋的說明由這裡自己組（說明保留原本的回應），不用新回合的提示
        failure: noop,                                                      // 失敗由 `describeRevisionFailure` 依串流的最後結果整理
        done: function (effect) {
          ctx.status = effect.status;
          if (effect.ended) { host.ended.value = true; }
        }
      };
    }

    /* 成功：把新內容先放進歷史（重讀失敗時畫面也不會退回舊內容），並用 `done` 的 `revision`、`free_left` 先更新最後一輪的資訊（之後的快照重讀會覆寫）。 */
    function commitLocal(ctx, run, done) {
      var turn = Game.lastCompleteTurn(host.history.value);
      if (turn && turn.client_msg_id === run.clientMsgId) {
        var next = Game.normalizeTurn({
          seq: turn.seq, client_msg_id: turn.client_msg_id, turn_number: turn.turn_number, status: 'complete',
          action: run.kind === KIND_EDIT ? run.newAction : turn.action, story: ctx.raw, revision: isObject(done) && isCount(done.revision) ? done.revision : run.expected + 1
        });
        if (next) {
          host.history.value = Game.mergeTurns(host.history.value, [next]);
          Promise.resolve(store.putTurns(sessionId, [next])).then(noop, noop);          // 快取也先換上：之後重讀失敗（離線）時，合併快取不會把舊內容蓋回來；重讀成功就以後端的內容覆寫
        }
      }
      var last = host.lastTurn.value;
      if (last && last.clientMsgId === run.clientMsgId && isObject(done)) {
        host.lastTurn.value = Object.assign({}, last, {
          revision: isCount(done.revision) ? done.revision : last.revision, freeLeft: isCount(done.free_left) ? done.free_left : last.freeLeft
        });
      }
    }

    /* 開始前被退回的欄位或內容規範問題：把 Modal 開回來，保留玩家填的內容並說明（只用固定文字與伺服器的欄位說明，不回顯內容）。 */
    function reopenAfterRejection(run, failure) {
      var fields = isObject(failure.fields) ? failure.fields : {};
      var message = (run.kind === KIND_EDIT ? fields.action : fields.reason || fields.category) || fields.expected_revision || failure.message;
      if (run.kind === KIND_EDIT) {
        editDraft.value = run.payloadText;
        editRejection.value = { message: message, draft: run.payloadText };
        editOpen.value = true;
      } else {
        regenerateCategory.value = run.payloadCategory;
        regenerateReason.value = run.payloadText;
        regenerateRejection.value = { message: message, category: run.payloadCategory, reason: run.payloadText };
        regenerateOpen.value = true;
      }
    }

    function clearRecord() { return Promise.resolve(store.clearInflight(sessionId)).then(noop, noop); }

    /* 串流結束之後（任何結果）：成功就以後端內容為準；其餘拿掉覆蓋層還原舊內容，重讀並說明。永不拋出。 */
    function afterRun(result, ctx, run) {
      var outcome = result.outcome;
      var succeeded = outcome === 'done' && (ctx.status === 'complete' || ctx.status === 'ended');

      if (succeeded) {
        commitLocal(ctx, run, result.done);
        return host.refresh(run.fromSeq).then(function () { return clearRecord(); }).then(function () {
          ctx.settled = true;
          tryDrop(ctx);
          return true;
        });
      }

      // 沒有新內容：丟棄尚未完成的文字，拿掉覆蓋層就是舊內容
      ctx.typed = true;
      ctx.settled = true;
      ctx.typewriter.cancel();
      dropOverlay(ctx);

      if (outcome === 'done') {                                              // 被擋（或狀態不明）：伺服器已退回，舊內容保留
        toast(Game.revisionText('kept_blocked'));
        return host.refresh(run.fromSeq).then(function () { return clearRecord(); }).then(function () { return false; });
      }

      var failure = Game.describeRevisionFailure(result);
      if (outcome === 'rejected') {                                          // 開始前被拒：沒有認領、沒有預扣
        return clearRecord().then(function () {
          if (failure.kind === 'fields' || failure.kind === 'rejected') { reopenAfterRejection(run, failure); return false; }
          if (failure.ended) { host.ended.value = true; }
          if (failure.kind === 'coins') { wallet.openInsufficient(failure, { revision: true }); return false; }     // 說明保留的是原本的回應，不是輸入框裡的行動
          var reload = host.showFailure(failure);
          if (reload || failure.catchUp || failure.reload) { return host.refresh(run.fromSeq).then(function () { return false; }); }
          return false;
        });
      }

      // 錯誤、取消、中斷、逾時：重讀之後依後端的 revision 說明（取消與中斷時伺服器可能已經完成）
      return host.refresh(run.fromSeq).then(function (fresh) {
        var last = host.lastTurn.value;
        var backend = {
          revision: fresh && last && last.clientMsgId === run.clientMsgId ? last.revision : null, expected: run.expected,
          inProgress: !!(fresh && last && last.clientMsgId === run.clientMsgId && last.reason === 'in_progress')
        };
        toast(Game.revisionNoticeText(failure, backend));
        return failure.uncertain && !fresh ? false : clearRecord().then(function () { return false; });      // 判定不了（重讀失敗）時紀錄留到下次載入再判定
      });
    }

    function start(kind, body, extra) {
      var now = target.value;
      if (!now || host.revising.value) { return Promise.resolve(false); }
      var run = {
        controller: newController(), aborted: false, kind: kind, clientMsgId: now.clientMsgId, seq: now.seq, expected: now.revision, fromSeq: Math.max(0, now.seq - 1),
        newAction: extra.action, payloadText: extra.text, payloadCategory: extra.category
      };
      currentRun = run;
      host.revising.value = true;
      host.dismissNotice();
      host.overlay.value = { clientMsgId: now.clientMsgId, action: kind === KIND_EDIT ? extra.action : null, story: '', streaming: true, notes: [] };
      var ctx = { id: now.clientMsgId, raw: '', typewriter: null, typed: false, settled: false, status: null };
      ctx.typewriter = makeTypewriter(ctx);
      host.notifyContent(true);
      var turn = Game.newStreamTurn(now.clientMsgId, extra.action || now.action, 'turn');
      var adapter = adapterFor(ctx);
      var record = { client_msg_id: now.clientMsgId, kind: kind, expected_revision: now.revision, action: kind === KIND_EDIT ? extra.action : '', created_at: nowIso() };

      return Promise.resolve(store.setInflight(sessionId, record)).then(noop, noop).then(function () {
        return stream.run({
          url: '/api/game/sessions/' + encodeURIComponent(sessionId) + '/turns/' + encodeURIComponent(now.clientMsgId) + '/' + (kind === KIND_EDIT ? 'edit' : 'regenerate') + '/stream',
          body: body, csrf: typeof deps.csrf === 'function' ? deps.csrf() : '',
          idleMs: streamLimits.idle_timeout_ms, totalMs: streamLimits.total_timeout_ms, signal: signalOf(run.controller),
          onEvent: function (event) {
            var step = Game.applyStreamEvent(turn, event);
            turn = step.turn;
            host.applyEffects(step.effects, adapter);
          }
        });
      }).then(function (result) {
        var end = Game.applyStreamEnd(turn, result);
        turn = end.turn;
        host.applyEffects(end.effects, adapter);
        return afterRun(result, ctx, run);
      }).then(function (value) { return value; }, function () {
        // 串流客戶端永不拋出；這裡只防止程式錯誤讓畫面卡在進行中（舊內容照舊保留）
        ctx.typed = true;
        ctx.settled = true;
        dropOverlay(ctx);
        toast(Game.revisionText('unavailable'));
        return false;
      }).finally(function () {
        run.newAction = '';
        run.payloadText = '';                                                // 理由與新行動不再需要：不留在記憶體裡
        run.payloadCategory = '';
        if (currentRun === run) { currentRun = null; }
        host.revising.value = false;                                         // finally：無論成功、失敗或中斷都恢復
      });
    }

    function submitRegenerate() {
      if (!regenerateCanSubmit.value || !canStart(KIND_REGENERATE)) { return Promise.resolve(false); }   // 重複點擊或條件不符：不送出
      var category = regenerateCategory.value;
      var reason = regenerateReason.value;
      var expected = target.value.revision;
      closeRegenerate();                                                     // Modal 關閉，理由與分類只留在這次請求的本文
      return start(KIND_REGENERATE, { expected_revision: expected, category: category, reason: reason }, { action: '', text: reason, category: category });
    }

    function submitEdit() {
      if (!editCanSubmit.value || !canStart(KIND_EDIT)) { return Promise.resolve(false); }
      var action = trim(editDraft.value);
      var expected = target.value.revision;
      closeEdit();
      return start(KIND_EDIT, { expected_revision: expected, action: action }, { action: action, text: action, category: '' });
    }

    /* 取消進行中的修訂：中止請求（不當成錯誤），重讀與說明在串流收尾時進行。 */
    function cancel() {
      var run = currentRun;
      if (!run) { return false; }
      run.aborted = true;
      abort(run.controller);
      return true;
    }

    /* 離開頁面（pagehide）：中止請求。 */
    function dispose() { if (currentRun) { abort(currentRun.controller); } }

    return {
      revisionBusy: revisionBusy, reviseKey: reviseKey, reviseDisabled: reviseDisabled, reviseHint: reviseHint, regenerateNote: regenerateNote, editNote: editNote,
      regenerateOpen: regenerateOpen, regenerateOptions: regenerateOptions, regenerateCategory: regenerateCategory, regenerateReason: regenerateReason,
      regenerateCount: regenerateCount, regenerateRule: regenerateRule, regenerateError: regenerateError, regenerateCanSubmit: regenerateCanSubmit, regenerateCostLine: regenerateCostLine,
      editOpen: editOpen, editDraft: editDraft, editCount: editCount, editHint: editHint, editError: editError, editCanSubmit: editCanSubmit,
      editInsufficient: editInsufficient, editCostLine: editCostLine,
      openRegenerate: openRegenerate, openEdit: openEdit, closeRegenerate: closeRegenerate, closeEdit: closeEdit, selectRegenerateCategory: selectRegenerateCategory,
      submitRegenerate: submitRegenerate, submitEdit: submitEdit, cancelRevision: cancel, dispose: dispose,
      target: target
    };
  }

  return { createRevision: createRevision };
});
