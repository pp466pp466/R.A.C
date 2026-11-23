/* 遊戲主畫面的狀態與流程（載入順序、第 0 幕、送出一輪、事件到時間軸的轉換、補齊、在途回合、離線、捲動；見 rpg-game-screen、rpg-stream-client、
 * rpg-game-sync 規格與 rpg-game-ui 的 design D3、D4、D5、D7）。
 *
 * `Game.createSession(deps)` 回傳一組響應式狀態與方法，由 game.js 注入依賴並接到樣板。這個檔案本身**不碰 `window`、`document`、瀏覽器儲存與主控台**
 * （儲存只透過注入的 `store`），所以 Node 可以用真的 Vue、真的 store／sync 與替身的 API 和串流來測試完整流程。依賴：
 *   deps.Vue                 `{ ref, computed, reactive }`
 *   deps.state               頁面初始狀態（存檔摘要、閘門、內容模式、串流逾時、行動字數上限；沒有任何劇情）
 *   deps.api                 `{ snapshot(id, options) }`（game-api.js）
 *   deps.store、deps.sync    game-store.js 與 game-sync.js 建立的（後端為權威，本機只是閱讀快取與單筆在途回合）
 *   deps.stream              `{ run(options) }`（turn-stream.js；原生 fetch＋ReadableStream）
 *   deps.TypewriterBuffer    打字機（UI.TypewriterBuffer）；每個氣泡一個
 *   deps.wallet              createWallet 的結果（餘額與不足時的 Modal）
 *   deps.toast、deps.goLogin、deps.newId、deps.now、deps.csrf、deps.isOnline、deps.setTimeout、deps.clearTimeout、deps.AbortController、deps.restrictionOf
 *
 * 流程重點：
 * - **載入順序**（D4）：快照 -> 第 0 幕（`ready`／`fallback` 直接顯示、**不重抽**；`pending`／`generating` 呼叫開場串流；409 `opening_in_progress` 時每 5 秒重讀快照，最多 24 次）
 *   與歷史補齊（`since_seq`）、在途回合的確認、錢包並行。第 0 幕完成之前輸入禁用。離線或帳號閘門非空時只讀本機快取。
 * - **送出一輪**：先驗證（空白、超長不送出）-> 新的 `client_msg_id` -> 先存在途回合 -> 串流 -> 事件轉成時間軸（每個氣泡一個打字機）-> `done` 後解鎖輸入、
 *   以 `since_seq` 補齊並用後端內容取代暫存的氣泡 -> `finally` 恢復。失敗保留玩家的行動文字。**重複點擊只有一個請求。**
 * - **被擋與中斷的回合不保存任何部分文字**；前端中止不代表後端中止，所以每個串流結果之後都補齊一次，在途回合是否還在以後端為準。
 * - 在途回合：`done` 或確認後端已有完整回合就移除；開始前被拒（餘額不足、驗證失敗…）也移除（什麼都沒送出）；中斷、逾時、取消則保留，補齊後仍沒有完整回合才提供「重送／放棄」，
 *   **從不自動重送**（每一輪都要消耗遊戲幣）。
 * - 快照裡的**記事簿摘要**（版本、字數、容量、是否已滿；沒有全文）放在 `notebookSummary`（每次讀到快照就更新，壞掉的摘要不改動），給 game-notebook.js 判斷遊戲頁上的「已滿」提示；
 *   它只在記憶體，不進本機暫存（game-store.js 的白名單不收），記事簿全文不經過這個檔案。
 * - **視覺小說式劇情卡**（rpg-story-view，design D2、D3）：時間軸的資料層（`items`、補齊、在途回合、IndexedDB）照舊，另外多一個閱讀器（story-reader.js）只管「當前輪」：
 *   串流的片段餵給它（`beginStory`、`feedStory`、`finishStory`、`noteStory`），由它依句分段、逐段打字、翻頁；`card` 是它的狀態經 `Game.cardView` 整理後的顯示欄位。
 *   載入既有存檔（重新整理、換裝置、離線）時由 `syncReader` 以最後一個完成的回合（沒有就是第 0 幕）載入閱讀器：停在最後一段、不打字；已經顯示了同樣內容（串流剛完成）就不動，
 *   所以玩家翻到哪一段不會被補齊或重讀快照打斷。被擋、中斷與錯誤的回合只留一行說明（`readerHold`，直到下一次送出或後端出現這一輪的完整內容）；開始前被拒絕的送出則還原成送出前的畫面。
 *   第 0 幕由串流生成時，玩家翻到最後一段之前不能使用行動區（`reading`）。閱讀位置不寫入任何儲存。
 * - **行動建議與直接推進劇情**：`toggleSuggestions` 只有玩家點圖示才呼叫 `api.suggestActions`（有快照裡已保存的就直接顯示，不呼叫）；結果只放在記憶體（`suggestItems`），
 *   點選建議只是把文字貼進輸入框（`pickSuggestion`，不送出）；新的一輪開始或最後一輪被修訂時一律收回。`advance` 以固定文字 `Game.ADVANCE_ACTION` 當一般行動送出，不讀取、不清除輸入框的草稿。
 * - 鍵盤翻頁由 `handleKey(event)` 判斷（焦點在輸入元素或有開啟的 Modal 時不作用；`deps.isModalOpen` 由掛載層注入），掛載層（game.js）負責把它接到 `document` 的 keydown。
 * - 這裡**不記錄、不輸出任何內容**（行動、劇情、第 0 幕、建議都是玩家文字）；錯誤訊息只來自 game-state.js／game-labels.js 的固定文字。
 */
