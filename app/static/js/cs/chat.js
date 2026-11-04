/* 客服頁面（Vue 3 全域變數模式）。
 *
 * - 後端是權威：訊息由後端先寫入並指派序號；這裡只有 IndexedDB 的快取與「待送」訊息（cs-store.js、cs-sync.js）。
 * - Sidebar、Modal、Toast 的顯示完全由 Vue 的響應式變數接管（v-show／v-if），不使用 Bootstrap 的 JavaScript。
 * - 所有文字（使用者與助理）都以 {{ }} 插值當作純文字顯示，保留換行（CSS pre-wrap）；不使用 v-html。
 * - 送出訊息的請求逾時只讀伺服器放進初始狀態的 request_timeout_ms（總預算 + 5 秒），這裡不寫死任何逾時數字。
 * - 用量：Sidebar 底部顯示今日剩餘輪數、暫停期間鎖住輸入並顯示恢復時間（資料取自 GET /api/cs/usage，邏輯在 cs-usage.js）；
 *   取得失敗就隱藏，真正的拒絕永遠在後端。對話數已滿時說明原因，輸入的文字留在輸入框。
 * - 取消、切換對話、離開頁面時以 AbortController 中止請求；無論成功、失敗或中斷，finally 都會恢復介面。
 *   前端中止不代表後端中止：後端仍會完成並寫入結果，之後以 since_seq 補齊。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var CS = window.CS;
  var state = Auth.readInitialState(document);
  var user = state.user || {};
  var TIER_LABELS = { free: '一般', plus: '進階', vip: '尊榮' };
  var GATE_CODES = { age_confirmation_required: true, age_restricted: true, forbidden: true, account_locked: true };
  var RETRY_DELAY_MS = 10000;   // 保留的訊息自動重送的間隔（不是請求逾時；請求逾時只來自初始狀態）

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

  var app = Vue.createApp({
    setup: function () {
      var maxChars = state.max_message_chars;
      var tierLabel = TIER_LABELS[user.member_tier] || '';

      var sidebar = CS.sidebar.createSidebar({ width: window.innerWidth, storage: CS.sidebar.safeStorage(window), reactive: Vue.reactive });
      CS.sidebar.watchWidth(window, function (width) { sidebar.setWidth(width); });

      var ready = Vue.ref(false);
      var storageMode = Vue.ref('indexeddb');
      var online = Vue.ref(window.navigator.onLine !== false);
      var gate = Vue.ref(state.chat_gate || null);
      var serviceDown = Vue.ref(state.service_available === false);
      var conversations = Vue.ref([]);
      var activeKey = Vue.ref(null);
      var serverMessages = Vue.ref([]);
      var pending = Vue.ref([]);
      var typing = Vue.reactive({ seq: 0, text: '' });
      var chipsUsed = Vue.ref(false);   // 點了 chip：這組 chips 立即移除（不等儲存與請求），下一則助理訊息到達時才重新計算
      var draft = Vue.ref('');
      var sending = Vue.ref(false);
      var renameOpen = Vue.ref(false);
      var renameValue = Vue.ref('');
      var renameError = Vue.ref('');
      var deleteOpen = Vue.ref(false);
      var targetKey = Vue.ref(null);
      var scroller = Vue.ref(null);
      var input = Vue.ref(null);

      // 用量（GET /api/cs/usage）：只用於顯示剩餘輪數與暫停提示；取得失敗時為 null，整行隱藏。真正的限制永遠在後端。
      var usage = Vue.ref(null);
      var nowMs = Vue.ref(Date.now());

      var store = null;
      var sync = null;
      var usageApi = null;
      var suspensionTimer = null;
      var controller = null;
      var knownSeq = 0;            // 這個對話已經「看過」的最大序號：比它大的新助理訊息才會用打字機顯示
      var typewriter = null;
      var retryTimer = null;

      // ---- 畫面資料 -------------------------------------------------------------------------

      var activeRecord = Vue.computed(function () {
        return conversations.value.filter(function (item) { return item.key === activeKey.value; })[0] || null;
      });
      var activeTitle = Vue.computed(function () { return activeRecord.value ? activeRecord.value.title : '智能客服'; });
      var targetTitle = Vue.computed(function () {
        var item = conversations.value.filter(function (record) { return record.key === targetKey.value; })[0];
        return item ? item.title : '';
      });

      var timeline = Vue.computed(function () {
        var items = serverMessages.value.map(function (message) {
          var isTyping = typing.seq === message.seq && message.role === 'assistant';
          return {
            key: 'm' + message.seq, role: message.role, text: isTyping ? typing.text : message.content, typing: isTyping,
            fallback: message.msg_status === 'fallback', pending: false
          };
        });
        pending.value.forEach(function (entry) {
          items.push({
            key: 'p' + entry.client_msg_id, id: entry.client_msg_id, role: 'user', text: entry.content, typing: false, fallback: false,
            pending: true, status: entry.status, attempts: entry.attempts, errorMessage: entry.status === 'failed' ? (entry.message || '') : ''
          });
        });
        return items;
      });

      // 建議動作：只在最後一則是帶建議動作的助理訊息、打字機結束、其後沒有任何新訊息（含待送）時顯示
      var chips = Vue.computed(function () {
        var last = serverMessages.value[serverMessages.value.length - 1];
        if (chipsUsed.value || !last || last.role !== 'assistant' || pending.value.length || typing.seq === last.seq || sending.value) { return []; }
        return (last.suggested_actions || []).slice(0, 4);
      });

      var suspension = Vue.computed(function () { return CS.usage.suspension(usage.value, nowMs.value); });
      var usageText = Vue.computed(function () { return CS.usage.remainingText(usage.value); });

      var gateNotice = Vue.computed(function () {
        if (gate.value === 'age_confirmation_required') {
          return { text: '請先到帳號設定填寫生日並選擇年齡區間，才能使用客服。', link: '/account', linkText: '前往帳號設定' };
        }
        if (gate.value === 'age_restricted') { return { text: '您的年齡目前不符合使用這項功能的規定。' }; }
        if (gate.value === 'account_locked') {                       // 使用中才被鎖定：直接給前往申訴的按鈕（appeal-shortcut）
          var locked = UI.restrictionNoticeOf('locked');
          return { text: locked.text, link: locked.href, linkText: locked.linkText, button: true };
        }
        if (gate.value === 'forbidden') {
          return { text: '您的帳號目前無法使用客服，請確認信箱已完成驗證。', link: '/account', linkText: '前往帳號設定' };
        }
        if (suspension.value) { return { text: suspension.value.text }; }
        if (serviceDown.value) { return { text: '客服暫時無法使用，請稍後再試。已輸入的文字會保留。' }; }
        return null;
      });
      var composerLocked = Vue.computed(function () { return gate.value !== null || !ready.value || suspension.value !== null; });

      // ---- 用量 -----------------------------------------------------------------------------

      /* 暫停到期的那一刻讓畫面自動恢復（不必重新整理）；暫停期間 nowMs 只在這一刻更新。 */
      function armSuspensionTimer() {
        if (suspensionTimer !== null) { window.clearTimeout(suspensionTimer); suspensionTimer = null; }
        nowMs.value = Date.now();
        var active = CS.usage.suspension(usage.value, nowMs.value);
        if (!active) { return; }
        var wait = Math.min(active.untilMs - nowMs.value + 500, 2147483647);
        suspensionTimer = window.setTimeout(function () { suspensionTimer = null; nowMs.value = Date.now(); }, wait);
      }

      function refreshUsage() {
        if (!usageApi || !online.value) { usage.value = null; return Promise.resolve(); }
        return usageApi.getUsage().then(function (data) {
          usage.value = data && data.usage ? data.usage : null;
          armSuspensionTimer();
        }, function (error) {
          usage.value = null;                                 // 取得失敗：隱藏這一行，其他功能照常
          armSuspensionTimer();
          if (error && error.status === 401) { onAuthLost(); }
        });
      }

      function scrollToEnd() {
        Vue.nextTick(function () { if (scroller.value) { scroller.value.scrollTop = scroller.value.scrollHeight; } });
      }

      function refreshConversations() {
        return store.listConversations().then(function (list) { conversations.value = list; });
      }

      function refreshMessages(animate) {
        var key = activeKey.value;
        if (!key) { serverMessages.value = []; pending.value = []; return Promise.resolve(); }
        return Promise.all([store.getMessages(key), store.listPending(key)]).then(function (both) {
          if (key !== activeKey.value) { return; }            // 期間切換了對話
          serverMessages.value = both[0];
          pending.value = both[1];
          var latest = both[0].length ? both[0][both[0].length - 1].seq : 0;
          if (animate) {
            var fresh = both[0].filter(function (m) { return m.role === 'assistant' && m.seq > knownSeq && m.msg_status === 'ok'; });
            if (fresh.length) { startTyping(fresh[fresh.length - 1]); }
          }
          if (latest > knownSeq) { chipsUsed.value = false; }     // 有新的訊息（新回覆）：重新依最後一則決定要不要顯示 chips
          knownSeq = Math.max(knownSeq, latest);
          scrollToEnd();
        });
      }

      function refreshAll(animate) {
        return refreshConversations().then(function () { return refreshMessages(animate); });
      }

      // ---- 打字機 ---------------------------------------------------------------------------

      function stopTyping() {
        if (typewriter) { typewriter.cancel(); typewriter = null; }
        typing.seq = 0;
        typing.text = '';
      }

      function startTyping(message) {
        stopTyping();
        typing.seq = message.seq;
        typing.text = '';
        typewriter = new UI.TypewriterBuffer({
          onUpdate: function (text) { typing.text = text; scrollToEnd(); },
          onDone: function () { typing.seq = 0; typing.text = ''; typewriter = null; scrollToEnd(); }
        });
        typewriter.play(message.content);
      }

      function skipTyping() { if (typewriter) { typewriter.flush(); } }

      // ---- 同步與送出 -----------------------------------------------------------------------

      function abortInflight() { if (controller) { controller.abort(); } }

      function notifyOnce(message) {
        var shown = UI.toasts.state.items.some(function (item) { return item.message === message; });
        if (!shown) { UI.toast(message, { type: 'error' }); }
      }

      function describeStop(result) {
        return store.listPending().then(function (entries) {
          var failed = entries.filter(function (entry) { return entry.status === 'failed'; });
          failed.forEach(function (entry) { if (GATE_CODES[entry.error]) { gate.value = entry.error; } });
          var queued = entries.filter(function (entry) { return entry.status === 'queued' && entry.error; })[0];
          if (result.limited) { notifyOnce(typeof result.limited === 'string' ? result.limited : '對話數已達上限，請先刪除不需要的對話。'); }
          if (result.stopped === 'network') {
            if (queued && queued.error === 'service_unavailable') {
              serviceDown.value = true;
              notifyOnce('客服暫時無法使用，訊息已保留。');
            } else if (queued && CS.usage.queuedNotice(queued.error)) {          // 每分鐘限制、全站忙碌：不是斷線，說明原因
              notifyOnce(CS.usage.queuedNotice(queued.error));
            } else {
              notifyOnce('目前無法連線到伺服器，訊息已保留，連線恢復後會自動送出。');
            }
          } else if (result.stopped === 'busy') {
            notifyOnce('上一則訊息還在處理中，這則已保留，稍後可再試。');
          } else if (result.failed > 0 && !failed.some(function (entry) { return GATE_CODES[entry.error]; })) {
            notifyOnce('有訊息無法送出，請查看訊息下方的說明。');
          }
        });
      }

      // 網路中斷、逾時或對話忙碌後：保留的訊息隔一段時間自動重送（同一個識別碼，後端以它去重，不會重複）
      function scheduleRetry() {
        if (retryTimer !== null) { return; }
        retryTimer = window.setTimeout(function () { retryTimer = null; runFlush(); }, RETRY_DELAY_MS);
      }

      function runFlush() {
        if (sending.value) { return Promise.resolve(); }
        sending.value = true;
        controller = new AbortController();
        return sync.flush({ signal: controller.signal })
          .then(function (result) {
            if (result.sent > 0) { serviceDown.value = false; }
            return refreshAll(true).then(function () { return describeStop(result); }).then(function () {
              if (result.stopped === 'network' || result.stopped === 'busy') { scheduleRetry(); }
              // 有訊息得到回覆或被拒絕：用量（剩餘輪數、暫停）可能變了，重新讀取；失敗時 refreshUsage 自己處理
              return (result.sent > 0 || result.failed > 0) ? refreshUsage() : undefined;
            });
          })
          .catch(function () {
            UI.toast('送出時發生錯誤，訊息已保留。', { type: 'error' });
            return refreshAll(false);
          })
          .finally(function () {                  // 無論成功、失敗或中斷都恢復介面
            sending.value = false;
            controller = null;
          });
      }

      function ensureConversation() {
        var record = activeRecord.value;
        if (record) { return Promise.resolve(record); }
        return sync.createConversation().then(function (created) {
          activeKey.value = created.key;
          knownSeq = 0;
          return refreshConversations().then(function () { return created; });
        });
      }

      /* 建立對話被拒絕（對話數已滿）：說明原因，輸入的文字留在輸入框。回傳是否已處理。 */
      function explainCreateRefusal(error) {
        if (!error || error.code !== 'conversation_limit_reached') { return false; }
        notifyOnce(error.message || '對話數已達上限，請先刪除不需要的對話。');
        return true;
      }

      function sendText(text) {
        var content = String(text || '').trim();
        if (!content || sending.value || composerLocked.value) { return Promise.resolve(); }
        return ensureConversation().then(function (record) {
          return sync.enqueue(record.key, content);
        }).then(function () {
          draft.value = '';
          autosize();
          return refreshMessages(false);
        }).then(runFlush, function (error) {
          if (explainCreateRefusal(error)) { return undefined; }
          throw error;
        });
      }

      function submit() { return sendText(draft.value); }
      function useChip(chip) {
        if (sending.value || composerLocked.value) { return Promise.resolve(); }
        chipsUsed.value = true;
        return sendText(chip.text);
      }

      function onEnter(event) {
        if (event.isComposing || event.keyCode === 229) { return; }     // 中文輸入法選字時的 Enter 不是送出
        event.preventDefault();
        submit();
      }

      function autosize() {
        Vue.nextTick(function () {
          var el = input.value;
          if (!el) { return; }
          el.style.height = 'auto';
          el.style.height = Math.min(el.scrollHeight, 160) + 'px';
          el.style.overflowY = el.scrollHeight > 160 ? 'auto' : 'hidden';
        });
      }

      function cancelSending() { abortInflight(); }

      function retryPending(id) {
        return sync.retryFailed(id).then(function () { return refreshMessages(false); }).then(runFlush);
      }

      function removePending(id) {
        return sync.removeFailed(id).then(function () { return refreshMessages(false); });
      }

      // ---- 對話的選擇、建立、改名與刪除 -----------------------------------------------------------

      function loadConversation(key) {
        stopTyping();
        activeKey.value = key;
        knownSeq = 0;
        return refreshMessages(false).then(function () {
          if (!online.value || !key || CS.store.isLocalConversationKey(key)) { return undefined; }
          return sync.pull(key).then(function () { return refreshAll(false); }).then(function () { return sync.flush().then(function () { return refreshAll(false); }); });
        }).catch(function () { /* 離線或暫時失敗：先顯示快取 */ });
      }

      function select(key) {
        abortInflight();
        sidebar.afterSelect();
        return loadConversation(key);
      }

      function newConversation() {
        abortInflight();
        if (activeRecord.value && serverMessages.value.length === 0 && pending.value.length === 0) { sidebar.afterSelect(); return Promise.resolve(); }
        return sync.createConversation().then(function (created) {
          sidebar.afterSelect();
          return refreshConversations().then(function () { return loadConversation(created.key); });
        }, function (error) {
          if (explainCreateRefusal(error)) { return undefined; }
          throw error;
        });
      }

      function askRename(item) {
        targetKey.value = item.key;
        renameValue.value = item.title;
        renameError.value = '';
        renameOpen.value = true;
      }

      function confirmRename() {
        var title = (renameValue.value || '').trim();
        if (!title) { renameError.value = '請輸入名稱。'; return Promise.resolve(); }
        return sync.rename(targetKey.value, title).then(function (result) {
          renameOpen.value = false;
          UI.toast(result.conflict ? '這個對話已在其他裝置被修改，已為您更新到最新的名稱。' : '已改名。');
          return refreshConversations();
        }, function (error) {
          renameError.value = error && error.fields && error.fields.title ? error.fields.title : (error && error.message) || '改名失敗，請稍後再試。';
        });
      }

      function askDelete(item) {
        targetKey.value = item.key;
        deleteOpen.value = true;
      }

      function confirmDelete() {
        var key = targetKey.value;
        return sync.deleteConversation(key).then(function () {
          UI.toast('已刪除對話。');
          return refreshConversations().then(function () {
            if (activeKey.value !== key) { return undefined; }
            var next = conversations.value[0];
            return loadConversation(next ? next.key : null);
          });
        }, function (error) {
          UI.toast((error && error.message) || '刪除失敗，請稍後再試。', { type: 'error' });
        });
      }

      // ---- Sidebar -----------------------------------------------------------------------------

      function toggleSidebar() { sidebar.toggle(); }
      function dismissSidebar() { sidebar.dismissOverlay(); }

      // ---- 啟動 ---------------------------------------------------------------------------------

      function onAuthLost() { window.location.assign(Auth.loginUrlFor('/cs')); }

      function start() {
        return CS.store.openStore({
          name: 'cs-cache-' + (user.public_id || 'anonymous'),
          onFallback: function () { storageMode.value = 'memory'; }
        }).then(function (opened) {
          store = opened;
          if (store.mode === 'memory') { storageMode.value = 'memory'; }
          var api = CS.sync.createAxiosApi(window.axios, Auth.describeError, { sendTimeoutMs: state.request_timeout_ms });
          usageApi = api;
          sync = CS.sync.createSync({ api: api, store: store, storeApi: CS.store, newId: newId, onAuthLost: onAuthLost });
          return refreshConversations();
        }).then(function () {
          ready.value = true;
          refreshUsage();                           // 不等它：剩餘輪數晚一點出現沒關係，失敗就不顯示
          var first = conversations.value[0];
          return loadConversation(first ? first.key : null).then(function () {
            if (!online.value) { return undefined; }
            return sync.refreshConversations().then(function () { return refreshConversations(); }).catch(function (error) {
              if (error && error.status === 401) { onAuthLost(); }
            });
          });
        });
      }

      window.addEventListener('online', function () {
        online.value = true;
        if (!sync) { return; }
        sync.refreshConversations().then(refreshConversations).catch(function () {});
        refreshUsage();
        runFlush();
      });
      window.addEventListener('offline', function () { online.value = false; });
      window.addEventListener('pagehide', function () {          // 離開頁面：中止等待中的請求（後端仍會完成，之後補齊）
        abortInflight();
        if (retryTimer !== null) { window.clearTimeout(retryTimer); retryTimer = null; }
        if (suspensionTimer !== null) { window.clearTimeout(suspensionTimer); suspensionTimer = null; }
      });
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') { sidebar.dismissOverlay(); }
      });

      Vue.onMounted(function () {
        start().then(autosize).catch(function () {
          ready.value = true;                       // 暫存或同步初始化失敗：仍讓頁面可用，錯誤只提示一次
          UI.toast('部分功能暫時無法使用，請重新整理頁面。', { type: 'error' });
        });
      });

      return {
        sidebar: sidebar.state, ready: ready, storageMode: storageMode, online: online, conversations: conversations,
        activeKey: activeKey, activeTitle: activeTitle, tierLabel: tierLabel, timeline: timeline, chips: chips,
        gateNotice: gateNotice, composerLocked: composerLocked, usageText: usageText, draft: draft, sending: sending, maxChars: maxChars,
        renameOpen: renameOpen, renameValue: renameValue, renameError: renameError, deleteOpen: deleteOpen,
        targetTitle: targetTitle, scroller: scroller, input: input,
        submit: submit, onEnter: onEnter, autosize: autosize, useChip: useChip, skipTyping: skipTyping,
        cancelSending: cancelSending, retryPending: retryPending, removePending: removePending,
        select: select, newConversation: newConversation, askRename: askRename, confirmRename: confirmRename,
        askDelete: askDelete, confirmDelete: confirmDelete, toggleSidebar: toggleSidebar, dismissSidebar: dismissSidebar
      };
    }
  });
  UI.install(app);
  app.mount('#cs-app');
})();
