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
      'user.lookup': '查詢使用者', 'user.suspend': '停權', 'user.unsuspend': '解除停權', 'user.change_tier': '調整會員等級',
      'conversation.read': '讀取對話', 'conversation.delete': '刪除對話', 'failures.read': '查看模型失敗原文',
      'reauth.failed': '重新輸入密碼失敗',
      'limit.set': '修改營運限制', 'publish.suspend': '暫停發佈', 'publish.lift': '解除暫停發佈', 'wallet.adjust': '調整遊戲幣',
      'account.lock': '帳號自動鎖定', 'appeal.submit': '玩家送出申訴', 'appeal.read': '檢視申訴', 'account.unlock': '解除帳號鎖定'
    },
    /* 鎖定原因（app/models/lock_strike.py 的計次類別，也是 users.lock_reason 的值） */
    lockReason: { identity_rejected: '登入或年齡確認多次被拒', rate_limited: '短時間內訊息過多' },
    kind: { chat: '客服對話', summary: '對話摘要', script_review: '劇本審查', protagonist_gen: '主角生成', protagonist_review: '主角審查' },
    messageRole: { user: '使用者', assistant: '客服' },
    messageStatus: { ok: '正常', fallback: '降級回覆' },
    via: { email: '以信箱', id: '以識別碼' },
    /* 遊戲幣的兩個餘額（app/models/wallet.py 的 BUCKETS） */
    bucket: { free: '免費幣', paid: '付費幣' }
  };

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
    return '';
  }

  return {
    LABELS: LABELS, EMPTY: EMPTY, WARN_RATIO: WARN_RATIO, FULL_RATIO: FULL_RATIO,
    parseTime: parseTime, taipeiDate: taipeiDate, taipeiDateTime: taipeiDateTime, taipeiClock: taipeiClock, shortDate: shortDate,
    statusLabel: statusLabel, tierLabel: tierLabel, roleLabel: roleLabel, actionLabel: actionLabel, kindLabel: kindLabel,
    messageRoleLabel: messageRoleLabel, messageStatusLabel: messageStatusLabel, lockReasonLabel: lockReasonLabel,
    bucketLabel: bucketLabel, signedCount: signedCount, parseCoinDelta: parseCoinDelta,
    ratioPercent: ratioPercent, ratioLevel: ratioLevel, ratioWidth: ratioWidth, count: count, shortId: shortId,
    suspensionText: suspensionText, auditDetailText: auditDetailText
  };
});
