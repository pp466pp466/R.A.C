/* 劇本版本的狀態說明（純函式，與框架無關，Node 可以直接測試；見 script-takedown 規格的「作者看得到審核結果與下架狀態」）。
 *
 * - `statusText(version)`：版本列上標籤的文字。「已下架」依下架者分成「已下架（作者）」與「已被管理員下架」。
 * - `versionNotice(version)`：版本列下方的說明——被退回附類別與時間；下架附下架者，管理員下架時附類別與「無法自行恢復」的引導。
 * - `readOnlyNotice(version)`：詳細檢視（內容唯讀）的橫幅文案。
 * - `canTakedown(version)`、`canRestore(version)`：只決定要不要顯示按鈕；後端仍會逐項驗證。管理員下架的版本沒有「恢復上架」。
 * - `showsSupportLink(version)`：管理員下架的版本旁顯示「前往客服」連結（導向客服的意見回饋；申訴頁一般作者進不去）。
 * - `actionErrorText(info)`：下架與恢復的 409／403 錯誤代碼的繁體中文說明；不認得的代碼回空字串，由呼叫端用伺服器的訊息。
 * - `latestVersion(script)`、`hasOpenVersion(script)`、`canStartOver(script, version)`、`startOverText(version)`：「重新來過」的按鈕——
 *   被退回、已上架或被下架的版本內容凍結，要修改只能由最新版本建立新草稿；按鈕只出現在最新版本上、且劇本沒有進行中的版本（使用者驗收時回報：
 *   被退回後在「檢視」畫面找不到出路，2026-11-20）。
 *
 * 資料來源只有版本摘要的 `status`、`decision_category`、`decided_at`、`removed_by`；**審核備註不會出現在任何一個欄位**（只有管理員看得到，
 * 後端的鍵集合測試固定了這件事），所以這裡也沒有備註可以顯示。所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 * 類別與狀態的標籤沿用 `script-draft.js`（先載入）；新增類別時要同步 `app/models/script.py` 的 `DECISION_CATEGORIES`（有測試對照）。
 */
