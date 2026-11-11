/* RPG 入口的 API 呼叫（離線引入的 axios；見 rpg-entry-api 與 rpg-creation-wizard 規格）。
 *
 * 每個函式回傳 Promise：成功時是回應本文，失敗時以 `Auth.describeError` 整理過的物件拒絕
 * （{ kind, message, fields, code, status }，另加 `categories`：審查拒絕（422）時的類別列舉，只有固定的識別字）。
 * - 兩個清單端點（`catalog`、`sessions`）是唯讀、不呼叫模型、不動遊戲幣，用 axios 的預設逾時即可。
 * - 創角三個呼叫（`setup` 載入、`suggest` 生成預覽、`start` 開存檔）由開存檔精靈使用：`suggest` 與 `start` 會呼叫模型，
 *   逾時（`timeout`，毫秒）與中止用的 `signal`（AbortController）**由呼叫端傳入**，數值取自頁面初始狀態（由設定算出，這裡不寫死）。
 *   `start` 的本文由 `Game.buildStartPayload` 組好；`suggest` 的本文由 `Game.buildSuggestPayload` 組好。**玩家文字只在請求本文裡**：
 *   這裡不記錄、不快取、不寫任何瀏覽器儲存。
 * 路徑都是固定字串，識別碼經 encodeURIComponent 才放進網址；分頁參數只送呼叫端給的整數（範圍由伺服器驗證，前端不重複實作規則）。
 */
(function (root) {
  'use strict';

  var axios = root.axios;
  var Auth = root.Auth;
  var BASE = '/api/sessions';

  function reject(error) {
    var info = Auth.describeError(error);
    var body = error && error.response && error.response.data;
    info.categories = body && Array.isArray(body.categories) ? body.categories.map(String) : [];
    return Promise.reject(info);
  }

  function paging(options) {
    var params = {};
    if (options && typeof options.limit === 'number') { params.limit = options.limit; }
    if (options && typeof options.offset === 'number') { params.offset = options.offset; }
    return params;
  }

  function get(path, options) {
    return axios({ method: 'get', url: BASE + path, params: paging(options) }).then(function (response) { return response.data; }, reject);
  }

  /* 逾時與中止訊號只在呼叫端有給時才放進請求（沒給就用 axios 的預設）。 */
  function call(method, path, data, options) {
    var request = { method: method, url: BASE + path };
    if (data !== undefined) { request.data = data; }
    if (options && typeof options.timeout === 'number') { request.timeout = options.timeout; }
    if (options && options.signal) { request.signal = options.signal; }
    return axios(request).then(function (response) { return response.data; }, reject);
  }

  root.Game = root.Game || {};
  root.Game.api = {
    /* 可玩的劇本一頁：{ ok, scripts, has_more, content_mode } */
    catalog: function (options) { return get('/catalog', options); },
    /* 我的存檔一頁：{ ok, sessions, has_more } */
    sessions: function (options) { return get('', options); },
    /* 序章與創角需要的資料：{ ok, title, background, characters, locations, milestones, limits, origin_options, suggestions_remaining, suggestions_available } */
    setup: function (versionId, options) { return call('get', '/setup/' + encodeURIComponent(versionId), undefined, options); },
    /* AI 生成主角的預覽（無狀態，不建立存檔）：{ ok, protagonist, anchor, remaining } */
    suggest: function (payload, options) { return call('post', '/protagonist/suggest', payload, options); },
    /* 開存檔（201）：{ ok, session: { id, title, content_tier, protagonist } } */
    start: function (payload, options) { return call('post', '', payload, options); }
  };
})(typeof self !== 'undefined' ? self : this);
