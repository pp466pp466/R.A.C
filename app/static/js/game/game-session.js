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
 * - 這裡**不記錄、不輸出任何內容**（行動、劇情、第 0 幕都是玩家文字）；錯誤訊息只來自 game-state.js／game-labels.js 的固定文字。
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
    root.Game.createSession = api.createSession;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var RENDER_WINDOW = 60;                // 時間軸只渲染最近這麼多項，其餘以「顯示更早的內容」展開
  var BOTTOM_THRESHOLD = 80;             // 距離底部不超過這麼多像素算「在底部附近」，新內容才自動捲動
  var OPENING_POLL_MS = 5000;            // 另一個裝置正在生成第 0 幕時，重讀快照的間隔
  var OPENING_POLL_MAX = 24;             // 最多重讀幾次（約 2 分鐘），之後提供重試按鈕
  var OPENING_STATES = { pending: true, generating: true, ready: true, fallback: true };
  var BLOCKER_TEXTS = {
    gate: '目前無法送出行動，請見上方說明。',
    ended: '存檔已完結，無法再送出新的行動。',
    offline: '目前離線，需要連線才能送出行動。',
    opening: '故事的開場還沒有準備好，完成後才能輸入行動。',
    inflight: '有一個行動還沒有確認完成，請先在時間軸裡選擇「重送」或「放棄」。'
  };
  var MINOR_NOTE = '未滿 18 歲固定使用安全模式，這個存檔的內容會依安全模式呈現；滿 18 歲後這項限制會自動解除。';

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
    var openingNotice = ref(null);         // { tone, text, retry }｜null
    var openingScene = ref({ state: null, text: '' });
    var history = ref([]);                 // 後端的回合（顯示用；進行中的回合沒有文字）
    var inflight = ref(null);              // { client_msg_id, action, created_at }｜null
    var world = ref(Game.worldView(null));
    var liveTurn = ref(null);              // 目前串流中的回合：{ key, clientMsgId, action, story, streaming, seq }
    var turnNotes = reactive({});          // client_msg_id -> 轉場文字（顯示在該回合的行動與劇情之間）
    var draft = ref('');
    var actionError = ref('');
    var sending = ref(false);
    var historyReady = ref(false);         // 歷史與在途回合都確認過了（確認之前不能送出：否則可能漏看「有一筆還沒完成的行動」）
    var visibleCount = ref(RENDER_WINDOW);
    var unread = ref(false);               // 玩家不在底部時有新內容到達：顯示「回到最新」
    var scrollTick = ref(0);               // game.js 監看它來捲到底部

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

    // ---- 計算 ---------------------------------------------------------------------------------------
    var tierLabel = computed(function () { return Game.tierLabel(contentTier.value); });
    var modeNote = computed(function () { return mode.is_adult === false ? MINOR_NOTE : ''; });
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

    var blockerReason = computed(function () {
      if (phase.value !== 'ready' || !historyReady.value) { return 'loading'; }
      return Game.sendBlocker({
        gate: gate.value, ended: ended.value, offline: offline.value, openingReady: openingReady.value,
        inflight: sending.value ? null : inflight.value, busy: sending.value
      });
    });
    var sendDisabled = computed(function () { return blockerReason.value !== ''; });
    var blockerText = computed(function () {
      var reason = blockerReason.value;
      return Object.prototype.hasOwnProperty.call(BLOCKER_TEXTS, reason) ? BLOCKER_TEXTS[reason] : '';
    });
    var inputDisabled = computed(function () { return !!gate.value || ended.value || phase.value !== 'ready' || !openingReady.value; });
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

    function makeTypewriter(onText, onDone) {
      return new deps.TypewriterBuffer({ onUpdate: function (value) { onText(value); notifyContent(false); }, onDone: onDone });
    }

    // ---- 快照與歷史 ---------------------------------------------------------------------------------
    /* 第 0 幕的狀態與文字。打字機還沒打完時把後端的內容先放著，打完再換上（不閃爍、不截斷）。 */
    function setOpening(opening) {
      if (!isObject(opening) || typeof opening.state !== 'string' || !OPENING_STATES[opening.state]) { return; }
      var next = { state: opening.state, text: text(opening.text) };
      if (openingTyping && !openingTyping.typed) { openingTyping.deferred = next; return; }
      openingScene.value = next;
      if (next.state === 'ready' || next.state === 'fallback') { openingNotice.value = null; clearPoll(); }
    }

    function applySnapshot(snapshot) {
      if (typeof snapshot.title === 'string' && snapshot.title) { title.value = snapshot.title; }
      if (typeof snapshot.content_tier === 'string') { contentTier.value = snapshot.content_tier; }
      ended.value = snapshot.ended === true;
      world.value = Game.worldView(snapshot.world);
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
        notifyContent(true);
        return true;
      }, function () {
        failLoad('無法讀取本機暫存的內容，請連上網路後重試。', null, true);
        return false;
      });
    }

    /* 載入完成後的歷史補齊與在途回合的確認（不依賴第 0 幕）。 */
    function loadHistory(signal) {
      return sync.catchUp(sessionId, { signal: signal }).then(function (result) {
        applyCatchUp(result);
        return sync.resolveInflight(sessionId, { signal: signal, result: result }).then(function (record) { inflight.value = record; });
      }).then(function () { historyReady.value = true; });
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

      var adapter = {
        meta: noop,
        push: function (value) { typewriter.push(value); },
        discard: function () {
          typewriter.cancel();
          openingText('');
          typewriter = makeTypewriter(openingText, function () { typing.typed = true; if (openingTyping === typing) { applyDeferred(); } });
        },
        transition: function (line) { openingNotice.value = { tone: 'info', text: line, retry: false }; },
        finish: function () { typewriter.finish(); },
        done: function (effect) {
          openingScene.value = { state: effect.openingState || 'ready', text: openingScene.value.text };
          openingNotice.value = null;
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
        return afterOpeningFailed(result);
      }).finally(function () { openingRun = null; });
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
      if (live === context && context.typed && context.settled) { liveTurn.value = null; live = null; }
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
      live = { id: id, typewriter: null, typed: false, settled: false, raw: '', reload: false };
      live.typewriter = bubbleTypewriter(live);
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
        push: function (value) { context.raw += value; context.typewriter.push(value); },
        discard: function () {
          context.typewriter.cancel();
          context.raw = '';
          if (liveTurn.value) { liveTurn.value.story = ''; }
          context.typewriter = bubbleTypewriter(context);
        },
        transition: function (line, willRetry) {
          addNote(context.id, line);
          if (liveTurn.value) { liveTurn.value.streaming = willRetry === true; }
        },
        finish: function () { context.typewriter.finish(); if (liveTurn.value) { liveTurn.value.streaming = false; } },
        hint: function (line) { notice.value = { tone: 'info', text: line, link: null }; },
        done: function (effect) {
          if (effect.ended) { ended.value = true; }
          if (liveTurn.value) { liveTurn.value.streaming = false; }
          if (effect.status === 'complete' || effect.status === 'ended') {
            commitLocal(context, effect);
            if (draft.value.replace(/^\s+|\s+$/g, '') === run.action) { draft.value = ''; }      // 成功才清空；被擋時保留，玩家可以修改後再試
          } else {
            context.typed = true;                                                                  // 被擋：沒有文字要打
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
      });
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
        return afterRun(result, context, run);
      }).then(function () { return true; }, function () {
        // 串流客戶端永不拋出；這裡只防止程式錯誤讓畫面卡在進行中
        context.typed = true;
        context.settled = true;
        notice.value = { tone: 'error', text: '這一輪沒有順利完成，已重新整理狀態。遊戲幣以伺服器的結算為準。', link: null };
        liveTurn.value = null;
        live = null;
        return false;
      }).finally(function () {
        if (currentRun === run) { sending.value = false; currentRun = null; }                // finally：無論成功、失敗或中斷都恢復
      });
    }

    function runTurn(options) {
      if (sending.value) { return Promise.resolve(false); }                                     // 重複點擊：只有一個請求
      sending.value = true;
      notice.value = null;
      actionError.value = '';
      var run = { controller: newController(), aborted: false, action: options.action };
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
      if (!record || sending.value || offline.value || ended.value || gate.value || !openingReady.value) { return Promise.resolve(false); }
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

    /* 離開頁面（pagehide）：中止所有進行中的請求與計時器。 */
    function dispose() {
      abort(bootController);
      if (currentRun) { abort(currentRun.controller); }
      if (openingRun) { abort(openingRun.controller); }
      clearPoll();
    }

    return {
      phase: phase, loadFailure: loadFailure, restriction: restriction, title: title, playerName: playerName, tierLabel: tierLabel, modeNote: modeNote,
      gateNotice: gateNotice, ended: ended, offline: offline, notice: notice, openingNotice: openingNotice,
      items: items, hasEarlier: hasEarlier, jumpVisible: jumpVisible, scrollTick: scrollTick,
      draft: draft, actionError: actionError, draftCount: draftCount, draftOver: draftOver, actionMax: actionMax, sending: sending,
      sendDisabled: sendDisabled, inputDisabled: inputDisabled, blockerText: blockerText,
      world: world, locationLine: locationLine, inflight: inflight,
      load: load, retryLoad: retryLoad, retryOpening: retryOpening, showEarlier: showEarlier, jumpToLatest: jumpToLatest, onScroll: onScroll,
      send: send, cancel: cancel, dismissNotice: dismissNotice, resendInflight: resendInflight, abandonInflight: abandonInflight,
      setOnline: setOnline, dispose: dispose
    };
  }

  return { createSession: createSession };
});