(function (root, factory) {
  'use strict';
  var labels = typeof module === 'object' && module.exports ? require('./script-draft.js') : root.Scripts;
  var api = factory(labels);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Scripts = root.Scripts || {};
    Object.keys(api).forEach(function (key) { root.Scripts[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function (Labels) {
  'use strict';

  var REMOVER_LABELS = { author: '作者', admin: '管理員' };
  var TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;     // 台灣沒有日光節約時間，固定 UTC+8

  function has(table, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key); }
  function plain(value) { return value && typeof value === 'object' ? value : {}; }
  function pad(number) { return (number < 10 ? '0' : '') + number; }

  /* 伺服器的時間字串一律帶時區；萬一沒有，視為 UTC，而不是瀏覽器的本地時間。無法解析回空字串。 */
  function timeText(text) {
    if (typeof text !== 'string' || !text) { return ''; }
    var value = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text + 'Z';
    var ms = Date.parse(value);
    if (isNaN(ms)) { return ''; }
    var d = new Date(ms + TAIPEI_OFFSET_MS);
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
  }

  function categoryName(category) { return has(Labels.CATEGORY_LABELS, category) ? Labels.CATEGORY_LABELS[category] : ''; }

  /* 括號裡的補充：類別與時間，缺的就不寫（不補猜測的內容）。 */
  function detail(version, withCategory) {
    var parts = [];
    var name = withCategory ? categoryName(version.decision_category) : '';
    var when = timeText(version.decided_at);
    if (name) { parts.push(name); }
    if (when) { parts.push(when); }
    return parts.length ? '（' + parts.join('，') + '）' : '';
  }

  function statusText(version) {
    var v = plain(version);
    if (v.status === 'removed') {
      if (v.removed_by === 'admin') { return '已被管理員下架'; }
      if (v.removed_by === 'author') { return '已下架（' + REMOVER_LABELS.author + '）'; }
      return Labels.STATUS_LABELS.removed;
    }
    return has(Labels.STATUS_LABELS, v.status) ? Labels.STATUS_LABELS[v.status] : String(v.status || '');
  }

  /* 管理員下架的引導：申訴頁只開放給被鎖定或被暫停發佈的帳號（AGENTS.md 第 10 節），一般作者進不去，所以導向客服的意見回饋
     （使用者 2026-11-20 決定）：客服會引導留下紀錄，管理員在後台看得到；不承諾處理結果。 */
  var REUSE_HINT = '如有疑問，可以到客服（/cs）以意見回饋留下紀錄，管理員會在後台看到；也可以由最新版本建立新草稿，修改內容後重新送審。';

  function versionNotice(version) {
    var v = plain(version);
    if (v.status === 'rejected') {
      return '這個版本已被退回' + detail(v, true) + '。請由最新版本建立新草稿，修改內容後重新送審。';
    }
    if (v.status === 'removed' && v.removed_by === 'admin') {
      return '管理員已下架這個版本' + detail(v, true) + '，無法自行恢復上架。' + REUSE_HINT;
    }
    if (v.status === 'removed') {
      return '你已下架這個版本' + detail(v, false) + '：不再出現在公開目錄，也不能用它開新存檔，已開始的存檔不受影響。可以隨時恢復上架。';
    }
    return '';
  }

  /* 詳細檢視的橫幅：既有狀態的原文不變，新增已上架、已被退回、已下架與已被取代的說明。 */
  function readOnlyNotice(version) {
    var v = plain(version);
    if (v.status === 'draft' || !v.status) { return ''; }
    if (v.status === 'pending_review') { return '這個版本已經送審，內容凍結中；要修改請先撤回。'; }
    if (v.status === 'published') { return '這個版本已上架，內容不可修改；要修改請回到列表，由最新版本建立新草稿。'; }
    if (v.status === 'superseded') { return '這個版本已被新的版本取代，內容不可修改。'; }
    var notice = versionNotice(v);
    return notice || '這個版本已經離開草稿，內容不可修改；要修改請回到列表建立新草稿。';
  }

  /* 重新來過：版本被退回（或已上架、被下架）之後內容是凍結的，作者要修改只能由「最新版本」建立新草稿（後端同一個劇本同時只允許
     一個進行中的版本：草稿或待審）。這幾個純函式決定要不要顯示按鈕與寫什麼字；後端仍會逐項驗證。 */
  function latestVersion(script) {
    var versions = plain(script).versions;
    if (!Array.isArray(versions) || !versions.length) { return null; }
    return versions.reduce(function (best, item) { return plain(item).version_no > plain(best).version_no ? item : best; });
  }

  function hasOpenVersion(script) {
    var versions = plain(script).versions;
    return Array.isArray(versions) && versions.some(function (item) { var s = plain(item).status; return s === 'draft' || s === 'pending_review'; });
  }

  /* 按鈕只出現在劇本的「最新版本」上（按下去是由最新版本建立新草稿，放在較舊的版本上會讓人以為是用那個版本的內容），
     而且該版本已離開草稿與待審（草稿直接編輯、待審先撤回），劇本也沒有進行中的版本。 */
  function canStartOver(script, version) {
    var v = plain(version);
    var latest = latestVersion(script);
    if (!latest || latest.id !== v.id || !v.id) { return false; }
    if (v.status === 'draft' || v.status === 'pending_review' || !v.status) { return false; }
    return !hasOpenVersion(script);
  }

  /* 被退回的用「修改後重新送審」（玩家想做的事），其餘（已上架、已下架）用「修改並送審新版本」；括號說明按下去會發生什麼。 */
  function startOverText(version) {
    return plain(version).status === 'rejected' ? '修改後重新送審（建立新草稿）' : '修改並送審新版本（建立新草稿）';
  }

  function canTakedown(version) { return plain(version).status === 'published'; }
  function canRestore(version) { var v = plain(version); return v.status === 'removed' && v.removed_by === 'author'; }
  /* 管理員下架的版本旁才放「前往客服」連結（說明文字裡已有同樣的引導；連結只是讓人不必自己找路徑）。 */
  function showsSupportLink(version) { var v = plain(version); return v.status === 'removed' && v.removed_by === 'admin'; }

  var ACTION_ERRORS = {
    not_published: '這個版本已經不是上架中，無法下架。列表已重新載入。',
    not_removed: '這個版本已經不是下架狀態，無法恢復上架（可能已被新的版本取代）。列表已重新載入。',
    removal_locked: '這個版本是管理員下架的，無法自行恢復上架。' + REUSE_HINT,
    live_exists: '這個劇本已經有另一個上架中的版本，無法恢復這個版本。'
  };

  function actionErrorText(info) {
    var code = plain(info).code;
    return has(ACTION_ERRORS, code) ? ACTION_ERRORS[code] : '';
  }

  return {
    REMOVER_LABELS: REMOVER_LABELS, ACTION_ERRORS: ACTION_ERRORS,
    timeText: timeText, statusText: statusText, versionNotice: versionNotice, readOnlyNotice: readOnlyNotice,
    canTakedown: canTakedown, canRestore: canRestore, showsSupportLink: showsSupportLink, actionErrorText: actionErrorText,
    latestVersion: latestVersion, hasOpenVersion: hasOpenVersion, canStartOver: canStartOver, startOverText: startOverText
  };
});
