/* 管理員後台（Vue 3 全域變數模式）。
 *
 * - 所有資料都來自 /api/admin/*；頁面本身只有管理員的顯示名稱（初始狀態）。真正的權限、重新輸入密碼與稽核都在後端，
 *   這裡只負責顯示與收集輸入，前端的任何檢查（例如原因不可空白）都只是為了少打一次請求。
 * - Modal、Toast 的顯示完全由 Vue 的響應式變數接管（v-if／v-show），不使用 Bootstrap 的 JavaScript。
 * - 所有文字（使用者、模型、管理員填寫的原因）都以 {{ }} 插值當作純文字顯示，不使用 v-html。
 * - 危險操作（停權、解除停權、調整等級、刪除對話、解除帳號鎖定）共用一個確認視窗。密碼只存在輸入框的響應式欄位：送出的當下立即清空，
 *   取消、關閉視窗也清空，不寫入 localStorage、IndexedDB 或任何其他儲存體。
 * - 「看使用者」會依對外識別碼取得使用者（寫稽核）；信箱查詢是次要入口，標明「供客服反應時使用」。沒有使用者清單。
 * - 「被鎖定的帳號」與「申訴」：申訴只有目前被鎖定或被暫停發佈的帳號送出的才看得到（兩種都解除之後消失；每列標明是哪一種，並提供對應的解除鎖定或解除暫停），內容以純文字顯示；
 *   讀取申訴會寫稽核（只記筆數），所以和「模型失敗」一樣，進入分頁時才載入、不因切換分頁而重複讀取。
 * - 清單依分頁載入：進入分頁時才載入一次（模型失敗每次讀取都會寫稽核，所以不因切換分頁而重複讀取），
 *   「重新整理」才會重新讀取；做了會改變資料的操作之後，相關的分頁標記為過期，下次進入時重新載入。
 * - 「劇本審核」（rpg-script-publishing）：待審、上架中、已下架三個清單（清單沒有劇本內文）；點「看全文」才讀取版本（先寫稽核），
 *   全文與審核備註都以純文字顯示。核可不需密碼；退回有類別與選填備註；下架與恢復走共用的密碼確認視窗（類別與備註在同一個視窗）。
 *   待審數量取自待審清單（進入頁面時就讀取一次，顯示在分頁標籤上，不新增總覽欄位）。409（他人已處理）時關閉視窗、顯示訊息並重新載入清單。
 * - 「被檢舉」檢視（rpg-script-reports）：上架中且有未結案檢舉的版本，排序（紅線優先、人數、時間）由後端決定，前端不重排。開啟版本時先讀全文（`script.read` 稽核），
 *   上架中且有未結案檢舉才再讀檢舉面板（`report.read` 稽核；失敗只在面板顯示錯誤，不阻止全文）；面板的理由以中文標籤、自寫文字以純文字顯示，沒有檢舉人。
 *   「不成立（結案）」不需要密碼；「下架」沿用既有的密碼確認視窗（檢舉由下架轉移自動結案）。409／404（他人已處理）時關閉視窗、顯示原因並重新載入清單。
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
    { id: 'scripts', label: '劇本審核' },
    { id: 'locked', label: '被鎖定的帳號' },
    { id: 'suspended', label: '被暫停發佈' },
    { id: 'appeals', label: '申訴' },
    { id: 'users', label: '使用者' },
    { id: 'failures', label: '模型失敗' },
    { id: 'audit', label: '稽核' }
  ];

  /* 「劇本審核」的三個清單（status 是 GET /api/admin/scripts 的查詢參數）；清單編號為 scripts_<status>。 */
  var SCRIPT_VIEWS = [
    { id: 'pending_review', label: '待審' },
    { id: 'published', label: '上架中' },
    { id: 'reported', label: '被檢舉' },
    { id: 'removed', label: '已下架' }
  ];
  var PENDING_LIST = 'scripts_pending_review';
  var CATEGORY_OPTIONS = fmt.categoryOptions();

  /* 清單的來源。paged 為 false 的只讀一次（伺服器已限制筆數）；params 是固定的查詢參數。 */
  var SOURCES = {
    scripts_pending_review: { path: '/scripts', key: 'versions', paged: true, params: { status: 'pending_review' } },
    scripts_published: { path: '/scripts', key: 'versions', paged: true, params: { status: 'published' } },
    scripts_reported: { path: '/scripts', key: 'versions', paged: true, params: { status: 'reported' } },
    scripts_removed: { path: '/scripts', key: 'versions', paged: true, params: { status: 'removed' } },
    flagged: { path: '/flagged', key: 'conversations', paged: true },
    feedback: { path: '/feedback', key: 'conversations', paged: true },
    audit: { path: '/audit', key: 'entries', paged: true },
    failures: { path: '/failures', key: 'failures', paged: false },
    locked: { path: '/locked', key: 'accounts', paged: true },
    suspended: { path: '/publish-suspended', key: 'accounts', paged: true },
    appeals: { path: '/appeals', key: 'appeals', paged: true }
  };

  /* 十個危險操作。needsReason：停權、刪除帳號、刪除對話與調整遊戲幣必須填原因；needsTier：調整等級要選新的等級；needsWallet：調整遊戲幣要選餘額種類與增減量；
     needsCategory／needsNote：劇本下架要選類別（必填）並可附只有管理員看得到的備註（選填）。 */
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
    lift: {
      code: 'ADMIN_LIFT_PUBLISH_SUSPENSION', title: '解除這位玩家的暫停發佈？', confirmText: '確認解除', needsReason: false, needsTier: false,
      intro: '解除後，這位玩家可以再次送審劇本；待處理的申訴會標為已處理。解除之後若再被自動拒絕，要重新累積連續的次數才會再次暫停。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/lift-publish-suspension'; }, done: '已解除暫停發佈。'
    },
    wallet: {
      code: 'ADMIN_ADJUST_WALLET', title: '調整遊戲幣', confirmText: '確認調整', needsReason: true, needsTier: false, needsWallet: true,
      intro: '增加或扣除這位玩家的遊戲幣（扣除用負數）。調整不受免費幣上限限制，餘額不會低於 0；每一筆都會留下稽核紀錄。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/wallet/adjust'; }, done: '已調整遊戲幣。'
    },
    delete: {
      code: 'ADMIN_DELETE_USER', title: '永久刪除這個帳號？', confirmText: '確認永久刪除', needsReason: true, needsTier: false,
      intro: '帳號會立即且永久地刪除，無法復原，也沒有寬限期。連帶刪除：登入身分、全部對話、存檔與記事簿、遊戲幣、全部劇本（含已上架者）、檢舉與申訴；其他玩家已開出的存檔不受影響。'
        + '這位玩家目前的登入立即失效；之後他仍可用同一個 Google 帳號重新註冊成全新的帳號。',
      path: function (id) { return '/users/' + encodeURIComponent(id) + '/delete'; }, done: '已刪除帳號。'
    },
    remove: {
      code: 'ADMIN_DELETE_CONVERSATION', title: '刪除這個對話？', confirmText: '確認刪除', needsReason: true, needsTier: false,
      intro: '整個對話與其中所有訊息會被永久刪除，無法復原；用量紀錄會保留。',
      path: function (id) { return '/conversations/' + encodeURIComponent(id) + '/delete'; }, done: '已刪除對話。'
    },
    script_remove: {
      code: 'ADMIN_REMOVE_SCRIPT', title: '下架這份劇本？', confirmText: '確認下架', needsReason: false, needsTier: false, needsCategory: true, needsNote: true, script: true,
      intro: '下架後，這個版本立即不在公開目錄，也不能再用來開新存檔；已經開出的存檔不受影響。作者看得到類別與時間，看不到備註；只有您能恢復管理員下架的版本。',
      path: function (id) { return '/scripts/versions/' + encodeURIComponent(id) + '/remove'; }, done: '已下架。'
    },
    script_restore: {
      code: 'ADMIN_RESTORE_SCRIPT', title: '恢復上架這份劇本？', confirmText: '確認恢復', needsReason: false, needsTier: false, script: true,
      intro: '恢復後，這個版本回到公開目錄，內容與發佈時間不變；下架時記下的類別與備註會清除。',
      path: function (id) { return '/scripts/versions/' + encodeURIComponent(id) + '/restore'; }, done: '已恢復上架。'
    }
  };
  var NO_CONFIRM = { code: 'ADMIN_CONFIRM', title: '', confirmText: '確認', needsReason: false, needsTier: false, needsWallet: false, needsCategory: false, needsNote: false, intro: '' };
  var BUCKETS = ['free', 'paid'];

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
      var lists = {
        flagged: newList(), feedback: newList(), audit: newList(), failures: newList(), locked: newList(), suspended: newList(), appeals: newList(),
        scripts_pending_review: newList(), scripts_published: newList(), scripts_reported: newList(), scripts_removed: newList()
      };

      function fetchList(id, more) {
        var list = lists[id];
        var source = SOURCES[id];
        if (list.loading) { return Promise.resolve(); }
        if (!more) { list.items = []; list.done = false; list.loaded = false; }
        list.loading = true;
        list.error = '';
        var params = Object.assign({}, source.params || {}, source.paged ? { limit: PAGE_SIZE, offset: more ? list.items.length : 0 } : {});
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
        if (id === 'scripts') { id = scriptListId.value; }                // 「劇本審核」分頁：載入目前選的那個清單
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
          tier: manageable && current.status !== 'deletion_pending',
          wallet: manageable && current.status !== 'deletion_pending',
          delete: manageable                               // 任何狀態都可刪（含停權、鎖定與待刪除，不必等寬限期）；管理員不行
        };
      });
      var userNote = Vue.computed(function () {
        var current = user.value;
        if (!current) { return ''; }
        if (current.role !== 'user') { return '管理員帳號無法在後台操作。'; }
        return current.status === 'deletion_pending' ? '這個帳號已申請刪除，無法停權或調整，但仍可立即永久刪除。' : '';
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
        kind: '', targetId: '', targetLabel: '', currentTier: '', reason: '', tier: '', bucket: 'paid', delta: '', category: '', note: '', password: '', busy: false,
        error: { reason: '', password: '', tier: '', bucket: '', delta: '', category: '', note: '', general: '' }
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
        confirmState.error.bucket = '';
        confirmState.error.delta = '';
        confirmState.error.category = '';
        confirmState.error.note = '';
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
        confirmState.bucket = 'paid';
        confirmState.delta = '';
        confirmState.category = '';
        confirmState.note = '';
        clearConfirmErrors();
        confirmOpen.value = true;
        window.setTimeout(function () {                      // 把焦點放在第一個要填的欄位
          var spec = CONFIRMS[kind];
          var field = document.getElementById(kind === 'tier' ? 'admin-confirm-tier' : (kind === 'wallet' ? 'admin-confirm-bucket' : (spec.needsCategory ? 'admin-confirm-category' : (spec.needsReason ? 'admin-confirm-reason' : 'admin-confirm-password'))));
          if (field) { field.focus(); }
        }, 60);
      }

      // 視窗無論如何關閉（取消、Esc、點遮罩、成功）都清空密碼、原因與備註
      Vue.watch(confirmOpen, function (open) {
        if (open) { return; }
        confirmState.password = '';
        confirmState.reason = '';
        confirmState.note = '';
        confirmState.category = '';
        confirmState.busy = false;
        clearConfirmErrors();
      });

      function userTarget() {
        return { id: user.value.public_id, label: user.value.display_name, tier: user.value.tier };
      }
      function askSuspend() { if (user.value) { openConfirm('suspend', userTarget()); } }
      function askUnsuspend() { if (user.value) { openConfirm('unsuspend', userTarget()); } }
      function askTier() { if (user.value) { openConfirm('tier', userTarget()); } }
      function askWallet() { if (user.value) { openConfirm('wallet', userTarget()); } }
      function askDelete() { if (user.value && user.value.role === 'user') { openConfirm('delete', userTarget()); } }
      function askUnlock(publicId, displayName) { openConfirm('unlock', { id: publicId, label: displayName }); }
      function askLift(publicId, displayName) { openConfirm('lift', { id: publicId, label: displayName }); }
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

      function removeLiftedFromLists(publicId) {
        lists.suspended.items = lists.suspended.items.filter(function (row) { return row.public_id !== publicId; });
        lists.appeals.items = lists.appeals.items.filter(function (row) { return row.user_public_id !== publicId; });
      }

      /* 帳號被永久刪除：它在鎖定、暫停發佈與申訴清單中的列都不再有意義（申訴隨帳號刪除）。 */
      function removeDeletedFromLists(publicId) {
        lists.locked.items = lists.locked.items.filter(function (row) { return row.public_id !== publicId; });
        removeLiftedFromLists(publicId);                       // 暫停發佈清單與申訴清單
      }

      function applyConfirmError(error) {
        var fields = error.fields || {};
        if (fields.category || fields.note) {                  // 劇本下架：後端一次指出所有不合法的欄位
          confirmState.error.category = fields.category || '';
          confirmState.error.note = fields.note || '';
          if (fields.password) { confirmState.error.password = fields.password; }
          return;
        }
        if (fields.password) { confirmState.error.password = fields.password; return; }
        if (fields.reason) { confirmState.error.reason = fields.reason; return; }
        if (fields.tier) { confirmState.error.tier = fields.tier; return; }
        if (fields.bucket) { confirmState.error.bucket = fields.bucket; return; }
        if (fields.delta) { confirmState.error.delta = fields.delta; return; }
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
        if (spec.needsCategory && !isCategory(confirmState.category)) {
          confirmState.error.category = '請選擇類別。';
          invalid = true;
        }
        var noteIssue = spec.needsNote ? fmt.noteProblem(confirmState.note) : '';
        if (noteIssue) {
          confirmState.error.note = noteIssue;
          invalid = true;
        }
        var delta = null;
        if (spec.needsWallet) {
          if (BUCKETS.indexOf(confirmState.bucket) === -1) { confirmState.error.bucket = '請選擇要調整的餘額。'; invalid = true; }
          delta = fmt.parseCoinDelta(String(confirmState.delta));
          if (delta === null) { confirmState.error.delta = '增減量必須是不為 0 的整數（扣除請用負數，例如 -30）。'; invalid = true; }
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
        if (spec.needsWallet) { body.bucket = confirmState.bucket; body.delta = delta; }
        if (spec.needsCategory) { body.category = confirmState.category; }
        if (spec.needsNote && confirmState.note.trim()) { body.note = confirmState.note; }
        confirmState.busy = true;

        return request('post', spec.path(targetId), { data: body }).then(function (data) {
          confirmOpen.value = false;
          UI.toast(spec.done);
          if (kind === 'remove') {
            removeConversationFromLists(targetId);
            markStale('audit');
          } else if (kind === 'script_remove') {
            removeScriptRow('scripts_published', targetId);       // 離開上架中，出現在已下架（下次進入時重新載入）
            removeScriptRow('scripts_reported', targetId);        // 它未結案的檢舉由下架自動結案，不再出現在被檢舉清單
            markStale('scripts_removed', 'audit');
          } else if (kind === 'script_restore') {
            removeScriptRow('scripts_removed', targetId);
            markStale('scripts_published', 'audit');
          } else if (kind === 'unlock') {
            removeUnlockedFromLists(targetId);                 // 解除之後，帳號與它的申訴都從管理員的檢視消失
            markStale('overview', 'audit');
          } else if (kind === 'lift') {
            removeLiftedFromLists(targetId);                   // 申訴是否還看得到取決於帳號是否仍被鎖定，下次開啟時重新讀取
            markStale('overview', 'audit', 'appeals');
          } else if (kind === 'delete') {
            user.value = null;                                 // 成功回應沒有 user；帳號已不存在，面板清空（不可落入下面的預設分支）
            removeDeletedFromLists(targetId);
            markStale('flagged', 'feedback', 'overview', 'audit');   // 被刪帳號的對話已一併消失，下次進入時重新載入
          } else {
            user.value = data.user;
            markStale('overview', 'audit');
          }
        }, function (error) {
          if (!confirmOpen.value) {                            // 視窗已被關掉：改用提示
            UI.toast(error.message || '操作失敗，請稍後再試。', { type: 'error' });
            return;
          }
          if (spec.script && error.status === 409) {           // 他人已處理（版本已不是這個狀態）：關閉視窗、顯示原因並重新載入清單
            confirmOpen.value = false;
            UI.toast(error.message || '這份劇本的狀態已經改變，請重新整理。', { type: 'error' });
            return refreshScriptLists();
          }
          applyConfirmError(error);
        }).finally(function () { confirmState.busy = false; });
      }

      // ---- 劇本審核 -------------------------------------------------------------------------
      var scriptView = Vue.ref('pending_review');
      var scriptListId = Vue.computed(function () { return 'scripts_' + scriptView.value; });
      var scriptList = Vue.computed(function () { return lists[scriptListId.value]; });
      var scriptEmptyText = Vue.computed(function () { return fmt.scriptEmptyText(scriptView.value); });
      var pendingText = Vue.computed(function () {
        var list = lists[PENDING_LIST];
        return list.loaded ? fmt.pendingCountText(list.items.length, list.done) : '';
      });

      function tabBadge(id) { return id === 'scripts' ? pendingText.value : ''; }
      function isCategory(value) { return CATEGORY_OPTIONS.some(function (option) { return option.value === value; }); }
      function failureText(error) { return (error && error.message) || '操作失敗，請稍後再試。'; }

      function selectScriptView(id) {
        scriptView.value = id;
        return ensureLoaded('scripts');
      }
      function reloadScripts() { return fetchList(scriptListId.value, false); }
      function loadMoreScripts() { return fetchList(scriptListId.value, true); }

      /* 他人已處理（409）或版本已不可讀（404）：待審清單（標籤上的數量也取自它）與目前看的清單重新載入，另一個清單標記為過期。 */
      function refreshScriptLists() {
        markStale('scripts_published', 'scripts_reported', 'scripts_removed', 'audit');
        var jobs = [fetchList(PENDING_LIST, false)];
        if (scriptListId.value !== PENDING_LIST) { jobs.push(fetchList(scriptListId.value, false)); }
        return Promise.all(jobs);
      }

      function removeScriptRow(listId, publicId) {
        lists[listId].items = lists[listId].items.filter(function (row) { return row.id !== publicId; });
      }

      // 全文：讀取版本會寫稽核，所以只在按下「看全文」時才讀取；關閉視窗就丟掉內容
      var scriptOpen = Vue.ref(false);
      var scriptDoc = Vue.reactive({ loading: false, error: '', actionError: '', version: null, busy: false });
      var scriptToken = 0;                                     // 只採用最後一次讀取的結果
      /* 檢舉面板（只在上架中且有未結案檢舉的版本顯示）：讀取會寫 `report.read` 稽核，所以與全文一樣只在開啟版本時讀；失敗只在面板顯示，不影響全文。 */
      var scriptReports = Vue.reactive({ shown: false, loading: false, error: '', items: [] });
      var scriptReportRows = Vue.computed(function () { return fmt.reportItems(scriptReports.items); });
      var openReportCount = Vue.computed(function () { return scriptReportRows.value.filter(function (row) { return row.open; }).length; });
      var scriptSections = Vue.computed(function () { return scriptDoc.version ? fmt.scriptSections(scriptDoc.version.content) : []; });
      var scriptDocActions = Vue.computed(function () { return fmt.scriptActions(scriptDoc.version); });

      function clearScriptReports() {
        scriptReports.shown = false;
        scriptReports.loading = false;
        scriptReports.error = '';
        scriptReports.items = [];
      }

      /* 讀取檢舉面板：只採用最後一次開啟的結果（token）；失敗只設定面板的錯誤。 */
      function loadScriptReports(publicId, token) {
        scriptReports.shown = true;
        scriptReports.loading = true;
        scriptReports.error = '';
        markStale('audit');
        return request('get', '/scripts/versions/' + encodeURIComponent(publicId) + '/reports').then(function (data) {
          if (token === scriptToken) { scriptReports.items = Array.isArray(data.reports) ? data.reports : []; }
        }, function (error) {
          if (token === scriptToken) { scriptReports.error = failureText(error); }
        }).finally(function () { if (token === scriptToken) { scriptReports.loading = false; } });
      }

      function reloadScriptReports() {
        if (!scriptDoc.version) { return Promise.resolve(); }
        return loadScriptReports(scriptDoc.version.id, scriptToken);
      }

      function openScript(publicId) {
        scriptDoc.version = null;
        scriptDoc.error = '';
        scriptDoc.actionError = '';
        scriptDoc.busy = false;
        scriptDoc.loading = true;
        clearScriptReports();
        scriptOpen.value = true;
        markStale('audit');
        var token = ++scriptToken;
        return request('get', '/scripts/versions/' + encodeURIComponent(publicId)).then(function (data) {
          if (token !== scriptToken) { return; }
          scriptDoc.version = data.version;
          scriptDoc.loading = false;                                        // 全文先顯示，檢舉面板自己有載入中的提示
          if (fmt.scriptActions(data.version).dismiss) { return loadScriptReports(publicId, token); }     // 先讀全文，上架中且有未結案檢舉才再讀檢舉
        }, function (error) {
          if (token !== scriptToken) { return; }
          scriptDoc.error = failureText(error);
          if (error.status === 404) { refreshScriptLists(); }  // 作者在這段時間撤回了：清單已過期
        }).finally(function () { if (token === scriptToken) { scriptDoc.loading = false; } });
      }

      Vue.watch(scriptOpen, function (open) {
        if (open) { return; }
        scriptToken++;
        scriptDoc.version = null;
        scriptDoc.error = '';
        scriptDoc.actionError = '';
        scriptDoc.loading = false;
        scriptDoc.busy = false;
        clearScriptReports();
      });

      function approveOpenScript() {
        if (!scriptDoc.version || scriptDoc.busy) { return Promise.resolve(); }
        var publicId = scriptDoc.version.id;
        scriptDoc.busy = true;
        scriptDoc.actionError = '';
        return request('post', '/scripts/versions/' + encodeURIComponent(publicId) + '/approve', { data: {} }).then(function () {
          scriptOpen.value = false;
          UI.toast('已核可，這份劇本已上架。');
          removeScriptRow(PENDING_LIST, publicId);
          markStale('scripts_published', 'scripts_reported', 'scripts_removed', 'audit');     // 核可也會把同劇本下架的舊版改為「已被取代」（舊版未結案的檢舉隨之自動結案）
        }, function (error) {
          if (!scriptOpen.value) { UI.toast(failureText(error), { type: 'error' }); return; }
          if (error.status === 409) {
            scriptOpen.value = false;
            UI.toast(failureText(error), { type: 'error' });
            return refreshScriptLists();
          }
          scriptDoc.actionError = failureText(error);
        }).finally(function () { scriptDoc.busy = false; });
      }

      /* 結案為「不成立」：不需要密碼，版本仍上架。成功關閉視窗、該列從被檢舉清單消失；409（他人已結案或已下架）與 404 關閉視窗、顯示原因並重新載入清單。 */
      function dismissOpenScript() {
        // 讀完檢舉（沒有載入中、沒有讀取錯誤）才能結案：不讓管理員在沒看到內容時結案
        if (!scriptDoc.version || scriptDoc.busy || scriptReports.loading || scriptReports.error || !fmt.scriptActions(scriptDoc.version).dismiss) { return Promise.resolve(); }
        var publicId = scriptDoc.version.id;
        scriptDoc.busy = true;
        scriptDoc.actionError = '';
        return request('post', '/scripts/versions/' + encodeURIComponent(publicId) + '/reports/dismiss', { data: {} }).then(function () {
          scriptOpen.value = false;
          UI.toast('已結案：檢舉不成立。');
          removeScriptRow('scripts_reported', publicId);
          markStale('scripts_published', 'audit');                        // 上架中清單的檢舉計數要更新
        }, function (error) {
          if (!scriptOpen.value) { UI.toast(failureText(error), { type: 'error' }); return; }
          if (error.status === 409 || error.status === 404) {
            scriptOpen.value = false;
            UI.toast(failureText(error), { type: 'error' });
            return refreshScriptLists();
          }
          scriptDoc.actionError = failureText(error);
        }).finally(function () { scriptDoc.busy = false; });
      }

      /* 從全文視窗進入退回、下架或恢復：先關掉全文（不疊兩個視窗），等它釋放之後再開下一個視窗。 */
      function leaveScriptModal(next) {
        var version = scriptDoc.version;
        if (!version) { return; }
        var target = { id: version.id, label: version.title };
        scriptOpen.value = false;
        Vue.nextTick(function () { next(target); });
      }
      function askRejectFromDoc() { leaveScriptModal(openReject); }
      function askRemoveFromDoc() { leaveScriptModal(function (target) { openConfirm('script_remove', target); }); }
      function askRestoreFromDoc() { leaveScriptModal(function (target) { openConfirm('script_restore', target); }); }
      function askRemoveScript(row) { openConfirm('script_remove', { id: row.id, label: row.title }); }
      function askRestoreScript(row) { openConfirm('script_restore', { id: row.id, label: row.title }); }

      // 退回：類別（必填）與只有管理員看得到的備註（選填）；不需要密碼
      var rejectOpen = Vue.ref(false);
      var rejectState = Vue.reactive({ id: '', label: '', category: '', note: '', busy: false, error: { category: '', note: '', general: '' } });

      function clearRejectErrors() {
        rejectState.error.category = '';
        rejectState.error.note = '';
        rejectState.error.general = '';
      }

      function openReject(target) {
        rejectState.id = target.id;
        rejectState.label = target.label;
        rejectState.category = '';
        rejectState.note = '';
        rejectState.busy = false;
        clearRejectErrors();
        rejectOpen.value = true;
        window.setTimeout(function () {
          var field = document.getElementById('admin-reject-category');
          if (field) { field.focus(); }
        }, 60);
      }

      Vue.watch(rejectOpen, function (open) {
        if (open) { return; }
        rejectState.category = '';
        rejectState.note = '';
        rejectState.busy = false;
        clearRejectErrors();
      });

      function submitReject() {
        if (rejectState.busy || !rejectState.id) { return Promise.resolve(); }
        clearRejectErrors();
        var publicId = rejectState.id;
        var invalid = false;
        if (!isCategory(rejectState.category)) { rejectState.error.category = '請選擇類別。'; invalid = true; }
        var noteIssue = fmt.noteProblem(rejectState.note);
        if (noteIssue) { rejectState.error.note = noteIssue; invalid = true; }
        if (invalid) { return Promise.resolve(); }

        var body = { category: rejectState.category };
        if (rejectState.note.trim()) { body.note = rejectState.note; }
        rejectState.busy = true;
        return request('post', '/scripts/versions/' + encodeURIComponent(publicId) + '/reject', { data: body }).then(function () {
          rejectOpen.value = false;
          UI.toast('已退回，作者只會看到類別。');
          removeScriptRow(PENDING_LIST, publicId);
          markStale('audit');
        }, function (error) {
          if (!rejectOpen.value) { UI.toast(failureText(error), { type: 'error' }); return; }
          if (error.status === 409) {
            rejectOpen.value = false;
            UI.toast(failureText(error), { type: 'error' });
            return refreshScriptLists();
          }
          var fields = error.fields || {};
          if (fields.category || fields.note) {
            rejectState.error.category = fields.category || '';
            rejectState.error.note = fields.note || '';
            return;
          }
          rejectState.error.general = failureText(error);
        }).finally(function () { rejectState.busy = false; });
      }

      // ---- 啟動 -----------------------------------------------------------------------------
      // 待審數量要顯示在分頁標籤上，所以進入頁面就讀一次待審清單（清單端點只讀、不寫稽核）
      Vue.onMounted(function () { ensureLoaded('overview'); ensureLoaded(PENDING_LIST); });

      return {
        adminName: adminName, fmt: fmt, tabs: TABS, tab: tab, maxReason: MAX_REASON, maxNote: fmt.MAX_NOTE, categoryOptions: CATEGORY_OPTIONS,
        overview: overview, overviewState: overviewState, warningLevel: warningLevel, warningHint: warningHint,
        budgetRows: budgetRows, tokenWidth: tokenWidth, levelNote: levelNote, reloadOverview: reloadOverview,
        lists: lists, reload: reload, loadMore: loadMore, selectTab: selectTab, suspensionNote: suspensionNote,
        user: user, lookup: lookup, userActions: userActions, userNote: userNote,
        openUserById: openUserById, refreshUser: refreshUser, lookupUser: lookupUser,
        convOpen: convOpen, conv: conv, openConversation: openConversation, viewConversationUser: viewConversationUser,
        confirmOpen: confirmOpen, confirmState: confirmState, confirmSpec: confirmSpec, tierOptions: tierOptions,
        askSuspend: askSuspend, askUnsuspend: askUnsuspend, askTier: askTier, askWallet: askWallet, askDelete: askDelete, askUnlock: askUnlock, askLift: askLift,
        askDeleteConversation: askDeleteConversation,
        onConfirmEnter: onConfirmEnter, submitConfirm: submitConfirm,
        scriptViews: SCRIPT_VIEWS, scriptView: scriptView, scriptListId: scriptListId, scriptList: scriptList, scriptEmptyText: scriptEmptyText, pendingText: pendingText,
        tabBadge: tabBadge, selectScriptView: selectScriptView, reloadScripts: reloadScripts, loadMoreScripts: loadMoreScripts,
        scriptOpen: scriptOpen, scriptDoc: scriptDoc, scriptSections: scriptSections, scriptDocActions: scriptDocActions,
        openScript: openScript, approveOpenScript: approveOpenScript, dismissOpenScript: dismissOpenScript,
        scriptReports: scriptReports, scriptReportRows: scriptReportRows, openReportCount: openReportCount, reloadScriptReports: reloadScriptReports,
        askRejectFromDoc: askRejectFromDoc, askRemoveFromDoc: askRemoveFromDoc, askRestoreFromDoc: askRestoreFromDoc,
        askRemoveScript: askRemoveScript, askRestoreScript: askRestoreScript,
        rejectOpen: rejectOpen, rejectState: rejectState, submitReject: submitReject
      };
    }
  });
  app.component('admin-list-foot', ListFoot);
  UI.install(app);
  app.mount('#admin-app');
})();