(function (root, factory) {
  'use strict';
  var Game;
  if (typeof module === 'object' && module.exports) {
    Game = Object.assign({}, require('./game-labels.js'), require('./game-state.js'), require('./story-reader.js'));
  } else {
    Game = root.Game;
  }
  var api = factory(Game);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.createSession = api.createSession;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var RENDER_WINDOW = 60;                // 時間軸只渲染最近這麼多項，其餘以「顯示更早的內容」展開
  var BOTTOM_THRESHOLD = 80;             // 距離底部不超過這麼多像素算「在底部附近」，新內容才自動捲動
  var OPENING_POLL_MS = 5000;            // 另一個裝置正在生成第 0 幕時，重讀快照的間隔
  var OPENING_POLL_MAX = 24;             // 最多重讀幾次（約 2 分鐘），之後提供重試按鈕
  var OPENING_STATES = { pending: true, generating: true, ready: true, fallback: true };
  var SUGGEST_TIMEOUT_FALLBACK_MS = 35000;   // 行動建議的前端逾時：略大於後端單次請求總預算 30 秒（AGENTS.md 第 5 節）；頁面初始狀態有 `suggest_timeout_ms` 時以它為準
  var BLOCKER_TEXTS = {
    gate: '目前無法送出行動，請見上方說明。',
    ended: '存檔已完結，無法再送出新的行動。',
    offline: '目前離線，需要連線才能送出行動。',
    opening: '故事的開場還沒有準備好，完成後才能輸入行動。',
    reading: Game.actionHint('reading'),
    inflight: '有一個行動還沒有確認完成，請先在時間軸裡選擇「重送」或「放棄」。'
  };
  /* 鍵盤翻頁：這些按鍵對應的動作；焦點在這些元素（或可編輯的元素）時一律不作用。 */
  var KEY_ACTIONS = { ' ': 'next', Spacebar: 'next', Enter: 'next', ArrowRight: 'next', ArrowLeft: 'prev' };
  var INTERACTIVE_TAGS = { INPUT: true, TEXTAREA: true, SELECT: true, OPTION: true, BUTTON: true, A: true, SUMMARY: true };

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function text(value) { return typeof value === 'string' ? value : ''; }
  function noop() {}
  function assign(target, source) {
    if (source) { Object.keys(source).forEach(function (key) { target[key] = source[key]; }); }
    return target;
  }

  function createSession(deps) {
    var ref = deps.Vue.ref;
    var computed = deps.Vue.computed;
    var reactive = deps.Vue.reactive;
    var initial = isObject(deps.state) ? deps.state : {};
    var info = isObject(initial.session) ? initial.session : {};
    var mode = isObject(initial.content_mode) ? initial.content_mode : {};
    var streamLimits = isObject(initial.stream) ? initial.stream : {};
    var sessionId = text(info.id);
    var actionMax = isObject(initial.limits) && isCount(initial.limits.action_max) ? initial.limits.action_max : 0;   // 0 = 不知道：不在前端檢查長度
    var suggestTimeout = isCount(initial.suggest_timeout_ms) && initial.suggest_timeout_ms > 0 ? initial.suggest_timeout_ms : SUGGEST_TIMEOUT_FALLBACK_MS;
    var api = deps.api;
    var store = deps.store;
    var sync = deps.sync;
    var stream = deps.stream;
    var wallet = deps.wallet;
    var toast = typeof deps.toast === 'function' ? deps.toast : noop;
    var goLogin = typeof deps.goLogin === 'function' ? deps.goLogin : noop;
    var noteRestriction = typeof deps.restrictionOf === 'function' ? deps.restrictionOf : function () { return null; };
    var schedule = deps.setTimeout;
    var unschedule = deps.clearTimeout;

    // ---- 狀態 ---------------------------------------------------------------------------------------
    var phase = ref('loading');            // 'loading'｜'ready'｜'gone'｜'failed'
    var loadFailure = ref(null);
    var restriction = ref(null);
    var title = ref(text(info.title) || '未命名的劇本');
    var playerName = ref(text(info.player_name));
    var contentTier = ref(text(info.content_tier));
    var ended = ref(info.ended === true);
    var gate = ref(typeof initial.gate === 'string' && initial.gate ? initial.gate : null);
    var offline = ref(false);
    var notice = ref(null);                // { tone: 'error'｜'info', text, link }｜null
    var safeHint = ref(null);              // 遊戲內「可以嘗試關閉安全模式」提示（safe-mode-upgrade-hint）：{ text, link, linkText, closeText, closeLabel }｜null；只影響畫面
    var openingNotice = ref(null);         // { tone, text, retry }｜null
    var openingScene = ref({ state: null, text: '' });
    var history = ref([]);                 // 後端的回合（顯示用；進行中的回合沒有文字）
    var inflight = ref(null);              // { client_msg_id, action, created_at }｜null
    var world = ref(Game.worldView(null));
    var notebookSummary = ref(null);       // 快照裡的記事簿摘要 { version, length, capacity, full }｜null（沒有全文；遊戲頁上的「已滿」提示用它，不寫入本機暫存）
    var lastTurn = ref(null);              // 快照裡最後一輪的修訂資訊（Game.lastTurnView）｜null：不知道（按鈕不顯示）；新的一輪完成後先清成 null，等快照重讀
    var revising = ref(false);             // 重新生成或編輯最後一輪的串流進行中（由 game-revision.js 設定）：期間不得送出新行動
    var revisionOverlay = ref(null);       // 修訂串流中最後一輪的氣泡：{ clientMsgId, action（編輯時為新行動，否則 null）, story, streaming, notes }｜null；舊內容仍在 history 裡，沒有被改動
    var liveTurn = ref(null);              // 目前串流中的回合：{ key, clientMsgId, action, story, streaming, seq }
    var turnNotes = reactive({});          // client_msg_id -> 轉場文字（顯示在該回合的行動與劇情之間）
    var draft = ref('');
    var actionError = ref('');
    var sending = ref(false);
    var historyReady = ref(false);         // 歷史與在途回合都確認過了（確認之前不能送出：否則可能漏看「有一筆還沒完成的行動」）
    var visibleCount = ref(RENDER_WINDOW);
    var unread = ref(false);               // 玩家不在底部時有新內容到達：顯示「回到最新」
    var scrollTick = ref(0);               // game.js 監看它來捲到底部
    var card = ref(Game.cardView(null));   // 劇情卡的顯示欄位（閱讀器的狀態經 Game.cardView 整理）；沒有內容時 visible 為假
    var openingReached = ref(false);       // 第 0 幕已經翻到最後一段過（之後即使用 `‹` 回看，行動區也不再鎖上）
    var suggestOpen = ref(false);          // 行動建議是否展開（只有玩家點圖示才會有內容）
    var suggestItems = ref([]);            // 展開時顯示的建議（純文字，至多 3 個）；只在記憶體
    var suggestBusy = ref(false);          // 正在向伺服器取得建議（可取消）
    var suggestError = ref('');            // 取得建議失敗的固定說明；圖示仍可再按
    var savedSuggestions = ref([]);        // 快照裡已保存、錨點仍有效的建議：再按圖示時直接顯示，不呼叫端點
    var focusTick = ref(0);                // 貼上建議之後加一：掛載層監看它，把焦點放到輸入框並把游標移到尾端

    var live = null;                       // 非響應式：{ id, typewriter, typed, settled, raw }
    var currentRun = null;                 // 非響應式：{ controller, aborted }
    var openingRun = null;                 // 非響應式：{ controller }
    var openingTyping = null;              // 非響應式：{ typed, deferred }（第 0 幕的打字機還沒打完時，快照的新內容先放著）
    var bootController = null;
    var pollTimer = null;
    var pollCount = 0;
    var atBottom = true;
    var loading = false;
    var loadingPromise = null;
    var tailPromise = Promise.resolve();   // 上一輪 `done` 之後的補齊；下一輪等它做完才開始，所以輸入可以在 `done` 時就解鎖
    var suggestRun = null;                 // 非響應式：{ controller, canceled }（取得建議的請求）
    var readerContent = null;              // 非響應式：{ key, action, story }——閱讀器目前持有的內容（串流中隨片段增加；載入時是整輪）
    var readerHold = null;                 // 非響應式：{ base }——這一輪沒有完整內容（被擋、中斷、錯誤），劇情卡只留一行說明；base 是當時的最後完成回合的 key

    function readerTypewriter(handlers) { return new deps.TypewriterBuffer(handlers); }
    var reader = Game.createStoryReader({
      createTypewriter: typeof deps.createTypewriter === 'function' ? deps.createTypewriter : (typeof deps.TypewriterBuffer === 'function' ? readerTypewriter : undefined),
      reducedMotion: deps.reducedMotion, scheduler: deps.scheduler,
      onChange: function (state) {
        card.value = Game.cardView(state);
        if (state && state.key === 'opening' && card.value.atEnd) { openingReached.value = true; }
      }
    });

    // ---- 計算 ---------------------------------------------------------------------------------------
    // 存檔目前的生效模式（現算：玩家設定、劇本綁定、緊急煞車）；補充說明只有緊急煞車與劇本綁定兩種（沒有年齡相關的說明）
    var modeLabel = computed(function () { return Game.modeLabel(contentTier.value); });
    var modeNote = computed(function () { return Game.sessionModeNotice(mode, contentTier.value); });
    var gateNotice = computed(function () { return Game.gateNotice(gate.value); });
    var locationLine = computed(function () { return world.value.location; });
    var openingReady = computed(function () { return openingScene.value.state === 'ready' || openingScene.value.state === 'fallback'; });

    function notesOf(id) {
      var list = turnNotes[id];
      return Array.isArray(list) ? list.slice() : [];
    }

    /* 補上轉場文字：完整回合的行動與劇情之間，以及只剩一行說明的被擋回合（用伺服器送來的轉場文字取代固定說明）。 */
    function decorate(item) {
      if (item.kind === 'turn') { return assign(assign({}, item), { notes: notesOf(item.clientMsgId) }); }
      if (item.kind === 'note' && item.key.indexOf('note:') === 0) {
        var lines = notesOf(item.key.slice(5));
        if (lines.length > 0) { return assign(assign({}, item), { text: lines[lines.length - 1] }); }
      }
      return item;
    }

    var baseItems = computed(function () {
      var scene = openingScene.value;
      var opening = scene.text ? { text: scene.text, state: scene.state } : null;
      var shown = !sending.value && !liveTurn.value ? inflight.value : null;       // 串流中或剛結束時不顯示在途回合的提示卡
      return Game.timelineFrom(opening, history.value, shown).map(decorate);
    });

    /* 串流中的氣泡接在最後面；與補齊後的完整回合同一個 key，所以打字機還在跑時它蓋過後端的版本，結束後就地交給後端的版本。 */
    var timeline = computed(function () {
      var items = baseItems.value;
      var overlay = revisionOverlay.value;
      if (overlay) {                       // 修訂中：最後一輪的氣泡顯示新文字（編輯時連行動也是新的）；舊內容仍在 history，失敗時拿掉這一層就還原
        items = items.map(function (item) {
          if (item.kind !== 'turn' || item.clientMsgId !== overlay.clientMsgId) { return item; }
          return assign(assign({}, item), {
            action: overlay.action !== null ? overlay.action : item.action, story: overlay.story, streaming: overlay.streaming, notes: overlay.notes.slice()
          });
        });
      }
      var current = liveTurn.value;
      if (!current) { return items; }
      var out = items.filter(function (item) { return item.key !== current.key; });
      out.push({
        key: current.key, kind: 'turn', seq: current.seq, clientMsgId: current.clientMsgId, turnNumber: null, action: current.action,
        story: current.story, streaming: current.streaming, notes: notesOf(current.clientMsgId)
      });
      return out;
    });

    var items = computed(function () {
      var all = timeline.value;
      return all.length > visibleCount.value ? all.slice(all.length - visibleCount.value) : all;
    });
    var hasEarlier = computed(function () { return timeline.value.length > visibleCount.value; });
    var jumpVisible = computed(function () { return unread.value; });

    /* 第 0 幕還在閱讀中：劇情卡顯示的是第 0 幕、而且還沒翻到過最後一段（串流生成的第 0 幕要從第一段讀起；載入既有的第 0 幕直接停在最後一段，所以不會卡住；
       翻到過最後一段之後即使回看較早的段，行動區也不會再鎖上）。 */
    var reading = computed(function () { return card.value.visible && card.value.key === 'opening' && !openingReached.value; });

    function actionState() {
      return {
        gate: gate.value, ended: ended.value, offline: offline.value, openingReady: openingReady.value, reading: reading.value,
        inflight: sending.value ? null : inflight.value, busy: sending.value || revising.value
      };
    }
    var blockerReason = computed(function () {
      if (phase.value !== 'ready' || !historyReady.value) { return 'loading'; }
      return Game.sendBlocker(actionState());
    });
    var sendDisabled = computed(function () { return blockerReason.value !== ''; });
    var blockerText = computed(function () {
      var reason = blockerReason.value;
      return Object.prototype.hasOwnProperty.call(BLOCKER_TEXTS, reason) ? BLOCKER_TEXTS[reason] : '';
    });
    var inputDisabled = computed(function () { return !!gate.value || ended.value || phase.value !== 'ready' || !openingReady.value || reading.value; });

    /* 行動建議圖示與「直接推進劇情」：啟用條件與送出行動相同（Game.suggestBlocker／advanceBlocker）；說明是固定文字，可以按時為空字串。 */
    var suggestReason = computed(function () {
      if (phase.value !== 'ready' || !historyReady.value) { return 'loading'; }
      return Game.suggestBlocker(actionState());
    });
    var advanceReason = computed(function () {
      if (phase.value !== 'ready' || !historyReady.value) { return 'loading'; }
      return Game.advanceBlocker(actionState());
    });
    var suggestDisabled = computed(function () { return suggestReason.value !== '' && !suggestBusy.value; });   // 取得中仍可按（取消）
    var suggestHint = computed(function () { return suggestBusy.value ? Game.suggestText('loading') : Game.actionHint(suggestReason.value); });
    var advanceDisabled = computed(function () { return advanceReason.value !== ''; });
    var advanceHint = computed(function () { return Game.actionHint(advanceReason.value); });
    var draftCount = computed(function () { return Game.characterCount(draft.value.replace(/^\s+|\s+$/g, '')); });
    var draftOver = computed(function () { return actionMax > 0 && draftCount.value > actionMax; });

    // ---- 小工具 -------------------------------------------------------------------------------------
    function newController() { return typeof deps.AbortController === 'function' ? new deps.AbortController() : null; }
    function signalOf(controller) { return controller ? controller.signal : undefined; }
    function abort(controller) { if (controller && typeof controller.abort === 'function') { try { controller.abort(); } catch (error) { /* 忽略 */ } } }
    function clearPoll() { if (pollTimer !== null) { unschedule(pollTimer); pollTimer = null; } }

    function noteFailureInfo(source) {
      var kind = noteRestriction(source);
      if (kind) { restriction.value = kind; }
    }

    /* 新內容到達：玩家在底部附近就捲到底，否則只標示有新內容（不強制捲動）。 */
    function notifyContent(force) {
      if (force === true) { atBottom = true; }                              // 玩家剛送出行動、或剛載入完：跟著內容走
      if (atBottom) { scrollTick.value += 1; unread.value = false; } else { unread.value = true; }
    }

    function onScroll(source) {
      var target = isObject(source) && isObject(source.target) ? source.target : source;
      if (!isObject(target) || !isCount(target.scrollHeight) || !isCount(target.clientHeight) || typeof target.scrollTop !== 'number') { return; }
      atBottom = target.scrollHeight - target.scrollTop - target.clientHeight <= BOTTOM_THRESHOLD;
      if (atBottom) { unread.value = false; }
    }

    function jumpToLatest() { atBottom = true; unread.value = false; scrollTick.value += 1; }
    function showEarlier() { visibleCount.value += RENDER_WINDOW; }
    function dismissNotice() { notice.value = null; }
    /* 關閉提示只影響畫面：不送任何請求、不改變安全模式的設定，也不改變伺服器端「已提示」的紀錄（重新整理或換裝置都不會再出現）。 */
    function dismissSafeHint() { safeHint.value = null; }

    function makeTypewriter(onText, onDone) {
      return new deps.TypewriterBuffer({ onUpdate: function (value) { onText(value); notifyContent(false); }, onDone: onDone });
    }

    // ---- 劇情卡（閱讀器）-----------------------------------------------------------------------------
    function storyKey(turn) { return 'turn:' + (turn.client_msg_id || 'seq-' + turn.seq); }

    /* 閱讀器應該顯示的當前輪：最後一個完成的回合；尚無完成的回合時是已完成的第 0 幕；都沒有是 null。 */
    function readerTarget() {
      var turn = Game.lastCompleteTurn(history.value);
      if (turn) { return { key: storyKey(turn), action: turn.action, story: turn.story }; }
      var scene = openingScene.value;
      if ((scene.state === 'ready' || scene.state === 'fallback') && scene.text) { return { key: 'opening', action: '', story: scene.text }; }
      return null;
    }
    function readerTargetKey() { var target = readerTarget(); return target ? target.key : null; }
    /* 比較劇情時忽略空白的差異（後端定稿可能修剪頭尾或換行），避免串流剛完成就被「重新載入」而跳到最後一段。 */
    function sameStory(left, right) { return text(left).replace(/\s+/g, ' ').trim() === text(right).replace(/\s+/g, ' ').trim(); }
    /* 串流中（回合、第 0 幕、修訂）閱讀器由串流事件驅動，不被補齊或快照重讀打斷。 */
    function readerBusy() { return !!liveTurn.value || !!openingRun || revising.value; }

    /* 讓閱讀器對齊目前的資料：已經顯示同一輪的同樣內容就不動（玩家翻到哪一段不被打斷）；否則載入（停在最後一段、不打字）。
       被擋、中斷或錯誤的那一輪（`readerHold`）維持一行說明，直到後端出現這一輪的完整內容或另一個裝置新增了回合。 */
    function syncReader() {
      if (!historyReady.value || readerBusy()) { return false; }
      var target = readerTarget();
      if (!target) { return false; }
      if (readerHold) {
        if (target.key === readerHold.base) { return false; }
        readerHold = null;
      }
      if (readerContent && readerContent.key === target.key && sameStory(readerContent.story, target.story)) { return false; }
      reader.load(target.key, { action: target.action, story: target.story });
      readerContent = { key: target.key, action: target.action, story: target.story };
      return true;
    }

    /* 新的一輪（回合、第 0 幕、修訂）開始：收回建議並丟掉已保存的（劇情前進後它們就過期了），閱讀器立即顯示玩家的行動（第 0 幕沒有行動）。 */
    function beginStory(key, action) {
      cancelSuggestRun();
      closeSuggestions();
      savedSuggestions.value = [];
      suggestError.value = '';
      readerHold = null;
      readerContent = { key: key, action: text(action), story: '' };
      if (key === 'opening') { openingReached.value = false; }                       // 新生成的第 0 幕要從第一段讀到最後一段
      reader.reset(key, { action: text(action) });
    }
    function feedStory(chunk) {
      if (readerContent && typeof chunk === 'string') { readerContent.story += chunk; }
      reader.feed(chunk, false);
    }
    function finishStory() { reader.feed('', true); }
    /* 轉場、被擋、中斷與錯誤：丟棄尚未完成的劇情，劇情卡只留一行說明；`willRetry` 為真（自動重生中）時仍在等待新內容。 */
    function noteStory(line, willRetry) {
      if (readerContent) { readerContent.story = ''; }
      reader.discard(text(line), { willRetry: willRetry === true });
    }
    function holdStory(base) { readerHold = { base: base === undefined ? null : base }; }
    /* 修訂失敗與開始前被拒絕時還原用：只在記憶體，不寫入任何儲存。 */
    function saveStory() {
      return { reader: reader.snapshot(), content: readerContent ? assign({}, readerContent) : null, hold: readerHold ? assign({}, readerHold) : null };
    }
    function restoreStory(saved) {
      if (!isObject(saved)) { return; }
      readerContent = saved.content ? assign({}, saved.content) : null;
      readerHold = saved.hold ? assign({}, saved.hold) : null;
      if (saved.reader) { reader.restore(saved.reader); }
    }
    /* 串流沒有完整結束時劇情卡上的一行說明：用整理過的失敗說明（固定文字），沒有就用該結果的固定說明。 */
    function streamLine(result) {
      var failure = Game.describeStreamFailure(result);
      return (failure && failure.message) || Game.streamOutcomeNotice(isObject(result) ? result.outcome : '');
    }

    // ---- 快照與歷史 ---------------------------------------------------------------------------------
    /* 第 0 幕的狀態與文字。打字機還沒打完時把後端的內容先放著，打完再換上（不閃爍、不截斷）。 */
    function setOpening(opening) {
      if (!isObject(opening) || typeof opening.state !== 'string' || !OPENING_STATES[opening.state]) { return; }
      var next = { state: opening.state, text: text(opening.text) };
      if (openingTyping && !openingTyping.typed) { openingTyping.deferred = next; return; }
      openingScene.value = next;
      if (next.state === 'ready' || next.state === 'fallback') { openingNotice.value = null; clearPoll(); }
      syncReader();                                                                // 另一個裝置生成了第 0 幕、或後端的版本換上了：劇情卡跟上（串流中不動）
    }

    function applySnapshot(snapshot) {
      if (typeof snapshot.title === 'string' && snapshot.title) { title.value = snapshot.title; }
      if (typeof snapshot.content_tier === 'string') { contentTier.value = snapshot.content_tier; }
      ended.value = snapshot.ended === true;
      world.value = Game.worldView(snapshot.world);
      var brief = Game.notebookSummary(snapshot.notebook);
      if (brief) { notebookSummary.value = brief; }
      lastTurn.value = Game.lastTurnView(snapshot.last_turn);                       // 沒有或壞掉的資料就是 null：不顯示重新生成與編輯
      savedSuggestions.value = Game.suggestionItems(snapshot.suggestions);          // 錨點仍有效、已保存的建議（過期的伺服器已不帶）；只在記憶體
      setOpening(snapshot.opening);
    }

    function applyCachedSession(record) {
      if (record.title) { title.value = record.title; }
      if (record.content_tier) { contentTier.value = record.content_tier; }
      ended.value = record.ended === true;
      world.value = Game.worldView(record.world);
      if (isObject(record.opening) && OPENING_STATES[record.opening.state]) {
        openingScene.value = { state: record.opening.state, text: text(record.opening.text) };
      }
    }

    function applyCatchUp(result) {
      if (!result) { return; }
      if (result.status === 'gone') { phase.value = 'gone'; history.value = []; inflight.value = null; return; }
      if (result.status === 'offline') { offline.value = true; }
      history.value = result.status === 'ok' ? result.turns : Game.mergeTurns(history.value, result.turns);
      if (result.status === 'failed' && result.error) {
        noteFailureInfo(result.error);
        var failure = Game.describeRequestFailure(result.error);
        if (failure.kind === 'login') { goLogin(); }
        if (failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; }
      }
    }

    /* 背景重讀快照（世界狀態、是否完結、第 0 幕）。永不拋出；回傳快照或 false。 */
    function refreshSnapshot(signal) {
      if (!sessionId) { return Promise.resolve(false); }
      return new Promise(function (resolve) { resolve(api.snapshot(sessionId, { signal: signal })); }).then(function (data) {
        var snapshot = isObject(data) ? data.session : null;
        if (!isObject(snapshot)) { return false; }
        offline.value = false;
        applySnapshot(snapshot);
        sync.remember(sessionId, snapshot);
        return snapshot;
      }, function (failed) {
        noteFailureInfo(failed);
        var failure = Game.describeRequestFailure(failed);
        if (failure.kind === 'not_found') { phase.value = 'gone'; store.clearSession(sessionId).then(noop, noop); }
        else if (failure.kind === 'login') { goLogin(); }
        else if (failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; }
        else if (failure.kind === 'network' || failure.kind === 'timeout') { offline.value = true; }
        return false;
      });
    }

    // ---- 失敗的顯示 ---------------------------------------------------------------------------------
    /* 把整理過的失敗（Game.describe*Failure 的結果）顯示出來。回傳是否需要重讀快照。 */
    function showFailure(failure) {
      if (!isObject(failure) || failure.silent) { return false; }
      noteFailureInfo({ status: failure.status, code: failure.code });
      if (failure.kind === 'coins') { wallet.openInsufficient(failure); return false; }
      if (failure.kind === 'login') { goLogin(); return false; }
      if (failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; return false; }      // 橫幅會說明並附連結
      if (failure.kind === 'ended') { ended.value = true; }
      if (failure.kind === 'fields') {
        var field = text(failure.fields && failure.fields.action);
        actionError.value = field || failure.message;
        return false;
      }
      var tone = failure.kind === 'ended' || failure.kind === 'busy' ? 'info' : 'error';
      notice.value = { tone: tone, text: failure.message, link: failure.link || null };
      return failure.reload === true;
    }

    // ---- 載入 ---------------------------------------------------------------------------------------
    function failLoad(message, link, retry) {
      loadFailure.value = { message: message, link: link || null, retry: retry === true };
      phase.value = 'failed';
    }

    /* 只讀本機快取（離線，或帳號閘門非空而伺服器不提供內容）。 */
    function loadFromCache(reason) {
      return Promise.all([store.getSession(sessionId), store.turnsOf(sessionId), store.getInflight(sessionId)]).then(function (parts) {
        var record = parts[0];
        var turns = parts[1];
        if (reason === 'offline') { offline.value = true; }
        if (!record && turns.length === 0 && reason === 'offline') {
          failLoad('目前離線，而且這個存檔還沒有可以離線閱讀的內容。請連上網路後再試。', null, true);
          return false;
        }
        if (record) { applyCachedSession(record); }
        history.value = turns;
        inflight.value = parts[2];
        loadFailure.value = null;
        phase.value = 'ready';
        historyReady.value = true;
        syncReader();
        notifyContent(true);
        return true;
      }, function () {
        failLoad('無法讀取本機暫存的內容，請連上網路後重試。', null, true);
        return false;
      });
    }

    /* 載入完成後的歷史補齊與在途回合的確認（不依賴第 0 幕）。 */
    /* 重新整理後遇到修訂（重新生成、編輯）的在途紀錄：換上最新的回合；沒有完成時說明「原本的回應已保留」。成功時什麼都不說，也不重送。 */
    function onRevisionResolved(info) {
      if (!isObject(info)) { return; }
      if (Array.isArray(info.turns)) { history.value = Game.mergeTurns(history.value, info.turns); }
      if (info.outcome === 'incomplete') { notice.value = { tone: 'info', text: Game.revisionText('incomplete'), link: null }; }
    }

    /* 載入時從「最後一輪的前一個序號」重讀（快照已先讀到 `last_turn`）：其他裝置的修訂與過期的快取內容被後端的內容覆寫。 */
    function loadHistory(signal) {
      var from = lastTurn.value ? Math.max(0, lastTurn.value.seq - 1) : undefined;
      return sync.catchUp(sessionId, { signal: signal, fromSeq: from }).then(function (result) {
        applyCatchUp(result);
        return sync.resolveInflight(sessionId, { signal: signal, result: result, onRevision: onRevisionResolved }).then(function (record) { inflight.value = record; });
      }).then(function () { historyReady.value = true; syncReader(); });
    }

    function loadOnline(soft, controller) {
      var signal = signalOf(controller);
      return new Promise(function (resolve) { resolve(api.snapshot(sessionId, { signal: signal })); }).then(function (data) {
        var snapshot = isObject(data) ? data.session : null;
        if (!isObject(snapshot)) { failLoad('無法讀取這個存檔，請稍後再試。', null, true); return false; }
        offline.value = false;
        applySnapshot(snapshot);
        sync.remember(sessionId, snapshot);
        loadFailure.value = null;
        phase.value = 'ready';
        var opening = isObject(snapshot.opening) ? snapshot.opening.state : null;
        if (opening === 'pending' || opening === 'generating') { runOpening(); }                // 不等它：開場串流可能要幾十秒，歷史與錢包並行載入
        return Promise.all([loadHistory(signal), wallet.load({ signal: signal })]).then(function () { notifyContent(true); return true; });
      }, function (failed) {
        noteFailureInfo(failed);
        var failure = Game.describeRequestFailure(failed);
        if (failure.kind === 'canceled') { return false; }
        if (failure.kind === 'login') { goLogin(); failLoad(failure.message, null, false); return false; }
        if (failure.kind === 'not_found') { return store.clearSession(sessionId).then(noop, noop).then(function () { phase.value = 'gone'; return false; }); }
        if (failure.kind === 'network' || failure.kind === 'timeout') {
          if (soft && phase.value === 'ready') { offline.value = true; return false; }
          return loadFromCache('offline');
        }
        if (failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; return loadFromCache('gate'); }
        failLoad(failure.message, failure.link, true);
        return false;
      });
    }

    /* 載入。`soft` 為真（恢復連線後重新載入）時不把畫面切回「載入中」。永不拋出；同時只有一個載入在跑。 */
    function load(options) {
      if (loading) { return loadingPromise; }
      var soft = !!(options && options.soft);
      loading = true;
      if (!soft) { phase.value = 'loading'; loadFailure.value = null; historyReady.value = false; }
      abort(bootController);
      bootController = newController();
      var controller = bootController;
      var task;
      if (!sessionId) {
        failLoad('找不到這個存檔，它可能已被刪除。', { href: '/game', text: '回到冒險' }, false);
        task = Promise.resolve(false);
      } else if (gate.value) {
        task = loadFromCache('gate');
      } else if (typeof deps.isOnline === 'function' && deps.isOnline() === false) {
        task = loadFromCache('offline');
      } else {
        task = loadOnline(soft, controller);
      }
      loadingPromise = task.catch(function () { failLoad('載入時發生了預期之外的問題，請稍後再試。', null, true); return false; }).finally(function () {
        loading = false;
        loadingPromise = null;
      });
      return loadingPromise;
    }

    function retryLoad() { return load(); }

    /* 連線狀態改變（game.js 轉接瀏覽器的 online／offline 事件）。恢復連線後重新走一次載入順序。 */
    function setOnline(online) {
      if (online === false) { offline.value = true; return Promise.resolve(false); }
      if (offline.value && !gate.value) { offline.value = false; return load({ soft: true }); }
      return Promise.resolve(false);
    }

    // ---- 第 0 幕 ------------------------------------------------------------------------------------
    function openingText(value) { openingScene.value = { state: openingScene.value.state, text: value }; }

    function applyDeferred() {
      var pending = openingTyping ? openingTyping.deferred : null;
      openingTyping = null;
      if (pending) { setOpening(pending); }
    }

    function schedulePoll() {
      clearPoll();
      if (pollCount >= OPENING_POLL_MAX) {
        openingNotice.value = { tone: 'error', text: '故事的開場等了很久還沒有準備好。', retry: true };
        return;
      }
      pollTimer = schedule(function () {
        pollTimer = null;
        pollCount += 1;
        refreshSnapshot(signalOf(bootController)).then(function (snapshot) {
          var state = snapshot && isObject(snapshot.opening) ? snapshot.opening.state : null;
          if (state === 'ready' || state === 'fallback') { return; }            // setOpening 已經換上並清掉提示
          if (state === 'pending') { runOpening(); return; }                    // 另一個裝置放棄了：換這個裝置生成
          if (phase.value === 'ready') { schedulePoll(); }
        });
      }, OPENING_POLL_MS);
    }

    /* 開場串流。已有一個在跑時不重複。 */
    function runOpening() {
      if (openingRun || !sessionId) { return Promise.resolve(false); }
      clearPoll();
      pollCount = 0;
      openingNotice.value = null;
      var run = { controller: newController() };
      openingRun = run;
      openingTyping = { typed: false, deferred: null };
      var typing = openingTyping;
      var typewriter = makeTypewriter(openingText, function () { typing.typed = true; if (openingTyping === typing) { applyDeferred(); } });
      openingScene.value = { state: 'generating', text: '' };
      var turn = Game.newStreamTurn('', '', 'opening');
      var openingLine = '';                                                      // 最後一次轉場的說明（劇情卡的一行說明）
      beginStory('opening', '');                                                 // 劇情卡從第 0 幕的第一段讀起（沒有玩家行動）

      var adapter = {
        meta: noop,
        push: function (value) { typewriter.push(value); feedStory(value); },
        discard: function () {
          typewriter.cancel();
          openingText('');
          typewriter = makeTypewriter(openingText, function () { typing.typed = true; if (openingTyping === typing) { applyDeferred(); } });
          noteStory(openingLine, true);
        },
        transition: function (line, willRetry) {
          openingNotice.value = { tone: 'info', text: line, retry: false };
          openingLine = line;
          noteStory(line, willRetry === true);
        },
        finish: function () { typewriter.finish(); finishStory(); },
        done: function (effect) {
          openingScene.value = { state: effect.openingState || 'ready', text: openingScene.value.text };
          openingNotice.value = null;
          if (effect.status !== 'complete' && effect.status !== 'ended') { noteStory(openingLine || Game.historyLine('blocked'), false); }   // 被擋：退回劇本的初始對話（快照），先只留一行說明
        },
        failure: noop,
        hint: noop
      };

      return stream.run({
        url: '/api/game/sessions/' + encodeURIComponent(sessionId) + '/opening/stream',
        csrf: typeof deps.csrf === 'function' ? deps.csrf() : '',
        idleMs: streamLimits.idle_timeout_ms, totalMs: streamLimits.total_timeout_ms, signal: signalOf(run.controller),
        onEvent: function (event) {
          var step = Game.applyStreamEvent(turn, event);
          turn = step.turn;
          applyEffects(step.effects, adapter);
        }
      }).then(function (result) {
        var end = Game.applyStreamEnd(turn, result);
        turn = end.turn;
        if (result.outcome === 'done') { return afterOpeningDone(); }
        typewriter.cancel();
        typing.typed = true;
        openingTyping = null;
        var failedFailure = Game.describeStreamFailure(result);
        if (!(failedFailure && failedFailure.silent)) { noteStory(streamLine(result), false); }   // 沒有完整的第 0 幕：只留一行說明（不留部分內容），重試或快照換上內容時會被取代
        return afterOpeningFailed(result);
      }).finally(function () { openingRun = null; syncReader(); });
    }

    /* 開場串流完成：重讀快照（`fallback` 的文字來自快照；`ready` 的文字等打字機打完再換成後端的版本）。 */
    function afterOpeningDone() {
      return refreshSnapshot(signalOf(bootController)).then(function () { return true; });
    }

    function afterOpeningFailed(result) {
      var failure = Game.describeStreamFailure(result);
      if (failure && failure.silent) {                                            // 取消（離開頁面）：什麼都不顯示
        openingScene.value = { state: 'pending', text: '' };
        return Promise.resolve(false);
      }
      if (failure && failure.kind === 'opening_wait') {
        openingScene.value = { state: 'generating', text: '' };
        openingNotice.value = { tone: 'info', text: '另一個裝置正在準備故事的開場，請稍候；準備好之後會自動顯示。', retry: false };
        schedulePoll();
        return Promise.resolve(false);
      }
      if (failure && failure.kind === 'login') { goLogin(); }
      if (failure && failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; }
      if (failure && failure.kind === 'not_found') { phase.value = 'gone'; }
      openingScene.value = { state: 'pending', text: '' };
      openingNotice.value = { tone: 'error', text: failure ? failure.message : '故事的開場暫時無法準備。', retry: true };
      // 前端中止不代表後端中止：伺服器可能已經完成第 0 幕，重讀快照（成功換上文字時會一併清掉這則提示）
      return refreshSnapshot(signalOf(bootController)).then(function () { return false; });
    }

    function retryOpening() { pollCount = 0; return runOpening(); }

    // ---- 送出一輪 -----------------------------------------------------------------------------------
    function addNote(id, line) { turnNotes[id] = (Array.isArray(turnNotes[id]) ? turnNotes[id] : []).concat([line]); }

    function bubbleTypewriter(context) {
      return makeTypewriter(function (value) {
        if (liveTurn.value && liveTurn.value.clientMsgId === context.id) { liveTurn.value.story = value; }
      }, function () { context.typed = true; tryDropLive(context); });
    }

    function tryDropLive(context) {
      if (live === context && context.typed && context.settled) { liveTurn.value = null; live = null; syncReader(); }
    }

    /* 立刻結束上一個串流中的氣泡（它的內容已經在歷史裡）。 */
    function dropLive() {
      if (!live) { return; }
      var context = live;
      try { context.typewriter.flush(); } catch (error) { /* 忽略 */ }
      live = null;
      liveTurn.value = null;
      context.typed = true;
    }

    function beginLive(id, action) {
      dropLive();
      turnNotes[id] = [];
      liveTurn.value = { key: 'turn:' + id, clientMsgId: id, action: action, story: '', streaming: true, seq: null };
      live = { id: id, typewriter: null, typed: false, settled: false, raw: '', reload: false, note: '', base: readerTargetKey(), pre: saveStory() };
      live.typewriter = bubbleTypewriter(live);
      beginStory('turn:' + id, action);                                                        // 劇情卡立即顯示「你」的行動頁，之後的片段由閱讀器分段
    }

    /* 串流中收到 `done`（完整或結局）：後端已寫入，先把這一輪放進歷史（之後的補齊以後端的內容覆寫它）。 */
    function commitLocal(context, effect) {
      if (!liveTurn.value || !isCount(liveTurn.value.seq)) { return; }
      var turn = Game.normalizeTurn({
        seq: liveTurn.value.seq, client_msg_id: context.id, turn_number: effect.turnNumber, status: 'complete',
        action: liveTurn.value.action, story: context.raw
      });
      if (turn) { history.value = Game.mergeTurns(history.value, [turn]); }
    }

    function turnAdapter(context, run) {
      return {
        meta: function (effect) { if (liveTurn.value && isCount(effect.seq)) { liveTurn.value.seq = effect.seq; } },
        push: function (value) { context.raw += value; context.typewriter.push(value); feedStory(value); },
        discard: function () {
          context.typewriter.cancel();
          context.raw = '';
          if (liveTurn.value) { liveTurn.value.story = ''; }
          context.typewriter = bubbleTypewriter(context);
          noteStory(context.note, true);                                                           // 還沒有新的內容：劇情卡只留上一則說明（`transition` 緊接著會換成這次的說明）
        },
        transition: function (line, willRetry) {
          addNote(context.id, line);
          if (liveTurn.value) { liveTurn.value.streaming = willRetry === true; }
          context.note = line;
          noteStory(line, willRetry === true);
        },
        finish: function () { context.typewriter.finish(); if (liveTurn.value) { liveTurn.value.streaming = false; } finishStory(); },
        hint: function (line) { notice.value = { tone: 'info', text: line, link: null }; },
        done: function (effect) {
          if (effect.ended) { ended.value = true; }
          if (liveTurn.value) { liveTurn.value.streaming = false; }
          if (effect.status === 'complete' || effect.status === 'ended') {
            lastTurn.value = null;                                                                 // 最後一輪換人了：舊的修訂資訊作廢，等快照重讀才顯示按鈕
            commitLocal(context, effect);
            if (!run.keepDraft && draft.value.replace(/^\s+|\s+$/g, '') === run.action) { draft.value = ''; }   // 成功才清空；被擋時保留，玩家可以修改後再試（直接推進劇情不動草稿）
          } else {
            context.typed = true;                                                                  // 被擋：沒有文字要打
            noteStory(context.note || Game.historyLine(effect.status === 'blocked' ? 'blocked' : 'interrupted'), false);   // 這一輪只留一行說明，直到後端出現它的完整內容或下一次送出
            holdStory(context.base);
          }
          store.clearInflight(sessionId).then(noop, noop);                                         // `done` 就是完整接收的依據：在途回合不再需要
          inflight.value = null;
          if (currentRun === run) { sending.value = false; }                                       // 輸入在 `done` 時就解鎖，不等打字機與補齊
        },
        failure: function (failure) { if (showFailure(failure)) { context.reload = true; } }
      };
    }

    /* 掛載層處理 effects 的順序固定（見 game-state.js 的說明）；`adapter` 決定每種 effect 對第 0 幕或回合的做法。 */
    function applyEffects(effects, adapter) {
      effects.forEach(function (effect) {
        if (effect.type === 'meta') { adapter.meta(effect); }
        else if (effect.type === 'push') { adapter.push(effect.text); }
        else if (effect.type === 'discard') { adapter.discard(); }
        else if (effect.type === 'transition') { adapter.transition(effect.text, effect.willRetry); }
        else if (effect.type === 'world') { world.value = Game.mergeWorld(world.value, effect.world); }
        else if (effect.type === 'notices') { effect.notices.forEach(function (item) { if (item.text) { toast(item.text); } }); }
        else if (effect.type === 'safe_hint') {
          if (effect.text) { safeHint.value = Game.safeHintCopy(Game.sessionPath(sessionId)); }       // 文字固定（不採用伺服器或模型的任何文字）；返回位置是目前存檔的頁面
        }
        else if (effect.type === 'finish') { adapter.finish(); }
        else if (effect.type === 'hint') { adapter.hint(effect.text); }
        else if (effect.type === 'coins') { wallet.setBalance(effect.balance); }
        else if (effect.type === 'failure') { adapter.failure(effect.failure); }
        else if (effect.type === 'done') { adapter.done(effect); }
      });
    }

    /* 串流結束之後（任何結果）：補齊一次，在途回合以後端為準，然後收尾氣泡。 */
    function afterRun(result, context, run) {
      var outcome = result.outcome;
      var failure = Game.describeStreamFailure(result);
      var catchUp = outcome !== 'rejected' || (!!failure && failure.catchUp === true);
      var keep = outcome !== 'done' && outcome !== 'rejected' && !(outcome === 'error_event' && !(failure && failure.catchUp));
      if (outcome !== 'done') { context.typed = true; if (liveTurn.value) { liveTurn.value.streaming = false; } }
      if (!keep) { store.clearInflight(sessionId).then(noop, noop); inflight.value = null; }
      if (outcome === 'aborted' && run.aborted) { notice.value = { tone: 'info', text: Game.streamOutcomeNotice('aborted'), link: null }; }

      var work = catchUp ? sync.catchUp(sessionId) : Promise.resolve(null);
      return work.then(function (found) {
        applyCatchUp(found);
        if (!keep) { return null; }
        return sync.resolveInflight(sessionId, found ? { result: found } : undefined).then(function (record) { inflight.value = record; });
      }).then(function () {
        if (outcome === 'done' || context.reload) { return refreshSnapshot(); }
        return null;
      }).then(function (snapshot) {
        if (context.reload && snapshot && isObject(snapshot.opening) && (snapshot.opening.state === 'pending' || snapshot.opening.state === 'generating')) {
          runOpening();
        }
      }).then(function () {
        context.settled = true;
        if (outcome !== 'done' && live === context) { liveTurn.value = null; live = null; }       // 沒有 done：丟棄尚未完成的氣泡，歷史以後端為準
        else { tryDropLive(context); }
        syncReader();                                                                             // 補齊之後：後端若已有這一輪的完整內容（中斷但伺服器完成了）劇情卡換上它
      });
    }

    /* 串流結束後劇情卡的收尾（`done` 的完成與被擋已由事件處理）：開始前就被拒絕（餘額不足、內容規範、閘門…）什麼都沒發生，還原成送出前的畫面；
       錯誤、取消、中斷與逾時留一行說明（不留部分內容）；前端中止不代表後端中止，補齊之後後端若有這一輪的完整內容，`syncReader` 會換上它。 */
    function settleStory(result, context) {
      if (result.outcome === 'done') { return; }
      if (result.outcome === 'rejected') { restoreStory(context.pre); return; }
      noteStory(streamLine(result), false);
      holdStory(context.base);
    }

    function execute(run, options) {
      var id = options.clientMsgId;
      var action = options.action;
      run.action = action;
      beginLive(id, action);
      var context = live;
      notifyContent(true);
      var turn = Game.newStreamTurn(id, action, 'turn');
      var adapter = turnAdapter(context, run);
      var record = options.resend && inflight.value ? inflight.value : { client_msg_id: id, action: action, created_at: new Date(typeof deps.now === 'function' ? deps.now() : Date.now()).toISOString() };
      inflight.value = record;
      return store.setInflight(sessionId, record).then(function () {
        return stream.run({
          url: '/api/game/sessions/' + encodeURIComponent(sessionId) + '/turns/stream',
          body: { client_msg_id: id, action: action },
          csrf: typeof deps.csrf === 'function' ? deps.csrf() : '',
          idleMs: streamLimits.idle_timeout_ms, totalMs: streamLimits.total_timeout_ms, signal: signalOf(run.controller),
          onEvent: function (event) {
            var step = Game.applyStreamEvent(turn, event);
            turn = step.turn;
            applyEffects(step.effects, adapter);
          }
        });
      }).then(function (result) {
        var end = Game.applyStreamEnd(turn, result);
        turn = end.turn;
        applyEffects(end.effects, adapter);
        settleStory(result, context);
        return afterRun(result, context, run);
      }).then(function () { return true; }, function () {
        // 串流客戶端永不拋出；這裡只防止程式錯誤讓畫面卡在進行中
        context.typed = true;
        context.settled = true;
        notice.value = { tone: 'error', text: '這一輪沒有順利完成，已重新整理狀態。遊戲幣以伺服器的結算為準。', link: null };
        liveTurn.value = null;
        live = null;
        restoreStory(context.pre);                                                             // 劇情卡不要停在「等待中」：還原成送出前的畫面，之後的補齊會對齊後端
        syncReader();
        return false;
      }).finally(function () {
        if (currentRun === run) { sending.value = false; currentRun = null; }                // finally：無論成功、失敗或中斷都恢復
      });
    }

    function runTurn(options) {
      if (sending.value || revising.value) { return Promise.resolve(false); }                   // 重複點擊：只有一個請求；修訂進行中也不得送出新行動
      sending.value = true;
      notice.value = null;
      actionError.value = '';
      var run = { controller: newController(), aborted: false, action: options.action, keepDraft: options.keepDraft === true };
      currentRun = run;
      var task = tailPromise.then(function () {
        if (run.aborted) { return false; }
        return execute(run, options);
      }).finally(function () { if (currentRun === run) { sending.value = false; currentRun = null; } });
      tailPromise = task.then(noop, noop);
      return task;
    }

    function send() {
      if (sending.value || sendDisabled.value) { return Promise.resolve(false); }
      var problem = Game.actionProblem(draft.value, actionMax);
      if (problem) { actionError.value = problem; return Promise.resolve(false); }
      return runTurn({ clientMsgId: deps.newId(), action: draft.value.replace(/^\s+|\s+$/g, ''), resend: false });
    }

    /* 在途回合的「重送」：用原本的 client_msg_id（後端冪等：已完成就重播、不重複扣幣）。 */
    function resendInflight() {
      var record = inflight.value;
      if (!record || sending.value || revising.value || offline.value || ended.value || gate.value || !openingReady.value) { return Promise.resolve(false); }
      return runTurn({ clientMsgId: record.client_msg_id, action: record.action, resend: true });
    }

    /* 在途回合的「放棄」：只移除本機提示。伺服器若已完成它，結果仍會出現在歷史裡。 */
    function abandonInflight() {
      inflight.value = null;
      return store.clearInflight(sessionId).then(function () { return true; }, function () { return false; });
    }

    /* 取消進行中的一輪：中止請求（不顯示成錯誤），補齊在串流收尾時進行。 */
    function cancel() {
      var run = currentRun;
      if (!run) { return false; }
      run.aborted = true;
      abort(run.controller);
      return true;
    }

    // ---- 直接推進劇情 -------------------------------------------------------------------------------
    /* 以固定的平台文字「推進劇情」當一般行動送出（預扣、規則層、重新生成與編輯都沿用）：啟用條件與送出行動相同，不另開確認視窗，
       **不讀取、不清除、不送出輸入框的草稿**（草稿即使剛好也是這四個字也保留）。 */
    function advance() {
      if (sending.value || advanceReason.value !== '') { return Promise.resolve(false); }
      return runTurn({ clientMsgId: deps.newId(), action: Game.ADVANCE_ACTION, resend: false, keepDraft: true });
    }

    // ---- 行動建議 -----------------------------------------------------------------------------------
    function closeSuggestions() { suggestOpen.value = false; }

    /* 取消正在進行的取得建議請求（玩家再按圖示、新的一輪開始、離開頁面）：中止請求，不顯示成錯誤。 */
    function cancelSuggestRun() {
      var run = suggestRun;
      if (!run) { return false; }
      run.canceled = true;
      suggestRun = null;
      suggestBusy.value = false;
      abort(run.controller);
      return true;
    }

    /* 取得失敗之後：登入過期、閘門與完結照一般處理，其餘只顯示固定說明（圖示仍可再按）；需要重讀的（過期、第 0 幕未完成、回合進行中）重讀歷史與快照。 */
    function suggestFailed(failed) {
      noteFailureInfo(failed);
      var failure = Game.describeSuggestionFailure(failed);
      if (failure.silent) { return Promise.resolve(false); }
      if (failure.kind === 'login') { goLogin(); return Promise.resolve(false); }
      if (failure.kind === 'gate') { gate.value = failure.code || 'forbidden'; return Promise.resolve(false); }
      if (failure.kind === 'ended') { ended.value = true; }
      if (failure.kind === 'not_found') { phase.value = 'gone'; }
      suggestError.value = failure.message;
      if (failure.reload || failure.catchUp) { return refreshAfterRevision(undefined).then(function () { syncReader(); return false; }); }
      return Promise.resolve(false);
    }

    function requestSuggestions() {
      var run = { controller: newController(), canceled: false };
      suggestRun = run;
      suggestBusy.value = true;
      return new Promise(function (resolve) {
        resolve(api.suggestActions(sessionId, { timeout: suggestTimeout, signal: signalOf(run.controller) }));
      }).then(function (data) {
        if (run.canceled || suggestRun !== run) { return false; }
        var items = Game.suggestionItems(isObject(data) ? data.suggestions : null);
        if (items.length === 0) { suggestError.value = Game.suggestText('unavailable'); return false; }       // 沒有任何可用的項目：當成失敗（伺服器本來就不會回空的）
        savedSuggestions.value = items;
        suggestItems.value = items.slice();
        suggestOpen.value = true;
        return true;
      }, function (failed) {
        if (run.canceled || suggestRun !== run) { return false; }
        return suggestFailed(failed);
      }).finally(function () {
        if (suggestRun === run) { suggestRun = null; suggestBusy.value = false; }
      });
    }

    /* 建議圖示：展開中就收回；取得中就取消；否則在可以送出行動時展開——快照裡已保存的建議直接顯示（不呼叫端點），沒有才呼叫端點。不可用時什麼都不做、不送請求。
       回傳 Promise<是否展開了建議>，永不拒絕。 */
    function toggleSuggestions() {
      if (suggestOpen.value) { closeSuggestions(); return Promise.resolve(false); }
      if (suggestBusy.value) { cancelSuggestRun(); return Promise.resolve(false); }
      if (suggestReason.value !== '') { return Promise.resolve(false); }
      suggestError.value = '';
      if (savedSuggestions.value.length > 0) {
        suggestItems.value = savedSuggestions.value.slice();
        suggestOpen.value = true;
        return Promise.resolve(true);
      }
      return requestSuggestions();
    }

    /* 點選一個建議：把文字貼進輸入框（取代原有內容），**不送出**，並收回成圖示；掛載層監看 `focusTick`，把焦點放到輸入框、游標移到尾端。只認目前顯示的建議。 */
    function pickSuggestion(value) {
      if (typeof value !== 'string' || suggestItems.value.indexOf(value) < 0) { return false; }
      draft.value = value;
      actionError.value = '';
      suggestOpen.value = false;
      focusTick.value += 1;
      return true;
    }

    // ---- 翻頁與鍵盤 ---------------------------------------------------------------------------------
    /* 點擊劇情卡、空白鍵、Enter 與向右鍵：打字中先補完該段，其次翻到下一段；最後一段不動作（回傳 completed｜advanced｜waiting｜end｜none）。 */
    function nextPage() { return reader.next(); }
    /* `‹` 與向左鍵：回到同一輪的上一段（第一段無作用）；已讀過的段直接完整顯示（回傳 back｜none）。 */
    function prevPage() { return reader.prev(); }

    /* 鍵盤翻頁。`event` 只讀 `key`、`target`、修飾鍵、`repeat`、`isComposing`、`defaultPrevented`，處理時呼叫 `preventDefault()`（避免空白鍵捲動頁面）。
       焦點在輸入、按鈕、連結等互動元素或可編輯元素時、有開啟的 Modal 時、按住不放時、帶 Ctrl／Alt／Meta 時都不作用；頁面還沒載入完成也不作用。回傳是否處理了這個按鍵。 */
    function handleKey(event) {
      if (!isObject(event) || event.defaultPrevented === true || event.isComposing === true || event.repeat === true) { return false; }
      if (event.ctrlKey === true || event.altKey === true || event.metaKey === true) { return false; }
      var action = Object.prototype.hasOwnProperty.call(KEY_ACTIONS, event.key) ? KEY_ACTIONS[event.key] : '';
      if (!action || phase.value !== 'ready' || !card.value.visible) { return false; }
      if (typeof deps.isModalOpen === 'function' && deps.isModalOpen()) { return false; }
      var target = isObject(event.target) ? event.target : null;
      if (target) {
        var tag = typeof target.tagName === 'string' ? target.tagName.toUpperCase() : '';
        if (INTERACTIVE_TAGS[tag] === true || target.isContentEditable === true) { return false; }
      }
      if (action === 'next') { nextPage(); } else { prevPage(); }
      if (typeof event.preventDefault === 'function') { event.preventDefault(); }
      return true;
    }

    /* 離開頁面（pagehide）：中止所有進行中的請求與計時器（含取得建議）。 */
    function dispose() {
      abort(bootController);
      if (currentRun) { abort(currentRun.controller); }
      if (openingRun) { abort(openingRun.controller); }
      cancelSuggestRun();
      clearPoll();
    }

    /* 修訂結束之後（不論結果）：從最後一輪的前一個序號起重讀回合並覆寫快取，再重讀快照（世界狀態、是否完結、`last_turn`）。永不拋出；
       回傳快照有沒有讀到（讀不到時 `lastTurn` 仍是舊的，呼叫端不可據此判斷後端的 revision）。 */
    function refreshAfterRevision(fromSeq) {
      return sync.catchUp(sessionId, { fromSeq: fromSeq }).then(function (found) {
        applyCatchUp(found);
        return refreshSnapshot();
      }).then(function (snapshot) { return !!snapshot; }, function () { return false; });
    }

    /* 交給 game-revision.js 的把手：它自己管 Modal 與串流流程，這裡只開放它需要讀寫的狀態與幾個既有的動作（不含任何玩家文字的處理）。 */
    /* 劇情卡交給 game-revision.js 的把手（rpg-story-view）：修訂開始時 `begin`（劇情卡改顯示這一輪的行動，從第一段重新讀起；同時收回建議）、串流事件 `feed`／`note`／`finish`，
       失敗或被擋時 `restore` 還原成修訂前的畫面與所在的段，結束後 `sync` 對齊後端。`save` 的結果只放在記憶體。 */
    var storyBridge = { begin: beginStory, feed: feedStory, finish: finishStory, note: noteStory, save: saveStory, restore: restoreStory, sync: syncReader };

    var revisionHost = {
      sessionId: sessionId, lastTurn: lastTurn, history: history, phase: phase, historyReady: historyReady, openingReady: openingReady, ended: ended, offline: offline,
      gate: gate, sending: sending, inflight: inflight, revising: revising, overlay: revisionOverlay, notice: notice, card: card, story: storyBridge,
      applyEffects: applyEffects, showFailure: showFailure, refresh: refreshAfterRevision, notifyContent: notifyContent, dismissNotice: dismissNotice
    };

    return {
      revisionHost: revisionHost, lastTurn: lastTurn, revising: revising,
      card: card, reading: reading, nextPage: nextPage, prevPage: prevPage, handleKey: handleKey,
      advance: advance, advanceDisabled: advanceDisabled, advanceHint: advanceHint,
      suggestOpen: suggestOpen, suggestItems: suggestItems, suggestBusy: suggestBusy, suggestError: suggestError, suggestDisabled: suggestDisabled, suggestHint: suggestHint,
      toggleSuggestions: toggleSuggestions, closeSuggestions: closeSuggestions, pickSuggestion: pickSuggestion, focusTick: focusTick,
      phase: phase, loadFailure: loadFailure, restriction: restriction, title: title, playerName: playerName, modeLabel: modeLabel, modeNote: modeNote,
      gateNotice: gateNotice, ended: ended, offline: offline, notice: notice, safeHint: safeHint, openingNotice: openingNotice,
      items: items, hasEarlier: hasEarlier, jumpVisible: jumpVisible, scrollTick: scrollTick,
      draft: draft, actionError: actionError, draftCount: draftCount, draftOver: draftOver, actionMax: actionMax, sending: sending,
      sendDisabled: sendDisabled, inputDisabled: inputDisabled, blockerText: blockerText,
      world: world, locationLine: locationLine, inflight: inflight, notebookSummary: notebookSummary,
      load: load, retryLoad: retryLoad, retryOpening: retryOpening, showEarlier: showEarlier, jumpToLatest: jumpToLatest, onScroll: onScroll,
      send: send, cancel: cancel, dismissNotice: dismissNotice, dismissSafeHint: dismissSafeHint, resendInflight: resendInflight, abandonInflight: abandonInflight,
      setOnline: setOnline, dispose: dispose
    };
  }

  return { createSession: createSession };
});
