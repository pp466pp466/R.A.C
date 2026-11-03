/* 登入相關頁面的判斷邏輯（純函式，與框架無關，Node 可以直接測試）：登入頁、年齡確認頁、申訴頁、管理員的密碼驗證頁。
 *
 * 頁面腳本只負責「收集輸入、呼叫 API、把這裡算出的結果顯示出來」；錯誤代碼要怎麼處理（清空哪些欄位、導向哪裡、顯示什麼說明）
 * 集中在這裡，所以可以在沒有瀏覽器的情況下逐一測試。結果物件的 `action`：
 *   - `fields`：欄位錯誤，顯示在欄位旁；`clear` 列出要清空的欄位；
 *   - `message`：整體的錯誤說明，顯示在表單上方；
 *   - `redirect`：導向別的頁面（`to`）；`message` 若有，先顯示再導向；
 *   - `final`：流程已結束（例如帳號已抹除），顯示說明與前往 `to` 的連結，不再提供表單。
 * 所有文字都是繁體中文純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。必須在 auth-common.js 之後載入（瀏覽器）。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Auth = root.Auth || {};
    Object.keys(api).forEach(function (key) { root.Auth[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LOGIN_PATH = '/login';
  var DEFAULT_PAGE = '/cs';
  var APPEAL_PATH = '/appeal';

  var TEXT = {
    needBirthDate: '請輸入生日。',
    needAgeRange: '請選擇年齡區間。',
    needPassword: '請輸入密碼。',
    needAppeal: '請輸入申訴內容。',
    ageMismatch: '資料有誤，請重新填寫。',
    ageErased: '您的帳號與相關資料已刪除。',          // 伺服器的說明帶有政策的年齡數字；這裡是後備，不寫死任何年齡
    googleUnavailable: 'Google 登入目前無法使用，請稍後再試。'
  };

  function isString(value) { return typeof value === 'string'; }

  /* 只接受站內路徑（單一 `/` 開頭）；伺服器已驗證過，這裡是第二道，避免任何情況下導向外部網址。 */
  function safeDestination(value, fallback) {
    var target = isString(value) ? value : '';
    if (!target || target.charAt(0) !== '/' || target.charAt(1) === '/' || target.indexOf('\\') !== -1) {
      return fallback || DEFAULT_PAGE;
    }
    return target;
  }

  // ---- 登入頁（只有 Google 與條款勾選框）-----------------------------------------------------------

  /* 按鈕在條款沒勾選時仍可按：回訪的使用者不必勾選，由伺服器依帳號是否存在決定。送出的內容只有條款旗標與登入後要去的位置。 */
  function googleStartBody(acceptTerms, next) {
    return { accept_terms: acceptTerms === true, next: safeDestination(next, DEFAULT_PAGE) };
  }

  /* 授權網址必須是 https；其他協定一律不導向。`base` 是目前頁面的網址（相對路徑要有它才解得開；沒有就視為不安全）。 */
  function isSafeProviderUrl(url, base) {
    try {
      return new URL(url, base).protocol === 'https:';
    } catch (error) {
      return false;
    }
  }

  function googleStartOutcome(info) {
    if (info && info.status === 404) { return { action: 'message', message: TEXT.googleUnavailable }; }
    return { action: 'message', message: (info && info.message) || TEXT.googleUnavailable };
  }

  // ---- 年齡確認頁 ------------------------------------------------------------------------------

  /* 送出前的檢查只是為了少打一次請求；真正的驗證在伺服器。 */
  function validateAge(values) {
    var errors = {};
    if (!values || !values.birthDate) { errors.birth_date = TEXT.needBirthDate; }
    if (!values || !values.ageRange) { errors.age_range = TEXT.needAgeRange; }
    return errors;
  }

  function ageBody(values) { return { birth_date: values.birthDate, age_range: values.ageRange }; }

  /* 年齡確認送出失敗的處理。
     - age_not_eligible：帳號已抹除（登入狀態也已清除），流程結束，只留下回到登入頁的連結；
     - age_mismatch：通用說明、清空兩個欄位讓使用者重填（不指出哪一個不對，也不暗示可以改填其他值）；
     - validation_error：欄位旁的說明；
     - 401：登入已逾時 -> 登入頁；account_locked（填寫中被鎖定）-> 申訴頁。 */
  function ageOutcome(info) {
    var code = info && info.code;
    if (code === 'age_not_eligible') { return { action: 'final', message: (info.message || TEXT.ageErased), to: LOGIN_PATH }; }
    if (code === 'age_mismatch') { return { action: 'message', message: TEXT.ageMismatch, clear: ['birthDate', 'ageRange'] }; }
    if (code === 'account_locked') { return { action: 'redirect', to: APPEAL_PATH }; }
    if (info && info.status === 401) { return { action: 'redirect', to: LOGIN_PATH + '?next=' + encodeURIComponent('/age') }; }
    if (info && info.fields && Object.keys(info.fields).length) { return { action: 'fields', fields: info.fields }; }
    return { action: 'message', message: (info && info.message) || '發生錯誤，請稍後再試。' };
  }

  // ---- 申訴頁 ----------------------------------------------------------------------------------

  var APPEAL_STATUS = { pending: '等待管理員處理', handled: '管理員已處理' };

  function appealStatusLabel(status) { return APPEAL_STATUS[status] || '—'; }

  /* 字數以 Unicode 字元計（與伺服器相同），不是 UTF-16 單位：一個表情符號算一個字。 */
  function charCount(text) { return Array.from(isString(text) ? text : '').length; }

  function appealCounter(text, max) {
    var used = charCount(text);
    return { used: used, max: max, text: used + ' / ' + max, over: used > max };
  }

  function validateAppeal(text, max) {
    var trimmed = isString(text) ? text.trim() : '';
    if (!trimmed) { return { content: TEXT.needAppeal }; }
    if (charCount(trimmed) > max) { return { content: '申訴內容最多 ' + max + ' 字。' }; }
    return {};
  }

  /* 申訴送出失敗：404（已不是鎖定狀態，例如管理員剛解除）-> 導向預設頁面；409／429 顯示伺服器的說明；401 -> 登入頁。 */
  function appealOutcome(info) {
    if (info && info.status === 401) { return { action: 'redirect', to: LOGIN_PATH + '?next=' + encodeURIComponent(APPEAL_PATH) }; }
    if (info && info.status === 404) { return { action: 'redirect', to: DEFAULT_PAGE }; }
    if (info && info.fields && Object.keys(info.fields).length) { return { action: 'fields', fields: info.fields }; }
    return { action: 'message', message: (info && info.message) || '發生錯誤，請稍後再試。' };
  }

  // ---- 管理員的密碼驗證頁 ----------------------------------------------------------------------

  function validateAdminPassword(password) {
    return isString(password) && password ? {} : { password: TEXT.needPassword };
  }

  /* 密碼驗證失敗：沒有（或已過期的）中間狀態 -> 回登入頁重新 Google（`?error=google_state` 讓登入頁說明原因）；密碼錯誤與鎖定共用同一則通用說明（由伺服器提供），清空密碼。 */
  function adminPasswordOutcome(info) {
    var code = info && info.code;
    if (code === 'unauthorized' || (info && info.status === 401 && code !== 'invalid_credentials')) {
      return { action: 'redirect', to: LOGIN_PATH + '?error=google_state' };      // 登入頁顯示「驗證已逾時，請再試一次」
    }
    if (code === 'invalid_credentials') { return { action: 'message', message: info.message, clear: ['password'] }; }
    if (info && info.fields && Object.keys(info.fields).length) { return { action: 'fields', fields: info.fields }; }
    return { action: 'message', message: (info && info.message) || '發生錯誤，請稍後再試。', clear: ['password'] };
  }

  /* 忘記密碼：404 代表沒有寄信設定或中間狀態已失效，按鈕收起並提示重新登入。 */
  function adminForgotOutcome(info) {
    if (info && info.status === 404) {
      return { action: 'message', message: '目前無法寄送重設密碼的信件。若登入已逾時，請重新以 Google 登入。', hideButton: true };
    }
    return { action: 'message', message: (info && info.message) || '發生錯誤，請稍後再試。' };
  }

  return {
    TEXT: TEXT,
    safeDestination: safeDestination,
    googleStartBody: googleStartBody,
    isSafeProviderUrl: isSafeProviderUrl,
    googleStartOutcome: googleStartOutcome,
    validateAge: validateAge,
    ageBody: ageBody,
    ageOutcome: ageOutcome,
    appealStatusLabel: appealStatusLabel,
    charCount: charCount,
    appealCounter: appealCounter,
    validateAppeal: validateAppeal,
    appealOutcome: appealOutcome,
    validateAdminPassword: validateAdminPassword,
    adminPasswordOutcome: adminPasswordOutcome,
    adminForgotOutcome: adminForgotOutcome
  };
});
