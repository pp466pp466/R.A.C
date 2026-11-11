/* RPG 入口的 API 呼叫（離線引入的 axios；見 rpg-entry-api 規格）。
 *
 * 每個函式回傳 Promise：成功時是回應本文，失敗時以 `Auth.describeError` 整理過的物件拒絕
 * （{ kind, message, fields, code, status }）。兩個清單端點都是唯讀、不呼叫模型、不動遊戲幣，用 axios 的預設逾時即可。
 * 路徑都是固定字串；分頁參數只送呼叫端給的整數（範圍由伺服器驗證，前端不重複實作規則）。
 */
(function (root) {
  'use strict';

  var axios = root.axios;
  var Auth = root.Auth;
  var BASE = '/api/sessions';

  function reject(error) { return Promise.reject(Auth.describeError(error)); }

  function paging(options) {
    var params = {};
    if (options && typeof options.limit === 'number') { params.limit = options.limit; }
    if (options && typeof options.offset === 'number') { params.offset = options.offset; }
    return params;
  }

  function get(path, options) {
    return axios({ method: 'get', url: BASE + path, params: paging(options) }).then(function (response) { return response.data; }, reject);
  }

  root.Game = root.Game || {};
  root.Game.api = {
    /* 可玩的劇本一頁：{ ok, scripts, has_more, content_mode } */
    catalog: function (options) { return get('/catalog', options); },
    /* 我的存檔一頁：{ ok, sessions, has_more } */
    sessions: function (options) { return get('', options); }
  };
})(typeof self !== 'undefined' ? self : this);
