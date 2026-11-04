/* 劇本創作 API 的呼叫（離線引入的 axios；見 script-authoring 規格）。
 *
 * 每個函式回傳 Promise：成功時是回應本文，失敗時以 `Auth.describeError` 整理過的物件拒絕
 * （{ kind, message, fields, code, status }，另加 `categories`：審查拒絕時的類別列舉，只有固定的識別字）。
 * 送審會呼叫審查的模型層，最長約 30 秒，所以逾時由呼叫端指定（伺服器的總預算加 5 秒），並用 AbortController 於離開頁面時中止。
 * 所有路徑都是固定字串，識別碼經 encodeURIComponent 才放進網址。
 */
(function (root) {
  'use strict';

  var axios = root.axios;
  var Auth = root.Auth;
  var BASE = '/api/scripts';

  function reject(error) {
    var info = Auth.describeError(error);
    var body = error && error.response && error.response.data;
    info.categories = body && Array.isArray(body.categories) ? body.categories.map(String) : [];
    return Promise.reject(info);
  }

  function call(method, path, data, config) {
    var request = { method: method, url: BASE + path };
    if (data !== undefined) { request.data = data; }
    if (config) { Object.keys(config).forEach(function (key) { request[key] = config[key]; }); }
    return axios(request).then(function (response) { return response.data; }, reject);
  }

  function id(value) { return encodeURIComponent(value); }

  root.Scripts = root.Scripts || {};
  root.Scripts.api = {
    list: function () { return call('get', ''); },
    limits: function () { return call('get', '/limits'); },
    example: function () { return call('get', '/example'); },
    get: function (versionId) { return call('get', '/versions/' + id(versionId)); },
    create: function (content) { return call('post', '', { content: content }); },
    save: function (versionId, content, baseHash) { return call('put', '/versions/' + id(versionId), { content: content, base_hash: baseHash }); },
    remove: function (versionId) { return call('delete', '/versions/' + id(versionId)); },
    withdraw: function (versionId) { return call('post', '/versions/' + id(versionId) + '/withdraw'); },
    submit: function (versionId, options) {
      options = options || {};
      return call('post', '/versions/' + id(versionId) + '/submit', undefined, { timeout: options.timeout, signal: options.signal });
    },
    newVersion: function (scriptId) { return call('post', '/' + id(scriptId) + '/versions'); },
    refreshAuthorName: function (scriptId) { return call('post', '/' + id(scriptId) + '/author-name/refresh'); }
  };
})(typeof self !== 'undefined' ? self : this);
