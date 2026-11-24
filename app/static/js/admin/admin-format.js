/* 管理員後台的顯示邏輯（與框架無關的純函式；Node 可以直接測試）。
 *
 * 這裡只決定「怎麼顯示」：台灣時間的日期、列舉值的中文標籤、用量比例的百分比與警示等級。
 * 真正的權限、額度與暫停永遠在後端判斷；遇到不認得的值（例如後端新增了動作而前端還沒更新）一律退回原始代碼，
 * 不丟例外、不讓整頁壞掉。所有回傳值都是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Admin = root.Admin || {};
    root.Admin.format = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;     // 台灣沒有夏令時間，固定 UTC+8，不依賴 Intl 的時區資料
  var EMPTY = '—';
  var WARN_RATIO = 0.8;                          // 全站用量達 80% 告警（AGENTS.md 第 10 節）
  var FULL_RATIO = 1;                            // 達 100% 回覆系統忙碌

  /* 與後端的列舉一一對應（app/models/user.py、admin_audit_log.py、llm_call_log.py）；
     tests/test_admin_format_labels.py 會比對兩邊，新增列舉卻漏了標籤時測試會失敗。 */
  var LABELS = {
    status: {
      active: '使用中', suspended: '已停權', deletion_pending: '待刪除'
    },
    tier: { free: '一般', plus: '進階', vip: '尊榮' },
    role: { user: '使用者', admin: '管理員' },
    action: {
      'user.lookup': '查詢使用者', 'user.suspend': '停權', 'user.unsuspend': '解除停權', 'user.change_tier': '調整會員等級', 'user.delete': '刪除帳號',
      'conversation.read': '讀取對話', 'conversation.delete': '刪除對話', 'failures.read': '查看模型失敗原文',
      'reauth.failed': '重新輸入密碼失敗',
      'limit.set': '修改營運限制', 'publish.suspend': '暫停發佈', 'publish.lift': '解除暫停發佈', 'wallet.adjust': '調整遊戲幣',
      'account.lock': '帳號自動鎖定', 'appeal.submit': '玩家送出申訴', 'appeal.read': '檢視申訴', 'account.unlock': '解除帳號鎖定',
      'script.read': '讀取劇本全文', 'script.approve': '核可劇本', 'script.reject': '退回劇本', 'script.remove': '下架劇本', 'script.restore': '恢復上架劇本',
      'report.read': '讀取劇本檢舉', 'report.dismiss': '結案劇本檢舉（不成立）'
    },
    /* 鎖定原因（app/models/lock_strike.py 的計次類別，也是 users.lock_reason 的值） */
    lockReason: { identity_rejected: '登入或年齡確認多次被拒', rate_limited: '短時間內訊息過多' },
    kind: { chat: '客服對話', summary: '對話摘要', script_review: '劇本審查', protagonist_gen: '主角生成', protagonist_review: '主角審查', rpg_stream: '劇情串流', rpg_state: '劇情狀態', rpg_opening: '第 0 幕', rpg_notebook: '記事簿', rpg_suggest: '行動建議' },
    messageRole: { user: '使用者', assistant: '客服' },
    messageStatus: { ok: '正常', fallback: '降級回覆' },
    via: { email: '以信箱', id: '以識別碼' },
    /* 遊戲幣的兩個餘額（app/models/wallet.py 的 BUCKETS） */
    bucket: { free: '免費幣', paid: '付費幣' },
    /* 劇本審核（rpg-script-publishing；app/models/script.py）：版本狀態、下架者、人工決定的類別（審查類別加 other_policy）、模型層結果、
       讀者頻道、結局模式與里程碑觸發種類。 */
    scriptStatus: { draft: '草稿', pending_review: '待審', published: '上架中', rejected: '已退回', removed: '已下架', superseded: '已被取代' },
    remover: { author: '作者', admin: '管理員' },
    category: {
      minor_sexual: '涉及未成年角色的性內容', sexual_violence: '性暴力', non_consensual: '非自願的性情節', real_person_sexual: '以真人名義的性內容',
      minor_suspected: '疑似涉及未成年角色', real_person_suspected: '疑似涉及真人', tier_mismatch: '內容不符合綁定的安全模式', other_concern: '其他疑慮',
      other_policy: '其他違反平台規範'
    },
    verdict: { pass: '通過', flag: '有疑慮', reject: '拒絕' },
    audience: { all: '全讀者', male: '男頻', female: '女頻' },
    endingMode: { none: '無結局', custom: '自訂結局' },
    triggerKind: { age: '年齡', year: '年份' },
    /* 劇本檢舉（rpg-script-reports；app/models/script_report.py）：玩家勾選的七個理由（值與後端的 REPORT_REASONS 相同）與結案結果。 */
    reportReason: {
      minor_sexual: '涉及未成年的性內容', sexual_violence: '性暴力或非自願情節', real_person_sexual: '以真人名義的性內容', tier_mismatch: '內容分級不符',
      plagiarism: '抄襲或侵權', privacy_harassment: '洩漏個資或騷擾', other: '其他'
    },
    reportResult: { removed: '已下架', dismissed: '不成立', superseded: '被新版本取代' }
  };
  /* 紅線理由（後端的 RED_LINE_REASONS）：有人勾選任一項，版本在被檢舉清單排最前（排序由後端決定，這裡只用來標示）。 */
  var RED_LINE_REASONS = ['minor_sexual', 'sexual_violence', 'real_person_sexual'];

  var MAX_NOTE = 500;                            // 審核備註上限（Unicode 字元；app/models/script.py 的 MAX_DECISION_NOTE_CHARS）

  function pad(number) { return (number < 10 ? '0' : '') + number; }

  /* 伺服器的時間字串一律帶時區；萬一沒有，視為 UTC，而不是瀏覽器的本地時間。 */
  function parseTime(text) {
    if (typeof text !== 'string' || !text) { return null; }
    var value = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text + 'Z';
    var ms = Date.parse(value);
    return isNaN(ms) ? null : ms;
  }

  function taipeiParts(ms) {
    var moment = new Date(ms + TAIPEI_OFFSET_MS);
    return {
      year: moment.getUTCFullYear(), month: moment.getUTCMonth() + 1, day: moment.getUTCDate(),
      hour: moment.getUTCHours(), minute: moment.getUTCMinutes()
    };
  }

  /* 台灣時間的 YYYY-MM-DD；無法解析回傳「—」。 */
  function taipeiDate(text) {
    var ms = parseTime(text);
    if (ms === null) { return EMPTY; }
    var p = taipeiParts(ms);
    return p.year + '-' + pad(p.month) + '-' + pad(p.day);
  }

  /* 台灣時間的 YYYY-MM-DD HH:MM。 */
  function taipeiDateTime(text) {
    var ms = parseTime(text);
    if (ms === null) { return EMPTY; }
    var p = taipeiParts(ms);
    return p.year + '-' + pad(p.month) + '-' + pad(p.day) + ' ' + pad(p.hour) + ':' + pad(p.minute);
  }

  /* 台灣時間的 HH:MM。 */
  function taipeiClock(text) {
    var ms = parseTime(text);
    if (ms === null) { return EMPTY; }
    var p = taipeiParts(ms);
    return pad(p.hour) + ':' + pad(p.minute);
  }

  /* 總覽長條圖下方的日期標籤：'2026-10-29' → '10/29'（輸入已是台灣日期，不再換算時區）。 */
  function shortDate(dateText) {
    var match = /^\d{4}-(\d{2})-(\d{2})$/.exec(typeof dateText === 'string' ? dateText : '');
    return match ? match[1] + '/' + match[2] : EMPTY;
  }

  /* 列舉值的中文標籤；不認得的值退回原始代碼（字串化），空值回傳「—」。 */
  function label(group, value) {
    if (value === null || value === undefined || value === '') { return EMPTY; }
    var table = LABELS[group] || {};
    var key = String(value);
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : key;
  }

  function statusLabel(value) { return label('status', value); }
  function tierLabel(value) { return label('tier', value); }
  function roleLabel(value) { return label('role', value); }
  function actionLabel(value) { return label('action', value); }
  function kindLabel(value) { return label('kind', value); }
  function messageRoleLabel(value) { return label('messageRole', value); }
  function messageStatusLabel(value) { return label('messageStatus', value); }
  function lockReasonLabel(value) { return label('lockReason', value); }
  function bucketLabel(value) { return label('bucket', value); }
  function scriptStatusLabel(value) { return label('scriptStatus', value); }
  function reportReasonLabel(value) { return label('reportReason', value); }
  function reportResultLabel(value) { return label('reportResult', value); }
  function removerLabel(value) { return label('remover', value); }
  function categoryLabel(value) { return label('category', value); }
  function verdictLabel(value) { return label('verdict', value); }
  function audienceLabel(value) { return label('audience', value); }

  /* 比例（0 到 1，可超過 1）轉成百分比文字。無條件捨去：0.998 顯示 99% 而不是 100%，
     這樣「100%」只在真的用滿時出現，與 ratioLevel 的界線一致。資料不是有限的非負數時回傳「—」。 */
  function ratioPercent(ratio) {
    if (typeof ratio !== 'number' || !isFinite(ratio) || ratio < 0) { return EMPTY; }
    return Math.floor(ratio * 100 + 1e-9) + '%';
  }

  /* 警示等級：ok（未滿 80%）、warn（80% 以上、未滿 100%）、full（100% 以上）、unknown（資料不完整）。 */
  function ratioLevel(ratio) {
    if (typeof ratio !== 'number' || !isFinite(ratio) || ratio < 0) { return 'unknown'; }
    if (ratio >= FULL_RATIO) { return 'full'; }
    return ratio >= WARN_RATIO ? 'warn' : 'ok';
  }

  /* 長條圖的寬度（0 到 100 的整數），超過 100% 也只畫滿。 */
  function ratioWidth(ratio) {
    if (typeof ratio !== 'number' || !isFinite(ratio) || ratio <= 0) { return 0; }
    return Math.min(100, Math.floor(ratio * 100 + 1e-9));
  }

  /* 千分位；不是整數的資料回傳「—」。 */
  function count(value) {
    if (typeof value !== 'number' || !isFinite(value)) { return EMPTY; }
    var sign = value < 0 ? '-' : '';
    return sign + String(Math.floor(Math.abs(value))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /* 帶正負號的千分位（遊戲幣的增減量）：+500、-30；不是整數的資料回傳「—」。 */
  function signedCount(value) {
    if (typeof value !== 'number' || !isFinite(value)) { return EMPTY; }
    return (value > 0 ? '+' : '') + count(value);
  }

  /* 管理員輸入的增減量：只接受不為 0 的整數（可帶 + 或 -，不接受小數、千分位與空白以外的字元）；其餘回傳 null。 */
  function parseCoinDelta(text) {
    if (typeof text !== 'string') { return null; }
    var trimmed = text.trim();
    if (!/^[+-]?\d{1,9}$/.test(trimmed)) { return null; }
    var value = parseInt(trimmed, 10);
    return value === 0 ? null : value;
  }

  /* 對外識別碼（UUID）在表格裡只顯示前 8 碼；完整值放在 title 與操作中。 */
  function shortId(value) {
    return typeof value === 'string' && value ? value.slice(0, 8) : EMPTY;
  }

  /* 被標記清單的「暫停中」說明；不在暫停期間回傳空字串。 */
  function suspensionText(untilText, nowMs) {
    var until = parseTime(untilText);
    if (until === null || until <= nowMs) { return ''; }
    return '客服對話暫停中，' + taipeiClock(untilText) + ' 恢復';
  }

  /* 稽核細節（只有列舉值與整數）轉成一行中文；形狀不認得時回傳空字串。 */
  function auditDetailText(action, detail) {
    var d = detail && typeof detail === 'object' ? detail : {};
    if (action === 'user.change_tier') { return tierLabel(d.from_tier) + ' → ' + tierLabel(d.to_tier); }
    if (action === 'user.suspend') { return '原狀態：' + statusLabel(d.from_status); }
    if (action === 'user.unsuspend') { return '還原為：' + statusLabel(d.to_status); }
    if (action === 'user.lookup') { return label('via', d.via) + (d.found === 0 ? '，查無此人' : ''); }
    if (action === 'failures.read') { return typeof d.count === 'number' ? d.count + ' 筆' : ''; }
    if (action === 'reauth.failed') { return d.attempted ? '嘗試：' + actionLabel(d.attempted) : ''; }
    if (action === 'account.lock') { return d.reason ? '原因：' + lockReasonLabel(d.reason) : ''; }
    if (action === 'appeal.submit') { return typeof d.length === 'number' ? d.length + ' 字' : ''; }
    if (action === 'appeal.read') { return typeof d.count === 'number' ? d.count + ' 則' : ''; }
    if (action === 'wallet.adjust') {
      if (typeof d.delta !== 'number' || typeof d.balance_after !== 'number') { return ''; }
      return bucketLabel(d.bucket) + ' ' + signedCount(d.delta) + '，調整後 ' + count(d.balance_after);
    }
    if (typeof action === 'string' && action.indexOf('script.') === 0) { return scriptAuditText(d); }
    if (action === 'report.read' || action === 'report.dismiss') { return reportAuditText(action, d); }
    return '';
  }

  /* 劇本檢舉的稽核細節（只有版本與劇本的對外識別碼與筆數；沒有理由、自寫文字、標題與檢舉人）：
     「版本 4d6f5c2a，劇本 9a8c0123，3 筆」（讀取）、「…，結案 3 筆」（結案）。沒有可顯示的欄位時回傳空字串。 */
  function reportAuditText(action, d) {
    var parts = [];
    if (typeof d.version_id === 'string' && d.version_id) { parts.push('版本 ' + shortId(d.version_id)); }
    if (typeof d.script_id === 'string' && d.script_id) { parts.push('劇本 ' + shortId(d.script_id)); }
    if (typeof d.count === 'number' && isFinite(d.count)) { parts.push((action === 'report.dismiss' ? '結案 ' : '') + count(d.count) + ' 筆'); }
    return parts.join('，');
  }

  /* 劇本審核的稽核細節（只有版本與劇本的對外識別碼、狀態與類別列舉；沒有標題、筆名與任何劇本文字）：
     「版本 4d6f5c2a，劇本 9a8c0123，狀態：上架中，類別：性暴力」。沒有可顯示的欄位時回傳空字串。 */
  function scriptAuditText(d) {
    var parts = [];
    if (typeof d.version_id === 'string' && d.version_id) { parts.push('版本 ' + shortId(d.version_id)); }
    if (typeof d.script_id === 'string' && d.script_id) { parts.push('劇本 ' + shortId(d.script_id)); }
    if (d.status) { parts.push('狀態：' + scriptStatusLabel(d.status)); }
    if (d.category) { parts.push('類別：' + categoryLabel(d.category)); }
    return parts.join('，');
  }

  // ---- 劇本審核（rpg-script-publishing）------------------------------------------------------------

  function plainObject(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }

  function nonEmptyText(value) { return typeof value === 'string' ? value.trim() : ''; }

  function wholeNumberText(value) { return typeof value === 'number' && isFinite(value) ? String(Math.floor(value)) : ''; }

  /* 類別清單轉一行文字（「、」分隔）；不認得的類別退回原始代碼，不是陣列或沒有項目回傳空字串。 */
  function categoriesText(categories) {
    if (!Array.isArray(categories)) { return ''; }
    return categories.filter(function (value) { return typeof value === 'string' && value; }).map(categoryLabel).join('、');
  }

  /* 作者的筆名與短代號：「筆名#短代號」；沒有短代號只顯示筆名，兩者都沒有回傳「—」。 */
  function authorText(item) {
    var it = plainObject(item);
    var name = nonEmptyText(it.author_name);
    if (!name) { return EMPTY; }
    return nonEmptyText(it.author_code) ? name + '#' + it.author_code.trim() : name;
  }

  /* 清單一筆的狀態說明：待審附有疑慮的類別、下架附下架者與類別；其他狀態只有狀態名稱。 */
  function scriptStateText(item) {
    var it = plainObject(item);
    if (it.status === 'pending_review') {
      if (it.review_verdict !== 'flag') { return '待審'; }
      var concern = categoriesText(it.review_categories);
      return concern ? '待審，有疑慮：' + concern : '待審，有疑慮';
    }
    if (it.status === 'removed') {
      var base = it.removed_by === 'admin' ? '管理員下架' : (it.removed_by === 'author' ? '作者自行下架' : '已下架');
      return it.decision_category ? base + '（' + categoryLabel(it.decision_category) + '）' : base;
    }
    if (it.status === 'rejected' && it.decision_category) { return '已退回（' + categoryLabel(it.decision_category) + '）'; }
    return scriptStatusLabel(it.status);
  }

  /* 清單一筆的時間說明：待審是送審時間、上架中是發佈時間、已下架與已退回是決定的時間；時間不明顯示「—」。 */
  function scriptTimeText(item) {
    var it = plainObject(item);
    if (it.status === 'pending_review') { return '送審於 ' + taipeiDateTime(it.submitted_at); }
    if (it.status === 'published') { return '發佈於 ' + taipeiDateTime(it.published_at); }
    if (it.status === 'removed') { return '下架於 ' + taipeiDateTime(it.decided_at); }
    if (it.status === 'rejected') { return '退回於 ' + taipeiDateTime(it.decided_at); }
    return '';
  }

  /* 這一筆能做哪些操作（只決定要不要顯示按鈕；後端仍會逐項驗證）：待審才能核可與退回、上架中且不是內建劇本的才能下架（內建劇本後端回 409；
     「沒有作者」不等於內建：匿名保留的劇本也沒有作者，但它是玩家創作的，管理員可以下架）、
     只有「管理員下架」的才能由管理員恢復（作者自己下架的是作者自己的決定）、上架中且有未結案的檢舉才能結案為「不成立」。 */
  function scriptActions(item) {
    var it = plainObject(item);
    var pending = it.status === 'pending_review';
    return {
      approve: pending,
      reject: pending,
      remove: it.status === 'published' && !it.is_builtin,
      restore: it.status === 'removed' && it.removed_by === 'admin',
      dismiss: it.status === 'published' && typeof it.report_count === 'number' && it.report_count > 0
    };
  }

  // ---- 劇本檢舉（rpg-script-reports）-------------------------------------------------------------------

  /* 清單一筆的檢舉標示：「未結案檢舉 3 則」，有人勾選紅線理由附「（含紅線類別）」；沒有未結案的檢舉回傳空字串。 */
  function reportCountText(item) {
    var it = plainObject(item);
    if (typeof it.report_count !== 'number' || !isFinite(it.report_count) || it.report_count <= 0) { return ''; }
    return '未結案檢舉 ' + Math.floor(it.report_count) + ' 則' + (it.red_line_reported === true ? '（含紅線類別）' : '');
  }

  /* 理由陣列轉一行文字（「、」分隔）；不認得的理由顯示原始代碼（與這裡其他標籤相同），不是陣列回傳空字串。 */
  function reportReasonsText(reasons) {
    if (!Array.isArray(reasons)) { return ''; }
    return reasons.filter(function (value) { return typeof value === 'string' && value; }).map(reportReasonLabel).join('、');
  }

  /* 檢舉面板的各筆（純文字）：`open`（未結案）、`statusText`（「未結案」或「已結案：不成立，時間」）、`createdText`、`reasonsText`、`redLine`（理由含紅線）、`text`（玩家自寫文字，
     不是字串當作沒有）。順序照伺服器給的，不重排；不是陣列或項目不是物件時略過。 */
  function reportItems(reports) {
    if (!Array.isArray(reports)) { return []; }
    return reports.filter(function (item) { return item && typeof item === 'object' && !Array.isArray(item); }).map(function (item) {
      var open = !item.closed_at;
      var reasons = Array.isArray(item.reasons) ? item.reasons : [];
      return {
        open: open,
        statusText: open ? '未結案' : '已結案：' + reportResultLabel(item.closed_result) + '（' + taipeiDateTime(item.closed_at) + '）',
        createdText: '檢舉於 ' + taipeiDateTime(item.created_at),
        reasonsText: reportReasonsText(reasons),
        redLine: reasons.some(function (value) { return RED_LINE_REASONS.indexOf(value) >= 0; }),
        text: typeof item.text === 'string' ? item.text : ''
      };
    });
  }

  /* 各清單的空清單說明。 */
  var SCRIPT_EMPTY = {
    pending_review: '目前沒有待審的劇本。', published: '目前沒有上架中的劇本。', reported: '目前沒有被檢舉的劇本。', removed: '目前沒有已下架的劇本。'
  };
  function scriptEmptyText(view) { return Object.prototype.hasOwnProperty.call(SCRIPT_EMPTY, view) ? SCRIPT_EMPTY[view] : '目前沒有資料。'; }

  /* 分頁標籤上的待審數量：清單還沒讀完時是「至少」（N+）；沒有待審回傳空字串。 */
  function pendingCountText(total, done) {
    if (typeof total !== 'number' || !isFinite(total) || total <= 0) { return ''; }
    return done ? String(Math.floor(total)) : Math.floor(total) + '+';
  }

  /* 退回與下架的類別下拉選單：依後端列舉的順序（先四項紅線、再四項疑慮、最後是人工專用的 other_policy）。 */
  function categoryOptions() {
    return Object.keys(LABELS.category).map(function (value) { return { value: value, label: LABELS.category[value] }; });
  }

  /* 審核備註的字數（NFC 正規化後以 Unicode 字元計，與後端一致；不是字串當作 0）。 */
  function noteLength(text) {
    return typeof text === 'string' ? Array.from(text.normalize('NFC')).length : 0;
  }

  /* 備註的前端檢查（只是少打一次請求）；沒有問題回傳空字串。 */
  function noteProblem(text) {
    return noteLength(text) > MAX_NOTE ? '備註最多 ' + MAX_NOTE + ' 字。' : '';
  }

  /* 內容模式的綁定說明：只有 safe 是綁定安全模式；其餘（adult 與舊值 standard）跟隨玩家的設定。 */
  function tierBindingText(value) {
    return value === 'safe' ? '綁定安全模式' : '未綁定（跟隨玩家的安全模式設定）';
  }

  function characterText(character) {
    var c = plainObject(character);
    var head = '姓名：' + (nonEmptyText(c.name) || EMPTY);
    var age = wholeNumberText(c.age);
    var lines = [age ? head + '　年齡：' + age + ' 歲' : head];
    [['occupation', '職業'], ['personality', '個性'], ['appearance', '外貌'], ['speech_style', '說話風格'], ['hidden_trait', '隱藏個性']].forEach(function (pair) {
      var text = nonEmptyText(c[pair[0]]);
      if (text) { lines.push(pair[1] + '：' + text); }
    });
    return lines.join('\n');
  }

  function milestoneText(milestone) {
    var m = plainObject(milestone);
    var trigger = plainObject(m.trigger);
    var when = label('triggerKind', trigger.kind) + ' ' + (wholeNumberText(trigger.value) || EMPTY);
    var line = '・' + (nonEmptyText(m.title) || EMPTY) + '（' + when + '）';
    var note = nonEmptyText(m.note);
    return note ? line + '\n　備註：' + note : line;
  }

  /* 全文 Modal 的分段：[{ key, label, text }]，text 一律是純文字（以 white-space: pre-wrap 顯示，保留換行），沒有內容的欄位略過。
     content 的形狀不認得（不是物件、欄位型別不對）時盡量取得可讀的部分，不丟例外；不包含封面圖片（v1 不實作）。 */
  function scriptSections(content) {
    var c = plainObject(content);
    var sections = [];
    if (!Object.keys(c).length) { return sections; }
    function add(key, text, title) { if (text) { sections.push({ key: key, label: title, text: text }); } }
    var tags = Array.isArray(c.tags) ? c.tags.filter(function (tag) { return nonEmptyText(tag); }).join('、') : '';
    var calendar = plainObject(c.calendar);
    var calendarText = nonEmptyText(calendar.name);
    var startYear = wholeNumberText(calendar.start_year);
    if (calendarText && startYear) { calendarText += '，起始年 ' + startYear; }
    var ending = '';
    if (c.ending_mode === 'custom') {
      var endingAge = wholeNumberText(c.ending_age);
      ending = '自訂結局' + (endingAge ? '（壽終年齡 ' + endingAge + ' 歲）' : '');
      if (nonEmptyText(c.ending_text)) { ending += '\n' + nonEmptyText(c.ending_text); }
    } else if (c.ending_mode) {
      ending = label('endingMode', c.ending_mode);
    }
    var characters = Array.isArray(c.characters) ? c.characters : [];
    var locations = Array.isArray(c.locations) ? c.locations.filter(function (place) { return nonEmptyText(place); }).join('、') : '';
    var milestones = Array.isArray(c.milestones) ? c.milestones : [];

    add('summary', nonEmptyText(c.summary), '簡介');
    add('world_name', nonEmptyText(c.world_name), '世界名稱');
    add('tags', tags, '標籤');
    add('audience', audienceLabel(c.audience || 'all'), '讀者頻道');
    add('content_tier', c.content_tier ? tierBindingText(c.content_tier) : '', '內容模式');
    add('calendar', calendarText, '曆法');
    add('background', nonEmptyText(c.background), '背景與體系');
    add('rules', nonEmptyText(c.rules), '世界規則（狀態轉移規則）');
    add('ending', ending, '結局');
    add('opening', nonEmptyText(c.opening), '初始對話');
    add('characters', characters.map(characterText).join('\n\n'), '角色（' + characters.length + '）');
    add('locations', locations, '地點');
    add('milestones', milestones.map(milestoneText).join('\n'), '里程碑（' + milestones.length + '）');
    return sections;
  }

  return {
    LABELS: LABELS, EMPTY: EMPTY, WARN_RATIO: WARN_RATIO, FULL_RATIO: FULL_RATIO,
    parseTime: parseTime, taipeiDate: taipeiDate, taipeiDateTime: taipeiDateTime, taipeiClock: taipeiClock, shortDate: shortDate,
    statusLabel: statusLabel, tierLabel: tierLabel, roleLabel: roleLabel, actionLabel: actionLabel, kindLabel: kindLabel,
    messageRoleLabel: messageRoleLabel, messageStatusLabel: messageStatusLabel, lockReasonLabel: lockReasonLabel,
    bucketLabel: bucketLabel, signedCount: signedCount, parseCoinDelta: parseCoinDelta,
    ratioPercent: ratioPercent, ratioLevel: ratioLevel, ratioWidth: ratioWidth, count: count, shortId: shortId,
    suspensionText: suspensionText, auditDetailText: auditDetailText,
    MAX_NOTE: MAX_NOTE, scriptStatusLabel: scriptStatusLabel, removerLabel: removerLabel, categoryLabel: categoryLabel,
    verdictLabel: verdictLabel, audienceLabel: audienceLabel, categoriesText: categoriesText, authorText: authorText,
    scriptStateText: scriptStateText, scriptTimeText: scriptTimeText, scriptActions: scriptActions, pendingCountText: pendingCountText,
    RED_LINE_REASONS: RED_LINE_REASONS, reportReasonLabel: reportReasonLabel, reportResultLabel: reportResultLabel, reportCountText: reportCountText,
    reportReasonsText: reportReasonsText, reportItems: reportItems, scriptEmptyText: scriptEmptyText,
    categoryOptions: categoryOptions, noteLength: noteLength, noteProblem: noteProblem, tierBindingText: tierBindingText,
    scriptSections: scriptSections
  };
});
