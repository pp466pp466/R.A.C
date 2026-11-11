/* RPG 入口畫面的顯示對照與路徑組裝（純函式與純資料，與框架無關，Node 可以直接測試；見 rpg-entry-pages 規格與 rpg-entry-ui 的 design D5）。
 *
 * 後端只回列舉值（`safe`、`male`、`native`…），顯示用的繁體中文集中在這裡，沿用 `script-draft.js` 的做法。
 * - 所有 `*Label(value)` 對**未知值**一律退回固定的預設文字，不把原始值顯示給玩家（也避免把 `__proto__`、`constructor` 這類鍵當成對照）。
 * - `gateNotice(gate)`：帳號閘門（`AccessPolicy.chat_gate` 的結果）的提示文字與連結；未知的非空代碼一律當成「不可開始」（失敗時關閉），不放行。
 * - `contentModeNotice(mode)`：說明「為什麼沒看到某些劇本」。只有說明，沒有任何改變模式的動作：成年玩家的模式較保守時只提醒可到帳號設定調整，
 *   不自動調高；未滿成年門檻者固定為安全模式，只說明原因。
 * - `startPath(id)`、`sessionPath(id)`：站內路徑，識別碼經 encodeURIComponent。
 * - `formatTaipeiTime(iso)`：伺服器的 UTC 時間（`2026-10-27T12:00:00Z`）轉成台灣時間 `YYYY-MM-DD HH:mm`；台灣沒有日光節約時間，固定加 8 小時，不依賴瀏覽器的時區。
 * 遊戲主畫面（rpg-game-ui）另加：回合與串流結束狀態的名稱、轉場與中斷說明、串流 `error` 事件的訊息、歷史裡未完成回合的那一行說明、
 * 記事簿與簽到用的固定文字。這些文字**不含任何玩家文字**；不認得的值一律退回固定文字（失敗時關閉：不當成有內容的回合）。
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

  /* ---- 遊戲主畫面（rpg-game-ui）：串流結束狀態、轉場與中斷說明、記事簿與簽到的固定文字 ----
     這些文字只來自這裡，**不含任何玩家文字**（行動、劇情、記事簿內容、角色名都不會被帶進來）；數字只有字數、幣數這類整數。
     與後端列舉的鍵對照由 tests/test_game_pages.py 守住（回合狀態、串流錯誤代碼）。 */

  /* 回合的狀態（`turn_view.status`）。 */
  var TURN_STATUS_LABELS = { streaming: '進行中', complete: '已完成', blocked: '已轉場', interrupted: '已中斷' };
  /* `done` 事件的結束狀態。 */
  var DONE_STATUS_LABELS = { complete: '已完成', blocked: '劇情轉了個彎', ended: '故事已完結' };
  /* 串流客戶端的結果（turn-stream.js 的 outcome）。 */
  var STREAM_OUTCOME_LABELS = {
    done: '已完成', error_event: '生成失敗', interrupted: '連線中斷', timeout_idle: '等待逾時', timeout_total: '總時間逾時', aborted: '已取消', rejected: '無法開始'
  };
  var SYNC_REMINDER = '這一輪可能已在伺服器完成，已為你重新同步；遊戲幣與歷史以伺服器為準。';
  /* 連線在 `done` 之前結束的說明：前端中止不代表後端中止，所以每一則都提醒「可能已在伺服器完成」。 */
  var STREAM_OUTCOME_NOTICES = {
    interrupted: '連線在這一輪完成之前中斷了。' + SYNC_REMINDER,
    timeout_idle: '等了太久都沒有收到伺服器的回應，已中止這一輪。' + SYNC_REMINDER,
    timeout_total: '這一輪花的時間比預期久，已中止。' + SYNC_REMINDER,
    aborted: '已取消這一輪。' + SYNC_REMINDER
  };
  /* 歷史裡沒有完成的回合只顯示一行說明（不含任何部分內容）。 */
  var HISTORY_LINES = {
    blocked: '劇情在這裡轉了個方向，這一輪沒有留下內容。',
    interrupted: '這一輪在完成之前中斷了，沒有留下內容。',
    streaming: '另一個回合正在進行中，完成後會出現在這裡。'
  };
  /* 串流中的 `error` 事件（後端 `ERROR_MESSAGES` 的代碼）。 */
  var STREAM_ERROR_MESSAGES = {
    generation_timeout: '故事生成逾時了，這一輪沒有扣除遊戲幣，請再試一次。',
    generation_failed: '故事暫時無法生成，這一輪沒有扣除遊戲幣，請稍後再試。',
    turn_superseded: '這個回合已經被新的嘗試取代，已為你重新同步。',
    opening_superseded: '故事的開場已經由另一次嘗試完成，已為你重新同步。',
    internal_error: '發生了預期之外的問題，這一輪沒有扣除遊戲幣，請稍後再試。'
  };
  var STREAM_ERROR_FALLBACK = '故事暫時無法生成，這一輪沒有扣除遊戲幣，請稍後再試。';
  var STREAM_UNKNOWN_NOTICE = '這一輪沒有順利完成。' + SYNC_REMINDER;
  var BLOCKED_HINT = '這一輪沒有繼續下去，沒有扣除遊戲幣。換個行動再試試看。';

  var NOTEBOOK_TEXTS = {
    empty: '這本記事簿還是空的。每完成 10 輪，AI 會把故事的重點記在這裡。',
    full: '記事簿已滿：之後的設定不再被記住，AI 可能會遺忘先前的內容。可以整理內容，或升級會員以取得更大的容量。',
    full_edit: '目前的長度已超過容量，須一次刪到容量以內才能儲存，AI 才會恢復更新。',
    reserve: '預備空間只供 AI 寫入，你自己編輯時不能使用。',
    conflict: 'AI 或另一個裝置剛更新了記事簿，已顯示最新內容；你剛才編輯的內容還保留著，可以對照後再儲存。',
    ended: '這個存檔已經完結，記事簿成為唯讀。',
    network: '連線不穩，記事簿沒有儲存；你編輯的內容還在，請稍後再試。',
    not_found: '找不到這個存檔，它可能已被刪除。',
    rejected: '記事簿的內容未通過內容規範，無法儲存。請修改後再試。',
    invalid: '記事簿的內容有問題，請檢查後再儲存。',
    saved: '記事簿已儲存。',
    unavailable: '記事簿暫時無法使用，請稍後再試。'
  };
  var NOTEBOOK_FALLBACK = '記事簿暫時無法使用，請稍後再試。';

  var CHECKIN_TEXTS = {
    done: '簽到成功，今天的遊戲幣已入帳。',
    already_default: '今天已經簽到過了，明天 00:00（台灣時間）後可以再簽到。',
    unavailable: '簽到暫時無法使用，請稍後再試。'
  };

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

  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  /* 回合狀態的名稱；不認得的值當成「已中斷」（失敗時關閉：不把它當成有內容的回合）。 */
  function turnStatusLabel(value) { return labelOf(TURN_STATUS_LABELS, value, TURN_STATUS_LABELS.interrupted); }
  function doneStatusLabel(value) { return labelOf(DONE_STATUS_LABELS, value, '這一輪已結束'); }
  function streamOutcomeLabel(value) { return labelOf(STREAM_OUTCOME_LABELS, value, '未完成'); }

  /* 串流結束時對玩家的說明：完成（`done`）沒有說明；連線中斷、逾時、取消各有一則（都提醒「可能已在伺服器完成」）；其他一律是通用說明。 */
  function streamOutcomeNotice(outcome) {
    if (outcome === 'done') { return ''; }
    return labelOf(STREAM_OUTCOME_NOTICES, outcome, STREAM_UNKNOWN_NOTICE);
  }

  /* 歷史裡沒有完成的回合那一行說明；不認得的狀態用「中斷」的那一行（不顯示任何部分內容）。 */
  function historyLine(status) { return labelOf(HISTORY_LINES, status, HISTORY_LINES.interrupted); }

  /* `error` 事件的訊息：只認後端列舉的代碼，不認得的代碼退回通用文字（不顯示伺服器送來的原文）。 */
  function streamErrorMessage(code) { return labelOf(STREAM_ERROR_MESSAGES, code, STREAM_ERROR_FALLBACK); }

  function blockedHint() { return BLOCKED_HINT; }

  function notebookText(key) { return labelOf(NOTEBOOK_TEXTS, key, NOTEBOOK_FALLBACK); }

  /* 用量「1800 / 2000」；任一數字不是非負整數時顯示「—」。 */
  function notebookUsage(length, capacity) { return isCount(length) && isCount(capacity) ? length + ' / ' + capacity : '—'; }

  function notebookTooLongNotice(length, capacity) {
    if (!isCount(length) || !isCount(capacity)) { return '記事簿超過容量，請刪減到容量以內再儲存。'; }
    return '記事簿超過容量（' + length + ' / ' + capacity + ' 字），請刪減到容量以內再儲存。';
  }

  function checkinText(key) { return labelOf(CHECKIN_TEXTS, key, CHECKIN_TEXTS.unavailable); }

  /* 簽到後免費幣會超過上限：預覽會入帳多少、會作廢多少（整數）。 */
  function checkinOverflowNotice(gain, lose) {
    if (!isCount(gain) || !isCount(lose)) { return '領取後免費遊戲幣會超過上限，超出的部分將作廢。要繼續領取嗎？'; }
    return '領取後免費遊戲幣會超過上限：會入帳 ' + gain + ' 幣，超出上限的 ' + lose + ' 幣將作廢。要繼續領取嗎？';
  }

  /* 今天已簽到：有下次可簽到的時間（UTC 的 ISO 字串）就轉成台灣時間顯示，沒有就用固定說明。 */
  function checkinAlreadyNotice(nextResetAt) {
    var when = formatTaipeiTime(nextResetAt);
    return when ? '今天已經簽到過了，下次可簽到的時間是 ' + when + '（台灣時間）。' : CHECKIN_TEXTS.already_default;
  }

  return {
    TURN_STATUS_LABELS: TURN_STATUS_LABELS,
    DONE_STATUS_LABELS: DONE_STATUS_LABELS,
    STREAM_OUTCOME_LABELS: STREAM_OUTCOME_LABELS,
    STREAM_OUTCOME_NOTICES: STREAM_OUTCOME_NOTICES,
    HISTORY_LINES: HISTORY_LINES,
    STREAM_ERROR_MESSAGES: STREAM_ERROR_MESSAGES,
    NOTEBOOK_TEXTS: NOTEBOOK_TEXTS,
    CHECKIN_TEXTS: CHECKIN_TEXTS,
    turnStatusLabel: turnStatusLabel,
    doneStatusLabel: doneStatusLabel,
    streamOutcomeLabel: streamOutcomeLabel,
    streamOutcomeNotice: streamOutcomeNotice,
    historyLine: historyLine,
    streamErrorMessage: streamErrorMessage,
    blockedHint: blockedHint,
    notebookText: notebookText,
    notebookUsage: notebookUsage,
    notebookTooLongNotice: notebookTooLongNotice,
    checkinText: checkinText,
    checkinOverflowNotice: checkinOverflowNotice,
    checkinAlreadyNotice: checkinAlreadyNotice,
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
