/* 帳號頁「匿名保留我已發佈的劇本」的純函式與狀態機（與框架無關，Node 可以直接測試；見 script-anonymous-retention 規格「帳號頁可設定偏好」「刪除帳號視窗再次確認偏好」）。
 *
 * 在瀏覽器中，載入後會把內容放到全域的 `Auth.scriptRetention`。這裡只決定「怎麼顯示、什麼時候送出請求、失敗怎麼還原」；
 * 偏好的真正讀寫與抹除時的採用永遠在伺服器：`GET /api/account/script-retention` 回 `{ ok, keep, live_count }`（`live_count` 是目前上架的版本數），
 * `PUT` 本文 `{ keep: 布林 }` 回同樣的形狀。管理員（不能自助刪除）與等待刪除的帳號不會呼叫（頁面不啟動載入）；載入失敗或回應看不懂時區塊不顯示（失敗時關閉）。
 *
 * 狀態機（createController）：
 *   - `load()`：只讀一次偏好；成功才顯示區塊（`available`）。失敗一律不顯示，並把失敗交給 `onError`（401 回登入頁、鎖定的申訴捷徑）。
 *   - `toggle()`：請求期間禁用（再按不會再送）、請求失敗時開關維持原狀（不樂觀更新）並顯示說明、無論成功或失敗都在 finally 恢復；成功以 Toast 回報。
 * 刪除視窗的核取方塊與要多帶的欄位（`keep_published_scripts`，只在目前有上架劇本時才送）由 account-lifecycle.js 的 `deletionBody` 決定；這裡只提供 `liveCount` 與目前的偏好。
 * 所有文字是固定的繁體中文純文字，頁面一律以 {{ }} 插值顯示，不使用 v-html；偏好值只是布林，不含任何玩家文字。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Auth = root.Auth || {};
    root.Auth.scriptRetention = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var ENDPOINT = '/api/account/script-retention';

  /* 固定文案：伺服器不提供任何文字。 */
  var COPY = {
    title: '匿名保留我已發佈的劇本',
    onLabel: '開',
    offLabel: '關',
    busy: '儲存中…',
    stateOn: '已開啟：刪除帳號時，您目前上架的劇本版本會以「已註銷的作者」匿名保留。',
    stateOff: '已關閉（預設）：刪除帳號時，您的所有劇本都會一併刪除。',
    points: [
      '只適用於您自己申請刪除帳號，以及連續 12 個月未登入而被清除的帳號。',
      '只保留目前上架的版本；筆名會改為「已註銷的作者」，您的暱稱與短代號都不會留下。',
      '草稿、待審、被退回、已下架與舊版本一律刪除。',
      '保留的劇本仍可被檢舉，平台仍可依規範下架。',
      '偏好在帳號被刪除的當下才會採用，您可以隨時改變。'
    ],
    deleteTitle: '匿名保留我已發佈的劇本',
    deleteNote: '勾選後，帳號刪除時您目前上架的劇本版本會以「已註銷的作者」保留，其餘劇本（草稿、待審、被退回、已下架與舊版本）一律刪除；不勾選則所有劇本都會一併刪除。'
  };

  var MESSAGES = {
    switchedOn: '已開啟「匿名保留我已發佈的劇本」。',
    switchedOff: '已關閉「匿名保留我已發佈的劇本」。',
    malformed: '無法確認目前的設定，請重新整理頁面後再試。',
    failed: '暫時無法儲存，請稍後再試。'
  };

  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  /* 把伺服器的回應整理成 `{ keep, liveCount }`；形狀不對（`keep` 不是布林、`live_count` 不是非負整數）回傳 null。 */
  function parse(data) {
    if (!data || typeof data !== 'object' || typeof data.keep !== 'boolean' || !isCount(data.live_count)) { return null; }
    return { keep: data.keep, liveCount: data.live_count };
  }

  /* 失敗的說明：400 `keep` 與其他伺服器訊息都用固定文字，不顯示伺服器的原文。 */
  function describeFailure(info) {
    if (info && (info.kind === 'network' || info.kind === 'timeout')) { return info.message || MESSAGES.failed; }
    return MESSAGES.failed;
  }

  /* options：
   *   request.get() / request.put(body)  回傳 axios 風格的 Promise（成功時 response.data 是 `{ ok, keep, live_count }`）
   *   describeError   失敗時整理錯誤（Auth.describeError）
   *   toast(message, settings)  提示
   *   reactive        讓 view 成為響應式（Vue.reactive），預設原樣
   *   onError(info)   失敗之後的額外處理（例如 401 回登入頁、鎖定的申訴捷徑）
   */
  function createController(options) {
    options = options || {};
    var reactive = options.reactive || function (value) { return value; };
    var toast = options.toast || function () {};
    var describe = options.describeError || function (error) {
      return { kind: 'server', message: (error && error.message) || '', fields: {}, code: '', status: 0 };
    };
    var request = options.request || {};
    var view = reactive({ available: false, keep: false, liveCount: 0, busy: false, error: '' });
    var loading = false;

    function apply(data) {
      var next = parse(data);
      if (!next) { return false; }
      view.keep = next.keep;
      view.liveCount = next.liveCount;
      return true;
    }

    function fail(error) {
      var info = describe(error);
      if (options.onError) { options.onError(info); }
      return info;
    }

    /* 讀一次偏好；回傳是否成功（成功才顯示區塊）。載入中再呼叫不會再送一次。 */
    function load() {
      if (loading || view.available) { return Promise.resolve(view.available); }
      loading = true;
      return Promise.resolve()
        .then(function () { return request.get(); })
        .then(function (response) {
          var ok = apply(response && response.data);
          view.available = ok;
          return ok;
        }, function (error) {
          fail(error);
          return false;
        })
        .finally(function () { loading = false; });
    }

    /* 切換開關：回傳是否已儲存。忙碌中或區塊不可用時什麼都不做。 */
    function toggle() {
      if (!view.available || view.busy) { return Promise.resolve(false); }
      view.busy = true;
      view.error = '';
      var wanted = !view.keep;
      return Promise.resolve()
        .then(function () { return request.put({ keep: wanted }); })
        .then(function (response) {
          if (!apply(response && response.data)) {
            view.error = MESSAGES.malformed;
            return false;
          }
          toast(view.keep ? MESSAGES.switchedOn : MESSAGES.switchedOff);
          return true;
        }, function (error) {
          view.error = describeFailure(fail(error));      // 開關維持原狀（沒有樂觀更新）
          return false;
        })
        .finally(function () { view.busy = false; });
    }

    return { view: view, load: load, toggle: toggle };
  }

  return {
    ENDPOINT: ENDPOINT,
    COPY: COPY,
    MESSAGES: MESSAGES,
    parse: parse,
    describeFailure: describeFailure,
    createController: createController
  };
});
