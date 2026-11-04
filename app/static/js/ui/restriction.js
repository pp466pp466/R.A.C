/* 申訴捷徑的判定與文字（純函式，與框架無關，Node 可以直接測試；見 appeal-shortcut 規格）。
 *
 * 被鎖定或被暫停發佈的玩家，所在的頁面要直接給「前往申訴」的按鈕，不必自己去找申訴頁。是否顯示**只依伺服器的回應**判定：
 *   - API 回應 403 且代碼是 `account_locked`（使用中才被鎖定）或 `publish_suspended`（送審被拒）；
 *   - `/api/auth/me` 的使用者資料：`restriction === 'locked'`，或 `publish_suspended === true`。
 * 前端不自行推測，也不把狀態存進任何瀏覽器儲存空間，所以狀態解除後重新整理就消失。
 *
 * `restrictionOf(source)` -> 'locked'、'suspended' 或 null。`noticeOf(kind)` -> { text, linkText, href }；按鈕是指向 `/appeal` 的一般連結，
 * 沒有任何查詢字串或使用者資料。所有文字是繁體中文純文字，由 Vue 以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UI = root.UI || {};
    Object.keys(api).forEach(function (key) { root.UI[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var KIND_LOCKED = 'locked';
  var KIND_SUSPENDED = 'suspended';
  var APPEAL_PATH = '/appeal';
  var LINK_TEXT = '前往申訴';

  var NOTICES = {
    locked: '您的帳號已被鎖定，目前無法使用這項功能。如果您認為這是誤會，可以向管理員申訴。',
    suspended: '您的送審權限已被暫停，只有送審受到影響，其他功能照常。如果您認為這是誤會，可以向管理員申訴。'
  };

  function restrictionOf(source) {
    if (!source || typeof source !== 'object') { return null; }
    if (source.status === 403 && source.code === 'account_locked') { return KIND_LOCKED; }
    if (source.status === 403 && source.code === 'publish_suspended') { return KIND_SUSPENDED; }
    if (source.restriction === 'locked') { return KIND_LOCKED; }
    if (source.publish_suspended === true) { return KIND_SUSPENDED; }
    return null;
  }

  function noticeOf(kind) {
    if (!Object.prototype.hasOwnProperty.call(NOTICES, kind)) { return null; }
    return { text: NOTICES[kind], linkText: LINK_TEXT, href: APPEAL_PATH };
  }

  return { restrictionOf: restrictionOf, restrictionNoticeOf: noticeOf, APPEAL_PATH: APPEAL_PATH };
});
