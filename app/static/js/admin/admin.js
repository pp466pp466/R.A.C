/* 管理員後台（Vue 3 全域變數模式）。
 *
 * - 所有資料都來自 /api/admin/*；頁面本身只有管理員的顯示名稱（初始狀態）。真正的權限、重新輸入密碼與稽核都在後端，
 *   這裡只負責顯示與收集輸入，前端的任何檢查（例如原因不可空白）都只是為了少打一次請求。
 * - Modal、Toast 的顯示完全由 Vue 的響應式變數接管（v-if／v-show），不使用 Bootstrap 的 JavaScript。
 * - 所有文字（使用者、模型、管理員填寫的原因）都以 {{ }} 插值當作純文字顯示，不使用 v-html。
 * - 危險操作（停權、解除停權、調整等級、刪除對話、解除帳號鎖定）共用一個確認視窗。密碼只存在輸入框的響應式欄位：送出的當下立即清空，
 *   取消、關閉視窗也清空，不寫入 localStorage、IndexedDB 或任何其他儲存體。
 * - 「看使用者」會依對外識別碼取得使用者（寫稽核）；信箱查詢是次要入口，標明「供客服反應時使用」。沒有使用者清單。
 * - 「被鎖定的帳號」與「申訴」：申訴只有目前被鎖定的帳號送出的才看得到（解除之後從兩個清單消失），內容以純文字顯示；
 *   讀取申訴會寫稽核（只記筆數），所以和「模型失敗」一樣，進入分頁時才載入、不因切換分頁而重複讀取。
 * - 清單依分頁載入：進入分頁時才載入一次（模型失敗每次讀取都會寫稽核，所以不因切換分頁而重複讀取），
 *   「重新整理」才會重新讀取；做了會改變資料的操作之後，相關的分頁標記為過期，下次進入時重新載入。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var fmt = window.Admin.format;
  var state = Auth.readInitialState(document);
  var adminName = (state.admin && state.admin.display_name) || '';

  var PAGE_SIZE = 50;
  var MAX_REASON = 200;
  var API_PREFIX = '/api/admin';
  var TIERS = ['free', 'plus', 'vip'];
  var SUSPENDABLE = ['active'];

  var TABS = [
    { id: 'overview', label: '總覽' },
    { id: 'flagged', label: '被標記對話' },
    { id: 'feedback', label: '意見回饋' },
    { id: 'locked', label: '被鎖定的帳號' },
    { id: 'appeals', label: '申訴' },
    { id: 'users', label: '使用者' },
    { id: 'failures', label: '模型失敗' },
    { id: 'audit', label: '稽核' }
  ];

  /* 清單的來源。paged 為 false 的只讀一次（伺服器已限制筆數）。 */
  var SOURCES = {
    flagged: { path: '/flagged', key: 'conversations', paged: true },
    feedback: { path: '/feedback', key: 'conversations', paged: true },
    audit: { path: '/audit', key: 'entries', paged: true },
    failures: { path: '/failures', key: 'failures', paged: false },
    locked: { path: '/locked', key: 'accounts', paged: true },
    appeals: { path: '/appeals', key: 'appeals', paged: true }
  };

  /* 五個危險操作。needsReason：停權與刪除對話必須填原因；needsTier：調整等級要選新的等級。 */
  var CONFIRMS = {
    suspend: {
      code: 'ADMIN_SUSPEND_USER', title: '停權這位使用者？', confirmText: '確認停權', needsReason: true, needsTier: false,
      intro: '停權後，這位使用者的既有登入會立即失效，也無法再登入。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/suspend'; }, done: '已停權。'
    },
    unsuspend: {
      code: 'ADMIN_UNSUSPEND_USER', title: '解除停權？', confirmText: '確認解除', needsReason: false, needsTier: false,
      intro: '解除後，這位使用者可以再次以 Google 登入。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/unsuspend'; }, done: '已解除停權。'
    },
    tier: {
      code: 'ADMIN_CHANGE_TIER', title: '調整會員等級', confirmText: '確認調整', needsReason: false, needsTier: true,
      intro: '等級只影響額度（每日對話輪數、每分鐘訊息數、對話數上限），調整後立即生效。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/tier'; }, done: '已調整會員等級。'
    },
    unlock: {
      code: 'ADMIN_UNLOCK_ACCOUNT', title: '解除這個帳號的鎖定？', confirmText: '確認解除', needsReason: false, needsTier: false,
      intro: '解除後，這位玩家可以再次使用全部功能，目前的登入狀態立即恢復；該帳號的鎖定計次會清除，待處理的申訴會標為已處理。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/unlock'; }, done: '已解除鎖定。'
    },
    remove: {
      code: 'ADMIN_DELETE_CONVERSATION', title: '刪除這個對話？', confirmText: '確認刪除', needsReason: true, needsTier: false,
      intro: '整個對話與其中所有訊息會被永久刪除，無法復原；用量紀錄會保留。',
      path: function (id) { return '/conversations/' + encodeURIComponent(id) + '/delete'; }, done: '已刪除對話。'
    }
  };
  var NO_CONFIRM = { code: 'ADMIN_CONFIRM', title: '', confirmText: '確認', needsReason: false, needsTier: false, intro: '' };

  function onAuthLost() { window.location.assign(Auth.loginUrlFor('/admin')); }

  /* 呼叫 /api/admin/*。失敗時以 Auth.describeError 整理過的物件拒絕（{ message, fields, code, status }）；401 同時導向登入。 */
  function request(method, path, options) {
    var config = Object.assign({ method: method, url: API_PREFIX + path }, options || {});
    return window.axios.request(config).then(function (response) { return response.data; }, function (error) {
      var described = Auth.describeError(error);
      if (described.status === 401) { onAuthLost(); }
      throw described;
    });
  }

  function newLoadState() { return Vue.reactive({ loading: false, error: '', loaded: false }); }

  function newList() { return Vue.reactive({ items: [], loading: false, error: '', loaded: false, done: false }); }

  /* 清單底部：錯誤與重試、載入中、空清單的說明、載入更多。 */
  var ListFoot = {
    name: 'AdminListFoot',
    props: { list: { type: Object, required: true }, emptyText: { type: String, default: '目前沒有資料。' } },
    emits: ['retry', 'more'],
    template:
      '<div class="admin-listfoot">' +
      '  <p v-if="list.error" class="admin-error" role="alert">{{ list.error }} <button type="button" class="admin-link-btn" @click="$emit(\'retry\')">重試</button></p>' +
      '  <p v-if="list.loading" class="admin-hint" role="status">載入中…</p>' +
      '  <p v-else-if="list.loaded && !list.items.length && !list.error" class="admin-hint">{{ emptyText }}</p>' +
      '  <button v-if="list.loaded && !list.done && !list.loading" type="button" class="ui-btn" @click="$emit(\'more\')">載入更多</button>' +
      '</div>'
  };

  var app = Vue.createApp({
    setup: function () {
      var tab = Vue.ref('overview');

      // ---- 總覽 -----------------------------------------------------------------------------
      var overview = Vue.ref(null);
      var overviewState = newLoadState();

      var warningLevel = Vue.computed(function () {
        var warnings = overview.value && overview.value.warnings;
        if (!warnings) { return 'quiet'; }
        if (warnings.suspended_users > 0) { return 'critical'; }
        return warnings.blocked_calls > 0 ? 'alert' : 'quiet';
      });
      var warningHint = Vue.computed(function () {
        return warningLevel.value === 'quiet'
          ? '今日沒有警告。點此查看被標記的對話。'
          : '點此查看被標記的對話，先讀內容再決定要不要刪除或停權。';
      });
      var budgetRows = Vue.computed(function () {
        var budget = overview.value && overview.value.usage && overview.value.usage.budget;
        if (!budget) { return []; }
        return [
          { key: 'tokens', label: 'Token', used: budget.tokens.used, limit: budget.tokens.limit, ratio: budget.tokens.ratio },
          { key: 'calls', label: '模型呼叫次數', used: budget.calls.used, limit: budget.calls.limit, ratio: budget.calls.ratio }
        ];
      });
      var maxDayTokens = Vue.computed(function () {
        var days = (overview.value && overview.value.usage && overview.value.usage.days) || [];
        return days.reduce(function (max, day) { return Math.max(max, day.tokens || 0); }, 0);
      });

      function tokenWidth(day) { return maxDayTokens.value ? Math.round((day.tokens / maxDayTokens.value) * 100) : 0; }

      function levelNote(ratio) {
        var level = fmt.ratioLevel(ratio);
        if (level === 'full') { return '已達上限，客服回覆會顯示系統忙碌。'; }
        return level === 'warn' ? '已達 80% 告警線。' : '';
      }

      function fetchOverview() {
        if (overviewState.loading) { return Promise.resolve(); }
        overviewState.loading = true;
        overviewState.error = '';
        return request('get', '/overview').then(function (data) {
          overview.value = data.overview;
          overviewState.loaded = true;
        }, function (error) { overviewState.error = error.message; }).finally(function () { overviewState.loading = false; });
      }

      function reloadOverview() { overviewState.loaded = false; return fetchOverview(); }

      // ---- 清單 -----------------------------------------------------------------------------
      var lists = { flagged: newList(), feedback: newList(), audit: newList(), failures: newList(), locked: newList(), appeals: newList() };

      function fetchList(id, more) {
        var list = lists[id];
        var source = SOURCES[id];
        if (list.loading) { return Promise.resolve(); }
        if (!more) { list.items = []; list.done = false; list.loaded = false; }
        list.loading = true;
        list.error = '';
        var params = source.paged ? { limit: PAGE_SIZE, offset: more ? list.items.length : 0 } : {};
        return request('get', source.path, { params: params }).then(function (data) {
          var rows = Array.isArray(data[source.key]) ? data[source.key] : [];
          list.items = list.items.concat(rows);
          list.done = !source.paged || rows.length < PAGE_SIZE;
          list.loaded = true;
        }, function (error) { list.error = error.message; }).finally(function () { list.loading = false; });
      }

      function reload(id) { return fetchList(id, false); }
      function loadMore(id) { return fetchList(id, true); }

      /* 會改變資料的操作之後，標記相關分頁為過期：下次進入時重新載入。 */
      function markStale() {
        for (var i = 0; i < arguments.length; i++) {
          var id = arguments[i];
          if (id === 'overview') { overviewState.loaded = false; } else if (lists[id]) { lists[id].loaded = false; lists[id].items = []; lists[id].done = false; }
        }
      }

      function ensureLoaded(id) {
        if (id === 'overview') { return overviewState.loaded ? Promise.resolve() : fetchOverview(); }
        var list = lists[id];
        if (!list || list.loaded || list.loading) { return Promise.resolve(); }
        return fetchList(id, false);
      }

      function selectTab(id) {
        tab.value = id;
        window.scrollTo(0, 0);
        return ensureLoaded(id);
      }

      function suspensionNote(untilText) { return fmt.suspensionText(untilText, Date.now()); }

      // ---- 使用者 ---------------------------------------------------------------------------
      var user = Vue.ref(null);
      var lookup = Vue.reactive({ email: '', loading: false, error: '' });
      var userToken = 0;                    // 只採用最後一次查詢的結果，避免舊的回應覆蓋新的

      var userActions = Vue.computed(function () {
        var current = user.value;
        var manageable = !!current && current.role === 'user';
        return {
          suspend: manageable && SUSPENDABLE.indexOf(current.status) !== -1,
          unsuspend: manageable && current.status === 'suspended',
          tier: manageable && current.status !== 'deletion_pending'
        };
      });
      var userNote = Vue.computed(function () {
        var current = user.value;
        if (!current) { return ''; }
        if (current.role !== 'user') { return '管理員帳號無法在後台操作。'; }
        return current.status === 'deletion_pending' ? '這個帳號已申請刪除，無法操作。' : '';
      });

      function showUser(promise, token) {
        return promise.then(function (data) {
          if (token === userToken) { user.value = data.user; }
        }, function (error) {
          if (token === userToken) { lookup.error = error.message || ''; user.value = null; }
        }).finally(function () { if (token === userToken) { lookup.loading = false; } });
      }

      /* 從清單或對話進入：以對外識別碼取得使用者（寫稽核）並切到「使用者」分頁。 */
      function openUserById(publicId) {
        tab.value = 'users';
        window.scrollTo(0, 0);
        markStale('audit');
        lookup.error = '';
        lookup.loading = true;
        user.value = null;
        var token = ++userToken;
        return showUser(request('get', '/users/' + encodeURIComponent(publicId)), token);
      }

      function refreshUser() {
        if (!user.value) { return Promise.resolve(); }
        markStale('audit');
        lookup.error = '';
        lookup.loading = true;
        var token = ++userToken;
        return showUser(request('get', '/users/' + encodeURIComponent(user.value.public_id)), token);
      }

      function lookupUser() {
        var email = lookup.email.trim();
        if (!email || lookup.loading) { return Promise.resolve(); }
        markStale('audit');
        lookup.error = '';
        lookup.loading = true;
        user.value = null;
        var token = ++userToken;
        var promise = request('post', '/users/lookup', { data: { email: email } }).catch(function (error) {
          // 信箱格式不正確時，錯誤在 fields.email；其餘（查無此人等）用整體的訊息
          throw { message: (error.fields && error.fields.email) || error.message };
        });
        return showUser(promise, token);
      }

      // ---- 對話全文 -------------------------------------------------------------------------
      var convOpen = Vue.ref(false);
      var conv = Vue.reactive({ loading: false, error: '', data: null });
      var convToken = 0;

      function openConversation(publicId) {
        conv.data = null;
        conv.error = '';
        conv.loading = true;
        convOpen.value = true;
        var token = ++convToken;
        return request('get', '/conversations/' + encodeURIComponent(publicId)).then(function (data) {
          if (token === convToken) { conv.data = data; }
        }, function (error) {
          if (token === convToken) { conv.error = error.message; }
        }).finally(function () { if (token === convToken) { conv.loading = false; } });
      }

      // 關閉視窗就丟掉訊息內容：不留在記憶體裡，也讓還沒回來的請求結果被忽略
      Vue.watch(convOpen, function (open) {
        if (open) { return; }
        convToken++;
        conv.data = null;
        conv.error = '';
        conv.loading = false;
      });

      function viewConversationUser() {
        if (!conv.data) { return; }
        var owner = conv.data.conversation.user;
        convOpen.value = false;
        openUserById(owner);
      }

      // ---- 危險操作的確認視窗 -----------------------------------------------------------------
      var confirmOpen = Vue.ref(false);
      var confirmState = Vue.reactive({
        kind: '', targetId: '', targetLabel: '', currentTier: '', reason: '', tier: '', password: '', busy: false,
        error: { reason: '', password: '', tier: '', general: '' }
      });
      var confirmSpec = Vue.computed(function () { return CONFIRMS[confirmState.kind] || NO_CONFIRM; });
      var tierOptions = Vue.computed(function () {
        return TIERS.filter(function (value) { return value !== confirmState.currentTier; }).map(function (value) {
          return { value: value, label: fmt.tierLabel(value) };
        });
      });

      function clearConfirmErrors() {
        confirmState.error.reason = '';
        confirmState.error.password = '';
        confirmState.error.tier = '';
        confirmState.error.general = '';
      }

      function openConfirm(kind, target) {
        confirmState.kind = kind;
        confirmState.targetId = target.id;
        confirmState.targetLabel = target.label;
        confirmState.currentTier = target.tier || '';
        confirmState.reason = '';
        confirmState.password = '';
        confirmState.busy = false;
        confirmState.tier = kind === 'tier' ? (TIERS.filter(function (value) { return value !== target.tier; })[0] || '') : '';
        clearConfirmErrors();
        confirmOpen.value = true;
        window.setTimeout(function () {                      // 把焦點放在第一個要填的欄位
          var field = document.getElementById(kind === 'tier' ? 'admin-confirm-tier' : (CONFIRMS[kind].needsReason ? 'admin-confirm-reason' : 'admin-confirm-password'));
          if (field) { field.focus(); }
        }, 60);
      }

      // 視窗無論如何關閉（取消、Esc、點遮罩、成功）都清空密碼與原因
      Vue.watch(confirmOpen, function (open) {
        if (open) { return; }
        confirmState.password = '';
        confirmState.reason = '';
        confirmState.busy = false;
        clearConfirmErrors();
      });

      function userTarget() {
        return { id: user.value.public_id, label: user.value.display_name, tier: user.value.tier };
      }
      function askSuspend() { if (user.value) { openConfirm('suspend', userTarget()); } }
      function askUnsuspend() { if (user.value) { openConfirm('unsuspend', userTarget()); } }
      function askTier() { if (user.value) { openConfirm('tier', userTarget()); } }
      function askUnlock(publicId, displayName) { openConfirm('unlock', { id: publicId, label: displayName }); }
      function askDeleteConversation() {
        if (!conv.data) { return; }
        var id = conv.data.conversation.id;
        convOpen.value = false;                                // 先關掉對話全文，不疊兩個視窗
        // 等對話全文的視窗完全釋放（還原捲動鎖與焦點）之後再開確認視窗，否則兩個視窗的釋放與啟用順序不定
        Vue.nextTick(function () { openConfirm('remove', { id: id, label: '對話 ' + fmt.shortId(id) }); });
      }

      function onConfirmEnter(event) {
        if (event.isComposing || event.keyCode === 229) { return; }     // 中文輸入法選字時的 Enter 不是送出
        event.preventDefault();
        submitConfirm();
      }

      function removeConversationFromLists(publicId) {
        ['flagged', 'feedback'].forEach(function (id) {
          lists[id].items = lists[id].items.filter(function (row) { return row.conversation !== publicId; });
        });
      }

      function removeUnlockedFromLists(publicId) {
        lists.locked.items = lists.locked.items.filter(function (row) { return row.public_id !== publicId; });
        lists.appeals.items = lists.appeals.items.filter(function (row) { return row.user_public_id !== publicId; });
      }

      function applyConfirmError(error) {
        var fields = error.fields || {};
        if (fields.password) { confirmState.error.password = fields.password; return; }
        if (fields.reason) { confirmState.error.reason = fields.reason; return; }
        if (fields.tier) { confirmState.error.tier = fields.tier; return; }
        if (error.code === 'no_password') { confirmState.error.password = error.message; return; }
        confirmState.error.general = error.message || '操作失敗，請稍後再試。';
      }

      function submitConfirm() {
        if (confirmState.busy || !confirmState.kind) { return Promise.resolve(); }
        var spec = confirmSpec.value;
        var kind = confirmState.kind;
        var targetId = confirmState.targetId;
        clearConfirmErrors();

        var reason = confirmState.reason.trim();
        var invalid = false;
        if (spec.needsReason && (!reason || reason.length > MAX_REASON)) {
          confirmState.error.reason = '請填寫原因（1 至 ' + MAX_REASON + ' 字，請勿填寫個人資料）。';
          invalid = true;
        }
        if (spec.needsTier && TIERS.indexOf(confirmState.tier) === -1) {
          confirmState.error.tier = '請選擇新的會員等級。';
          invalid = true;
        }
        if (!confirmState.password) {
          confirmState.error.password = '請輸入您的密碼。';
          invalid = true;
        }
        if (invalid) { return Promise.resolve(); }

        var body = { password: confirmState.password };
        confirmState.password = '';                            // 送出的當下立即清空，不論結果
        if (spec.needsReason) { body.reason = reason; }
        if (spec.needsTier) { body.tier = confirmState.tier; }
        confirmState.busy = true;

        return request('post', spec.path(targetId), { data: body }).then(function (data) {
          confirmOpen.value = false;
          UI.toast(spec.done);
          if (kind === 'remove') {
            removeConversationFromLists(targetId);
            markStale('audit');
          } else if (kind === 'unlock') {
            removeUnlockedFromLists(targetId);                 // 解除之後，帳號與它的申訴都從管理員的檢視消失
            markStale('overview', 'audit');
          } else {
            user.value = data.user;
            markStale('overview', 'audit');
          }
        }, function (error) {
          if (!confirmOpen.value) {                            // 視窗已被關掉：改用提示
            UI.toast(error.message || '操作失敗，請稍後再試。', { type: 'error' });
            return;
          }
          applyConfirmError(error);
        }).finally(function () { confirmState.busy = false; });
      }

      // ---- 啟動 -----------------------------------------------------------------------------
      Vue.onMounted(function () { ensureLoaded('overview'); });

      return {
        adminName: adminName, fmt: fmt, tabs: TABS, tab: tab, maxReason: MAX_REASON,
        overview: overview, overviewState: overviewState, warningLevel: warningLevel, warningHint: warningHint,
        budgetRows: budgetRows, tokenWidth: tokenWidth, levelNote: levelNote, reloadOverview: reloadOverview,
        lists: lists, reload: reload, loadMore: loadMore, selectTab: selectTab, suspensionNote: suspensionNote,
        user: user, lookup: lookup, userActions: userActions, userNote: userNote,
        openUserById: openUserById, refreshUser: refreshUser, lookupUser: lookupUser,
        convOpen: convOpen, conv: conv, openConversation: openConversation, viewConversationUser: viewConversationUser,
        confirmOpen: confirmOpen, confirmState: confirmState, confirmSpec: confirmSpec, tierOptions: tierOptions,
        askSuspend: askSuspend, askUnsuspend: askUnsuspend, askTier: askTier, askUnlock: askUnlock,
        askDeleteConversation: askDeleteConversation,
        onConfirmEnter: onConfirmEnter, submitConfirm: submitConfirm
      };
    }
  });
  app.component('admin-list-foot', ListFoot);
  UI.install(app);
  app.mount('#admin-app');
})();
