/* 帳號頁「匯出我的資料」與「刪除帳號」的純函式（與框架無關，Node 可以直接測試）。
 *
 * 在瀏覽器中，載入後會把函式放到全域的 `Auth.lifecycle`。這裡只決定「怎麼顯示、什麼時候可以送出」；
 * 真正的確認本人、狀態轉換與寬限期永遠在後端判斷。確認文字由伺服器放進頁面的初始狀態（不在這裡寫死第二份），
 * 所有回傳值都是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Auth = root.Auth || {};
    root.Auth.lifecycle = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;     // 台灣沒有夏令時間，固定 UTC+8，不依賴 Intl 的時區資料
  var EXPORT_TIMEOUT_MS = 60000;                 // 匯出可能較大，比一般請求的 15 秒長
  var EXPORT_FORMATS = {
    json: { label: 'JSON', extension: 'json', fallbackName: 'my-data.json' },
    csv: { label: 'CSV（zip）', extension: 'zip', fallbackName: 'my-data.zip' }
  };

  /* 錯誤代碼對應的說明；與後端的訊息一致。後端回應若沒有帶訊息（例如下載失敗時的檔案本文）就用這裡的。 */
  var ERROR_MESSAGES = {
    grace_expired: '撤銷期限（申請後 7 天）已過，帳號即將被刪除，無法撤銷。',
    not_pending: '您的帳號沒有申請刪除，不需要撤銷。',
    deletion_not_allowed: '目前的帳號狀態無法申請刪除。',
    admin_delete_forbidden: '管理員帳號無法自行刪除。',
    account_deletion_pending: '您的帳號正在等待刪除。如要保留帳號，請先撤銷刪除。'
  };

  /* NFC 正規化並去除前後空白（含全形空白）後，是否與要求的確認文字完全相同。 */
  function matchesConfirmText(typed, expected) {
    if (typeof typed !== 'string' || typeof expected !== 'string' || !expected) { return false; }
    var normalized = typeof typed.normalize === 'function' ? typed.normalize('NFC') : typed;
    return normalized.trim() === expected;
  }

  /* 刪除帳號的送出條件：有密碼的帳號要輸入密碼；其餘要輸入完全相同的確認文字。 */
  function canSubmitDeletion(options) {
    if (options.hasPassword) { return typeof options.password === 'string' && options.password.length > 0; }
    return matchesConfirmText(options.confirmText, options.expectedText);
  }

  /* 送出請求的本文：只帶對應的那一個欄位（伺服器也只看對應的欄位）。 */
  function deletionBody(options) {
    return options.hasPassword ? { password: options.password } : { confirm_text: options.confirmText };
  }

  /* 把 Content-Disposition 裡的檔名取出來（伺服器保證純 ASCII）；取不到或可疑時用預設檔名。 */
  function filenameFrom(contentDisposition, fallback) {
    var match = /filename\*?=(?:"([^"]+)"|([^;\s]+))/i.exec(contentDisposition || '');
    var name = match ? (match[1] || match[2] || '') : '';
    return /^[A-Za-z0-9._-]{1,80}$/.test(name) ? name : fallback;
  }

  /* 預定刪除的時間（台灣時間）。無法解析時回傳破折號。 */
  function formatScheduledAt(iso) {
    var parsed = typeof iso === 'string' ? Date.parse(iso) : NaN;
    if (Number.isNaN(parsed)) { return '—'; }
    var local = new Date(parsed + TAIPEI_OFFSET_MS);
    var pad = function (value) { return String(value).padStart(2, '0'); };
    return local.getUTCFullYear() + ' 年 ' + (local.getUTCMonth() + 1) + ' 月 ' + local.getUTCDate() + ' 日 ' +
      pad(local.getUTCHours()) + ':' + pad(local.getUTCMinutes()) + '（台灣時間）';
  }

  /* 錯誤代碼 → 說明。已知代碼用這裡的文字，未知代碼退回伺服器給的訊息，再沒有就用通用說明。 */
  function describeLifecycleError(info) {
    var code = info && info.code;
    if (code && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)) { return ERROR_MESSAGES[code]; }
    return (info && info.message) || '發生錯誤，請稍後再試。';
  }

  /* 以 `responseType: 'blob'` 請求時，錯誤回應的本文也是 Blob：把它讀成 JSON 放回 `error.response.data`，
     讓共用的錯誤整理（Auth.describeError）照常運作。讀不出來就保持原樣。永遠回傳 rejected 的 Promise（帶著同一個錯誤）。 */
  function unwrapBlobError(error) {
    var response = error && error.response;
    var blob = response && response.data;
    if (!blob || typeof blob.text !== 'function') { return Promise.reject(error); }
    return blob.text().then(
      function (text) {
        try { response.data = JSON.parse(text); } catch (parseError) { /* 本文不是 JSON：保持原樣 */ }
        return Promise.reject(error);
      },
      function () { return Promise.reject(error); }
    );
  }

  return {
    TAIPEI_OFFSET_MS: TAIPEI_OFFSET_MS,
    EXPORT_TIMEOUT_MS: EXPORT_TIMEOUT_MS,
    EXPORT_FORMATS: EXPORT_FORMATS,
    ERROR_MESSAGES: ERROR_MESSAGES,
    matchesConfirmText: matchesConfirmText,
    canSubmitDeletion: canSubmitDeletion,
    deletionBody: deletionBody,
    filenameFrom: filenameFrom,
    formatScheduledAt: formatScheduledAt,
    describeLifecycleError: describeLifecycleError,
    unwrapBlobError: unwrapBlobError
  };
});
