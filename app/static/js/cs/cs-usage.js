/* 客服用量的顯示邏輯（與框架無關的純函式；Node 可以直接測試）。
 *
 * 資料來自 GET /api/cs/usage（只有呼叫者自己的用量）：
 *   { tier, daily: { used, limit, resets_at }, per_minute_limit, conversations: { count, limit }, suspended_until }
 * 這裡只決定「怎麼顯示」，真正的限制永遠在後端；顯示不一致（例如另一台裝置剛用掉一輪）不會造成任何問題，
 * 因為送出時後端仍會拒絕，前端再依錯誤代碼處理。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.CS = root.CS || {};
    root.CS.usage = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;     // 台灣沒有夏令時間，固定 UTC+8，不依賴 Intl 的時區資料

  /* 暫時性的拒絕（cs-sync.js 歸類為「稍後再試」）：訊息保留、稍後自動重送，這是給使用者看的說明。
     「短時間內不會成功」的代碼（今日額度用完、對話暫停、對話數已滿）由 cs-sync.js 歸類為無法送出，說明直接用伺服器的文案。 */
  var QUEUED_NOTICES = {
    rate_limited: '您傳送訊息的速度太快了，這則已保留，稍後會自動送出。',
    system_busy: '目前使用人數較多，這則已保留，稍後會自動送出。'
  };

  function pad(number) { return (number < 10 ? '0' : '') + number; }

  /* 台灣時間的 HH:MM。 */
  function taipeiClock(ms) {
    var moment = new Date(ms + TAIPEI_OFFSET_MS);
    return pad(moment.getUTCHours()) + ':' + pad(moment.getUTCMinutes());
  }

  function parseTime(text) {
    if (typeof text !== 'string') { return null; }
    var ms = Date.parse(text);
    return isNaN(ms) ? null : ms;
  }

  /* 今日還剩幾輪；資料不完整回傳 null（呼叫端據此隱藏顯示）。 */
  function remainingTurns(usage) {
    var daily = usage && usage.daily;
    if (!daily || typeof daily.used !== 'number' || typeof daily.limit !== 'number') { return null; }
    return Math.max(0, daily.limit - daily.used);
  }

  /* Sidebar 底部那一行；沒有資料時是空字串（整行隱藏）。 */
  function remainingText(usage) {
    var remaining = remainingTurns(usage);
    if (remaining === null) { return ''; }
    return remaining === 0 ? '今日額度已用完' : '今日剩餘 ' + remaining + ' 輪';
  }

  /* 目前是否在暫停期間；是的話回傳 { untilMs, text }，否則 null。 */
  function suspension(usage, nowMs) {
    var until = parseTime(usage && usage.suspended_until);
    if (until === null || until <= nowMs) { return null; }
    return { untilMs: until, text: '客服對話已暫停，將於 ' + taipeiClock(until) + '（台灣時間）恢復。' };
  }

  function queuedNotice(code) { return QUEUED_NOTICES[code] || null; }

  return {
    taipeiClock: taipeiClock, remainingTurns: remainingTurns, remainingText: remainingText, suspension: suspension,
    queuedNotice: queuedNotice
  };
});
