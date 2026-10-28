/* 會員頁面共用的純函式（與框架無關，Node 可以直接測試）。
 *
 * 在瀏覽器中，載入後會把函式放到全域的 `Auth`，並自動替 axios 設定預設值：
 *  - 所有請求預設帶 `X-CSRF-Token` 標頭，值取自頁面 `<meta name="csrf-token">`（AGENTS.md 第 10 節）；
 *  - 預設逾時 15 秒。
 * 訊息一律是繁體中文純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Auth = root.Auth || {};
    Object.keys(api).forEach(function (key) { root.Auth[key] = api[key]; });
    if (root.axios && root.document) { api.configureAxios(root.axios, root.document); }
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var CSRF_HEADER = 'X-CSRF-Token';
  var DEFAULT_TIMEOUT_MS = 15000;
  var MESSAGES = {
    network: '無法連線到伺服器，請檢查網路連線後再試。',
    timeout: '連線逾時，請稍後再試。',
    csrf: '頁面已過期，請重新整理頁面後再試。',
    unknown: '發生錯誤，請稍後再試。'
  };

  function csrfTokenFrom(doc) {
    var meta = doc.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  function configureAxios(axios, doc) {
    axios.defaults.headers.common[CSRF_HEADER] = csrfTokenFrom(doc);
    axios.defaults.timeout = DEFAULT_TIMEOUT_MS;
    return axios;
  }

  /* 頁面初始狀態：伺服器以 <script type="application/json" id="initial-state"> 提供（tojson 已跳脫），
     這裡只用 JSON.parse 讀取，不會執行任何內容。壞掉時回傳空物件。 */
  function readInitialState(doc) {
    try {
      var element = doc.getElementById('initial-state');
      var parsed = element ? JSON.parse(element.textContent) : {};
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (error) {
      return {};
    }
  }

  /* 信箱驗證與重設密碼的連結把權杖放在網址片段（#token=…）：片段不會送到伺服器，也不進存取日誌。
     讀取後立即從網址列移除，避免被複製、截圖或存進瀏覽紀錄。 */
  function readFragmentToken(win) {
    var hash = (win.location && win.location.hash) || '';
    // 權杖必須完整到片段結尾（或下一個 &）：夾雜其他字元的值整個視為無效，不取前半段
    var match = /(?:^#|&)token=([A-Za-z0-9_\-]+)(?:&|$)/.exec(hash);
    if (hash) {
      try { win.history.replaceState(null, '', win.location.pathname + win.location.search); } catch (error) { /* 舊瀏覽器：忽略 */ }
    }
    return match ? match[1] : '';
  }

  /* 把 axios 的錯誤整理成頁面要顯示的內容。
     kind：server（伺服器回了錯誤）、network、timeout、csrf、canceled。 */
  function describeError(error) {
    var response = error && error.response;
    if (!response) {
      var code = error && error.code;
      if (code === 'ERR_CANCELED' || (error && error.name === 'CanceledError')) {
        return { kind: 'canceled', message: '', fields: {}, code: 'canceled', status: 0 };
      }
      if (code === 'ECONNABORTED' || code === 'ETIMEDOUT') {
        return { kind: 'timeout', message: MESSAGES.timeout, fields: {}, code: 'timeout', status: 0 };
      }
      return { kind: 'network', message: MESSAGES.network, fields: {}, code: 'network', status: 0 };
    }
    var body = response.data && typeof response.data === 'object' ? response.data.error : null;
    var errorCode = body && typeof body.code === 'string' ? body.code : '';
    var message = body && typeof body.message === 'string' && body.message ? body.message : MESSAGES.unknown;
    var fields = {};
    if (body && body.fields && typeof body.fields === 'object') {
      Object.keys(body.fields).forEach(function (name) { fields[name] = String(body.fields[name]); });
    }
    if (response.status === 403 && errorCode === 'csrf_failed') {
      return { kind: 'csrf', message: message || MESSAGES.csrf, fields: {}, code: errorCode, status: 403 };
    }
    return { kind: 'server', message: message, fields: fields, code: errorCode, status: response.status };
  }

  /* 導向登入頁並保留目前位置。`next` 只是建議值，伺服器會在登入頁再驗證一次。 */
  function loginUrlFor(path) {
    return '/login?next=' + encodeURIComponent(path);
  }

  return {
    MESSAGES: MESSAGES,
    CSRF_HEADER: CSRF_HEADER,
    DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
    csrfTokenFrom: csrfTokenFrom,
    configureAxios: configureAxios,
    readInitialState: readInitialState,
    readFragmentToken: readFragmentToken,
    describeError: describeError,
    loginUrlFor: loginUrlFor
  };
});
