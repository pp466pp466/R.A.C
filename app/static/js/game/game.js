/* 遊戲主畫面 `/game/<存檔識別碼>` 的 Vue 掛載與接線（Vue 3 全域變數模式；見 rpg-game-screen、rpg-stream-client、rpg-game-sync、rpg-wallet-ui 規格與 rpg-game-ui 的 design D6、D7）。
 *
 * 這是唯一直接碰 `window`、`document` 的遊戲腳本：邏輯都在注入依賴的模組裡（game-state.js 的純函式、game-store.js／game-sync.js 的本機暫存與補齊、
 * turn-stream.js 的串流客戶端、game-wallet.js、game-notebook.js、game-session.js），這裡只負責建立它們、把依賴接上，並提供下面這些只屬於畫面的東西：
 * - **側欄**（分頁「世界」「記事簿」）：桌面與手機**都預設收合**，頂列的開關按鈕切換。桌面（寬度 992px 以上）開啟時推擠本文區（`push`），手機與平板（未滿 992px）以覆蓋方式開啟並附遮罩（`overlay`），
 *   Esc 與遮罩都能關閉。開關與分頁由 Vue 的響應式變數接管，**不記住**（不寫任何瀏覽器儲存，每次回到收合與「世界」）；沒有 Bootstrap 的 JavaScript，也沒有 data-bs-*。
 *   記事簿的全文只在側欄於「記事簿」分頁開啟（或切到該分頁）時才讀，載入頁面時不請求。
 * - 自製 Modal 的狀態（離開確認、放棄在途回合）與 Toast；遊戲幣 Modal 的狀態在 game-wallet.js，記事簿「放棄未儲存變更」的確認在 game-notebook.js，
 *   重新生成與編輯最後一輪的兩個 Modal、按鈕與串流流程在 game-revision.js（修訂進行中離開頁面與送出新行動的互斥也在這裡接線）。
 * - 捲動：`session.scrollTick` 變動時把時間軸捲到底（是否捲動由 session 依讀者的位置決定）；`pagehide` 時中止所有進行中的請求。
 * - 本機暫存在背景開啟（IndexedDB，失敗退回記憶體）；開好之前 session 的讀寫排在它後面，所以載入一開始就可以進行。
 * 所有玩家與作者的文字都由樣板以 {{ }} 插值顯示（純文字），這裡不輸出到主控台、不放網址、不寫瀏覽器儲存。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var Game = window.Game;
  var Auth = window.Auth;
  var UI = window.UI;
  var state = Auth.readInitialState(document);
  var user = state.user && typeof state.user === 'object' ? state.user : {};
  var HOME_PATH = '/game';
  var DESKTOP_MIN_WIDTH = 992;           // 與客服的 Sidebar 同一個斷點
  var STORE_METHODS = ['putSession', 'getSession', 'putTurns', 'turnsOf', 'maxSeq', 'setInflight', 'getInflight', 'clearInflight', 'clearSession'];

  /* 朗讀（rpg-story-tts）：瀏覽器的語音合成與發音物件；存取丟例外（隱私模式、被停用）視為不存在，頁面照常。 */
  function browserGlobal(name) {
    try { var value = window[name]; return value === null || value === undefined ? undefined : value; } catch (error) { return undefined; }
  }
  /* 朗讀只存玩家所選的語音（單一鍵 rpg.speech.voice；內容是 {"mode":…} 與語音名稱，沒有任何朗讀的文字）。讀寫失敗（隱私模式、容量、被停用）一律退回預設值，
     這是本頁唯一碰 localStorage 的地方（沿用專案的慣例：讀寫都包 try/catch）。 */
  var speechStorage = {
    getItem: function (key) { try { return window.localStorage.getItem(key); } catch (error) { return null; } },
    setItem: function (key, value) { try { window.localStorage.setItem(key, value); return true; } catch (error) { return false; } }
  };

  function newId() {
    var c = window.crypto;
    if (c && typeof c.randomUUID === 'function') { return c.randomUUID(); }
    var bytes = new Uint8Array(16);
    if (c && c.getRandomValues) { c.getRandomValues(bytes); } else { for (var i = 0; i < 16; i++) { bytes[i] = Math.floor(Math.random() * 256); } }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
  }

  /* 本機暫存是非同步開啟的；先給 session 一個「排在開啟之後」的代理，所以不必等它就能建立畫面。 */
  function deferredStore(opening) {
    var proxy = {};
    STORE_METHODS.forEach(function (name) {
      proxy[name] = function () {
        var args = arguments;
        return opening.then(function (store) { return store[name].apply(store, args); });
      };
    });
    return proxy;
  }

  var app = Vue.createApp({
    setup: function () {
      function goLogin() { window.location.assign(Auth.loginUrlFor(window.location.pathname)); }

      // ---- 依賴與模組 ---------------------------------------------------------------------------------
      var opening = Game.store.openStore({ name: Game.store.databaseName(user.public_id) });   // 識別碼不合格時只用記憶體（不共用資料庫）
      var store = deferredStore(opening);
      var sync = Game.sync.createSync({ api: Game.api, store: store });
      var stream = Game.createTurnStream({
        fetch: window.fetch.bind(window), setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
        AbortController: window.AbortController, TextDecoder: window.TextDecoder, describeHttpFailure: Game.describeHttpFailure
      });
      var noteRestriction = null;                                          // 錢包的失敗要交給 session 辨識「被鎖定」之類的受限狀態；session 建好之後才接上
      var wallet = Game.createWallet({
        Vue: Vue, api: Game.api, store: UI.wallet, toast: UI.toast, goLogin: goLogin,          // UI.wallet：全站共用的錢包狀態庫，頂列的餘額標籤讀同一份（整頁只有一個錢包請求）
        noteFailure: function (info) { if (noteRestriction) { noteRestriction(info); } }
      });
      var session = Game.createSession({
        Vue: Vue, state: state, api: Game.api, store: store, sync: sync, stream: stream, wallet: wallet, TypewriterBuffer: UI.TypewriterBuffer,
        toast: UI.toast, goLogin: goLogin, newId: newId, now: function () { return Date.now(); },
        csrf: function () { return Auth.csrfTokenFrom(document); },
        isOnline: function () { return window.navigator.onLine !== false; },
        setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window), AbortController: window.AbortController,
        restrictionOf: UI.restrictionOf,
        speechSynthesis: browserGlobal('speechSynthesis'), SpeechSynthesisUtterance: browserGlobal('SpeechSynthesisUtterance'), speechStorage: speechStorage,
        isModalOpen: function () { return anyModalOpen(); }                // 有 Modal 開啟時，鍵盤翻頁不作用
      });
      noteRestriction = function (info) {
        var kind = UI.restrictionOf(info);
        if (kind) { session.restriction.value = kind; }
      };
      var revision = Game.createRevision({
        Vue: Vue, state: state, host: session.revisionHost, wallet: wallet, stream: stream, store: store, TypewriterBuffer: UI.TypewriterBuffer,
        toast: UI.toast, goLogin: goLogin, csrf: function () { return Auth.csrfTokenFrom(document); }, now: function () { return Date.now(); },
        AbortController: window.AbortController
      });
      var notebook = Game.createNotebook({
        Vue: Vue, api: Game.api, sessionId: state.session && typeof state.session.id === 'string' ? state.session.id : '',
        summary: session.notebookSummary, ended: session.ended, onEnded: function () { session.ended.value = true; },
        toast: UI.toast, goLogin: goLogin, noteFailure: noteRestriction, AbortController: window.AbortController
      });

      // ---- 側欄 -------------------------------------------------------------------------------------
      var sidebarOpen = Vue.ref(false);                                    // 桌面與手機都預設收合
      var sideTab = Vue.ref('world');                                      // 'world'｜'notebook'；不記住（每次回到「世界」）
      var viewportWidth = Vue.ref(window.innerWidth);
      var sidebarMode = Vue.computed(function () { return viewportWidth.value >= DESKTOP_MIN_WIDTH ? 'push' : 'overlay'; });
      /* 記事簿面板每次「開啟」都讀一次最新的全文（側欄在記事簿分頁開啟，或切到記事簿分頁）；全文不在載入頁面時請求。 */
      function toggleSidebar() {
        sidebarOpen.value = !sidebarOpen.value;
        if (sidebarOpen.value && sideTab.value === 'notebook') { notebook.open(); }
        return sidebarOpen.value;
      }
      function closeSidebar() { sidebarOpen.value = false; }
      function selectSideTab(tab) {
        if (tab !== 'world' && tab !== 'notebook') { return false; }
        sideTab.value = tab;
        if (tab === 'notebook' && sidebarOpen.value) { notebook.open(); }
        return true;
      }
      /* 遊戲頁上「記事簿已滿」的入口：直接開到記事簿分頁。 */
      function openNotebook() {
        sideTab.value = 'notebook';
        sidebarOpen.value = true;
        notebook.open();
        return true;
      }

      // ---- Modal ------------------------------------------------------------------------------------
      var modal = Vue.reactive({ leave: false, abandon: false, history: false, voice: session.voiceMenuOpen });   // voice 與 session 的語音選單是同一個狀態（ref 放進 reactive 會自動解開）
      function anyModalOpen() {
        return modal.leave || modal.abandon || modal.history || modal.voice || wallet.coinModalOpen.value || notebook.notebookDiscardOpen.value || revision.regenerateOpen.value || revision.editOpen.value;
      }

      /* 頂列的「回到冒險」：這一輪還在進行時先問；其餘照一般連結離開。 */
      function leaveClick(event) {
        if (!session.sending.value && !revision.revisionBusy.value) { return false; }                // 一輪或一次修訂還在進行：先問
        if (event && typeof event.preventDefault === 'function') { event.preventDefault(); }
        modal.leave = true;
        return true;
      }
      function confirmLeave() {
        modal.leave = false;
        session.dispose();
        revision.dispose();
        window.location.assign(HOME_PATH);
        return true;
      }
      function askAbandon() {
        if (session.inflight.value && !session.sending.value) { modal.abandon = true; return true; }
        return false;
      }
      function confirmAbandon() {
        modal.abandon = false;
        return session.abandonInflight();
      }

      /* 歷史：完整的時間軸（純文字、只讀）放在 Modal 裡；開啟時捲到最新的內容。 */
      function openHistory() {
        modal.history = true;
        Vue.nextTick(function () {
          var element = scroller.value;
          if (element) { element.scrollTop = element.scrollHeight; }
        });
        return true;
      }
      function closeHistory() { modal.history = false; return true; }

      // ---- 元素參照與捲動 -----------------------------------------------------------------------------
      var scroller = Vue.ref(null);
      var input = Vue.ref(null);
      /* 貼上建議之後：焦點放到輸入框、游標移到尾端（建議只貼進輸入框，不送出）。 */
      Vue.watch(function () { return session.focusTick.value; }, function () {
        Vue.nextTick(function () {
          var element = input.value;
          if (!element || typeof element.focus !== 'function') { return; }
          element.focus();
          if (typeof element.setSelectionRange === 'function') { var end = element.value.length; element.setSelectionRange(end, end); }
        });
      });
      /* 語音選單（rpg-story-tts）：開啟時焦點落在目前選中的選項（沒有選中的落在第一個，也就是「自動」）；關閉之後（選了語音、Esc、遮罩、關閉按鈕）
         焦點還給選單按鈕（與建議貼上後的 focusTick 同一種做法）。ui-modal 自己會在開啟後把焦點放到主要按鈕，所以這裡等它做完再移。 */
      var voiceMenuButton = Vue.ref(null);
      var voiceList = Vue.ref(null);
      Vue.watch(function () { return session.voiceMenuOpen.value; }, function (open, was) {
        if (open) {
          Vue.nextTick(function () {
            Vue.nextTick(function () {
              var list = voiceList.value;
              var target = list && typeof list.querySelector === 'function' ? (list.querySelector('input:checked') || list.querySelector('input')) : null;
              if (target && typeof target.focus === 'function') { target.focus(); }
            });
          });
        } else if (was) {
          Vue.nextTick(function () {
            var element = voiceMenuButton.value;
            if (element && typeof element.focus === 'function') { element.focus(); }
          });
        }
      });
      Vue.watch(function () { return session.scrollTick.value; }, function () {
        Vue.nextTick(function () {
          var element = scroller.value;
          if (element) { element.scrollTop = element.scrollHeight; }
        });
      });

      // ---- 瀏覽器事件 ---------------------------------------------------------------------------------
      window.addEventListener('resize', function () { viewportWidth.value = window.innerWidth; });
      window.addEventListener('online', function () { session.setOnline(true); });
      window.addEventListener('offline', function () { session.setOnline(false); });
      window.addEventListener('pagehide', function () { session.dispose(); revision.dispose(); notebook.dispose(); });   // 離開頁面：中止所有進行中的請求（記事簿不會自動儲存）
      /* 編輯記事簿的分頁回到前景：另一個分頁或裝置可能已經儲存，先檢查版本（沒有在編輯就什麼都不做）。 */
      function checkNotebookFresh() { notebook.checkFresh(); }
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState !== 'hidden') { checkNotebookFresh(); }
      });
      window.addEventListener('focus', checkNotebookFresh);
      document.addEventListener('keydown', function (event) {
        if (event && event.key === 'Escape' && sidebarOpen.value && !anyModalOpen()) { sidebarOpen.value = false; }
      });
      document.addEventListener('keydown', function (event) { session.handleKey(event); });   // 空白鍵、Enter、方向鍵翻頁（焦點在輸入框或有 Modal 時不作用）
      Vue.onMounted(function () { session.load(); });

      return {
        phase: session.phase, loadFailure: session.loadFailure, restriction: session.restriction, title: session.title, playerName: session.playerName,
        modeLabel: session.modeLabel, modeNote: session.modeNote, gateNotice: session.gateNotice, ended: session.ended, offline: session.offline,
        notice: session.notice, safeHint: session.safeHint, openingNotice: session.openingNotice,
        items: session.items, hasEarlier: session.hasEarlier, scroller: scroller,
        card: session.card, nextPage: session.nextPage, prevPage: session.prevPage, advance: session.advance, advanceDisabled: session.advanceDisabled,
        advanceHint: session.advanceHint, suggestOpen: session.suggestOpen, suggestItems: session.suggestItems, suggestBusy: session.suggestBusy,
        suggestError: session.suggestError, suggestDisabled: session.suggestDisabled, suggestHint: session.suggestHint,
        toggleSuggestions: session.toggleSuggestions, pickSuggestion: session.pickSuggestion, openHistory: openHistory, closeHistory: closeHistory,
        input: input, draft: session.draft, actionError: session.actionError, draftCount: session.draftCount, draftOver: session.draftOver,
        actionMax: session.actionMax, sending: session.sending, sendDisabled: session.sendDisabled, inputDisabled: session.inputDisabled,
        blockerText: session.blockerText,
        speech: session.speech, speakDisabled: session.speakDisabled, speakHint: session.speakHint, speakCurrent: session.speakCurrent,
        openVoiceMenu: session.openVoiceMenu, closeVoiceMenu: session.closeVoiceMenu, chooseAutoVoice: session.chooseAutoVoice, chooseVoiceByKey: session.chooseVoiceByKey,
        previewVoice: session.previewVoice,
        voiceMenuButton: voiceMenuButton, voiceList: voiceList,
        world: session.world, locationLine: session.locationLine, lowBalanceText: wallet.lowBalanceText,
        sidebarOpen: sidebarOpen, sidebarMode: sidebarMode, modal: modal,
        coinModalOpen: wallet.coinModalOpen, coinModalText: wallet.coinModalText, checkinBusy: wallet.checkinBusy, checkinPreview: wallet.checkinPreview,
        checkinMessage: wallet.checkinMessage, checkinFinished: wallet.checkinFinished,
        retryLoad: session.retryLoad, retryOpening: session.retryOpening, showEarlier: session.showEarlier,
        onScroll: session.onScroll, send: session.send, cancel: session.cancel, dismissNotice: session.dismissNotice, dismissSafeHint: session.dismissSafeHint,
        resendInflight: session.resendInflight, askAbandon: askAbandon, confirmAbandon: confirmAbandon, leaveClick: leaveClick,
        confirmLeave: confirmLeave, toggleSidebar: toggleSidebar, closeSidebar: closeSidebar, claimCheckin: wallet.claimCheckin,
        confirmOverflow: wallet.confirmOverflow, cancelOverflow: wallet.cancelOverflow, closeCoinModal: wallet.closeCoinModal,
        sideTab: sideTab, selectSideTab: selectSideTab, openNotebook: openNotebook, reloadNotebook: notebook.open,
        notebook: notebook.notebook, notebookLoading: notebook.notebookLoading, notebookError: notebook.notebookError, notebookEmpty: notebook.notebookEmpty,
        notebookUsage: notebook.notebookUsage, notebookReserveNote: notebook.notebookReserveNote, notebookFull: notebook.notebookFull,
        notebookFullText: notebook.notebookFullText, notebookEmptyText: notebook.notebookEmptyText, notebookStale: notebook.notebookStale,
        notebookEditing: notebook.notebookEditing, notebookDraft: notebook.notebookDraft, notebookDraftCount: notebook.notebookDraftCount,
        notebookOver: notebook.notebookOver, notebookOverNote: notebook.notebookOverNote, notebookEditHint: notebook.notebookEditHint, notebookSaveHint: notebook.notebookSaveHint,
        notebookCanEdit: notebook.notebookCanEdit, notebookCanSave: notebook.notebookCanSave, notebookLocked: notebook.notebookLocked,
        notebookSaving: notebook.notebookSaving, notebookNotice: notebook.notebookNotice, notebookConflict: notebook.notebookConflict,
        notebookDiscardOpen: notebook.notebookDiscardOpen, startNotebookEdit: notebook.startEdit, saveNotebook: notebook.save,
        cancelNotebookEdit: notebook.cancelEdit, confirmNotebookDiscard: notebook.confirmDiscard,
        revisionBusy: revision.revisionBusy, reviseKey: revision.reviseKey, reviseDisabled: revision.reviseDisabled, reviseHint: revision.reviseHint,
        regenerateNote: revision.regenerateNote, editNote: revision.editNote, regenerateOpen: revision.regenerateOpen, regenerateOptions: revision.regenerateOptions,
        regenerateCategory: revision.regenerateCategory, regenerateReason: revision.regenerateReason, regenerateRule: revision.regenerateRule,
        regenerateCount: revision.regenerateCount, regenerateError: revision.regenerateError, regenerateCanSubmit: revision.regenerateCanSubmit,
        regenerateCostLine: revision.regenerateCostLine, editOpen: revision.editOpen, editDraft: revision.editDraft, editCount: revision.editCount,
        editHint: revision.editHint, editError: revision.editError, editCanSubmit: revision.editCanSubmit, editInsufficient: revision.editInsufficient,
        editCostLine: revision.editCostLine, openRegenerate: revision.openRegenerate, openEdit: revision.openEdit, closeRegenerate: revision.closeRegenerate,
        closeEdit: revision.closeEdit, selectRegenerateCategory: revision.selectRegenerateCategory, submitRegenerate: revision.submitRegenerate,
        submitEdit: revision.submitEdit, cancelRevision: revision.cancelRevision
      };
    }
  });
  UI.install(app);
  app.mount('#game-app');
})();
