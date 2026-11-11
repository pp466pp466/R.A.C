/* RPG 入口畫面的顯示對照與路徑組裝（純函式與純資料，與框架無關，Node 可以直接測試；見 rpg-entry-pages 規格與 rpg-entry-ui 的 design D5）。
 *
 * 後端只回列舉值（`safe`、`male`、`native`…），顯示用的繁體中文集中在這裡，沿用 `script-draft.js` 的做法。
 * - 所有 `*Label(value)` 對**未知值**一律退回固定的預設文字，不把原始值顯示給玩家（也避免把 `__proto__`、`constructor` 這類鍵當成對照）。
 * - `gateNotice(gate)`：帳號閘門（`AccessPolicy.chat_gate` 的結果）的提示文字與連結；未知的非空代碼一律當成「不可開始」（失敗時關閉），不放行。
 * - `contentModeNotice(mode)`：說明「為什麼沒看到某些劇本」。只有說明，沒有任何改變模式的動作：成年玩家的模式較保守時只提醒可到帳號設定調整，
 *   不自動調高；未滿成年門檻者固定為安全模式，只說明原因。
 * - `startPath(id)`、`sessionPath(id)`：站內路徑，識別碼經 encodeURIComponent。
 * - `formatTaipeiTime(iso)`：伺服器的 UTC 時間（`2026-10-27T12:00:00Z`）轉成台灣時間 `YYYY-MM-DD HH:mm`；台灣沒有日光節約時間，固定加 8 小時，不依賴瀏覽器的時區。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    Object.keys(api).forEach(function (key) { root.Game[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MEMBER_LABELS = { free: '一般', plus: '進階', vip: '尊榮' };
  var TIER_LABELS = { safe: '安全模式', standard: '一般模式', adult: '成人模式' };
  var AUDIENCE_LABELS = { all: '全讀者', male: '男頻', female: '女頻' };
  var ORIGIN_LABELS = { native: '本地人', transmigrator: '穿越者' };
  var STATUS_LABELS = { active: '進行中', ended: '已完結' };
  /* 審查類別：後端只回固定的列舉識別字（不含內容的位置或文字）。前四項是會被直接拒絕的紅線，後四項只會進人工。 */
  var CATEGORY_LABELS = {
    minor_sexual: '涉及未成年角色的性內容',
    sexual_violence: '性暴力',
    non_consensual: '非自願的性情節',
    real_person_sexual: '以真人名義的性內容',
    minor_suspected: '疑似涉及未成年角色',
    real_person_suspected: '疑似涉及真人',
    tier_mismatch: '內容與宣告的內容模式不符',
    other_concern: '其他疑慮'
  };

  var DEFAULTS = {
    member: '一般', tier: '未標示', audience: '全讀者', origin: '本地人', status: '進行中', category: '其他疑慮'
  };

  var ACCOUNT_PATH = '/account';
  var ACCOUNT_LINK_TEXT = '前往帳號設定';
  var GATE_NOTICES = {
    age_confirmation_required: { text: '請先到帳號設定填寫生日並選擇年齡區間，才能開始冒險。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT },
    age_restricted: { text: '您的年齡目前不符合使用這項功能的規定，所以無法開始冒險。', link: null, linkText: '' },
    forbidden: { text: '您的帳號目前無法開始冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT }
  };
  var GATE_FALLBACK = { text: '目前無法開始冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT };

  function has(map, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key); }
  function labelOf(map, value, fallback) { return has(map, value) ? map[value] : fallback; }
  function copy(notice) { return { text: notice.text, link: notice.link, linkText: notice.linkText }; }

  function memberLabel(value) { return labelOf(MEMBER_LABELS, value, DEFAULTS.member); }
  function tierLabel(value) { return labelOf(TIER_LABELS, value, DEFAULTS.tier); }
  function audienceLabel(value) { return labelOf(AUDIENCE_LABELS, value, DEFAULTS.audience); }
  function originLabel(value) { return labelOf(ORIGIN_LABELS, value, DEFAULTS.origin); }
  function statusLabel(value) { return labelOf(STATUS_LABELS, value, DEFAULTS.status); }
  function categoryLabel(value) { return labelOf(CATEGORY_LABELS, value, DEFAULTS.category); }

  /* 閘門為空（可使用）回傳 null；否則回傳 { text, link, linkText }（link 可為 null，例如年齡不符時補填也沒有用）。 */
  function gateNotice(gate) {
    if (typeof gate !== 'string' || gate === '') { return null; }
    return copy(has(GATE_NOTICES, gate) ? GATE_NOTICES[gate] : GATE_FALLBACK);
  }

  /* `mode` 是目錄回應的 `content_mode`（{ effective, is_adult }）。沒有需要說明時回傳 null。 */
  function contentModeNotice(mode) {
    if (!mode || typeof mode !== 'object') { return null; }
    if (mode.is_adult === false) {
      return { text: '未滿 18 歲固定使用安全模式，所以部分劇本不會顯示；滿 18 歲後這項限制會自動解除。', link: null, linkText: '' };
    }
    if (mode.effective === 'safe') {
      return { text: '您目前使用安全模式，所以部分劇本不會顯示。想調整內容模式，可以到帳號設定。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT };
    }
    return null;
  }

  function startPath(versionId) { return '/game/start/' + encodeURIComponent(versionId); }
  function sessionPath(sessionId) { return '/game/' + encodeURIComponent(sessionId); }

  function pad(number) { return number < 10 ? '0' + number : String(number); }

  function formatTaipeiTime(iso) {
    if (typeof iso !== 'string') { return ''; }
    var ms = Date.parse(iso);
    if (!isFinite(ms)) { return ''; }
    var date = new Date(ms + 8 * 60 * 60 * 1000);
    return date.getUTCFullYear() + '-' + pad(date.getUTCMonth() + 1) + '-' + pad(date.getUTCDate()) + ' ' + pad(date.getUTCHours()) + ':' + pad(date.getUTCMinutes());
  }

  return {
    MEMBER_LABELS: MEMBER_LABELS,
    TIER_LABELS: TIER_LABELS,
    AUDIENCE_LABELS: AUDIENCE_LABELS,
    ORIGIN_LABELS: ORIGIN_LABELS,
    STATUS_LABELS: STATUS_LABELS,
    CATEGORY_LABELS: CATEGORY_LABELS,
    memberLabel: memberLabel,
    tierLabel: tierLabel,
    audienceLabel: audienceLabel,
    originLabel: originLabel,
    statusLabel: statusLabel,
    categoryLabel: categoryLabel,
    gateNotice: gateNotice,
    contentModeNotice: contentModeNotice,
    startPath: startPath,
    sessionPath: sessionPath,
    formatTaipeiTime: formatTaipeiTime
  };
});
