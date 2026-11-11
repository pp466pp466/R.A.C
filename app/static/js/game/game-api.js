/* RPG 入口與遊戲頁的 API 呼叫（離線引入的 axios；見 rpg-entry-api、rpg-creation-wizard、rpg-game-sync、rpg-wallet-ui、rpg-notebook-panel 規格）。
 *
 * 每個函式回傳 Promise：成功時是回應本文，失敗時以 `Auth.describeError` 整理過的物件拒絕
 * （{ kind, message, fields, code, status }，再加下列只有固定形狀的欄位，供 `Game.describe*Failure` 對照成畫面訊息）：
 *   categories        審查拒絕（422）的類別列舉（只有固定的識別字，其他為空陣列）
 *   notebook          409 `notebook_conflict`：後端最新的記事簿（回應最外層的 `notebook`，玩家自己的內容；呼叫端只拿來顯示，不記錄）
 *   length、capacity  422 `notebook_too_long`：玩家的字數與目前等級的容量（整數）
 *   checkin           409 `checkin_overflow`：簽到預覽 `{ would_gain, would_lose }`（整數）
 *   balance、needed   409 `insufficient_coins`：合計餘額與所需數量（整數）
 *   沒有這些內容時欄位不存在；型別不對的值一律不放進來，也不複製其他未列出的回應欄位。
 *
 * 三組端點：
 * - **入口與創角**（`/api/sessions`）：`catalog`、`sessions` 兩個清單端點是唯讀、不呼叫模型、不動遊戲幣，用 axios 的預設逾時即可；
 *   `setup`（載入）、`suggest`（AI 生成預覽，無狀態）、`start`（開存檔）由開存檔精靈使用，`suggest` 與 `start` 會呼叫模型。
 * - **遊戲頁**（`/api/game/sessions/<id>`）：`snapshot`（存檔狀態、第 0 幕、世界狀態的玩家視圖、記事簿用量摘要）、`turns`（`since_seq` 增量讀取回合）、
 *   `notebook`（記事簿全文，開啟面板才讀）、`saveNotebook`（玩家儲存編輯，帶 `expected_version`）。**回合與第 0 幕的串流不在這裡**：
 *   那是 `turn-stream.js` 的事（原生 fetch 搭配 ReadableStream，AGENTS.md 第 5 節）。
 * - **遊戲幣**（`/api/wallet`）：`wallet`（餘額、每輪幣價、簽到狀態）、`checkin`（每日簽到；`confirmOverflow` 為真才帶 `confirm_overflow: true`）。
 * 會呼叫模型或較慢的請求，逾時（`options.timeout`，毫秒）與中止用的 `signal`（AbortController）**由呼叫端傳入**，數值取自頁面初始狀態（由設定算出，這裡不寫死）；
 * 沒給就用 axios 的預設。**寫入的呼叫恰好是四個**：`suggest`、`start`、`checkin`、`saveNotebook`（測試固定，`tests/test_game_pages.py`）。
 * **玩家文字只在請求本文裡**：這裡不記錄、不快取、不寫任何瀏覽器儲存。
 * 路徑都是固定字串，識別碼經 encodeURIComponent 才放進網址；分頁參數只送呼叫端給的數字（範圍由伺服器驗證，前端不重複實作規則）。
 */
(function (root) {
  'use strict';

  var axios = root.axios;
  var Auth = root.Auth;
  var BASE = '/api/sessions';
  var GAME_BASE = '/api/game/sessions';
  var WALLET_BASE = '/api/wallet';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  function reject(error) {
    var info = Auth.describeError(error);
    var body = error && error.response && error.response.data;
    info.categories = body && Array.isArray(body.categories) ? body.categories.map(String) : [];
    if (isObject(body)) {
      if (isObject(body.notebook)) { info.notebook = body.notebook; }
      if (isCount(body.length)) { info.length = body.length; }
      if (isCount(body.capacity)) { info.capacity = body.capacity; }
      if (isObject(body.checkin) && isCount(body.checkin.would_gain) && isCount(body.checkin.would_lose)) {
        info.checkin = { would_gain: body.checkin.would_gain, would_lose: body.checkin.would_lose };
      }
      if (isCount(body.balance)) { info.balance = body.balance; }
      if (isCount(body.needed)) { info.needed = body.needed; }
    }
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
  function send(method, url, data, options, params) {
    var request = { method: method, url: url };
    if (data !== undefined) { request.data = data; }
    if (params) { request.params = params; }
    if (options && typeof options.timeout === 'number') { request.timeout = options.timeout; }
    if (options && options.signal) { request.signal = options.signal; }
    return axios(request).then(function (response) { return response.data; }, reject);
  }

  /* 入口與創角的路徑（相對於 `/api/sessions`）。 */
  function call(method, path, data, options) { return send(method, BASE + path, data, options); }

  function gamePath(id) { return GAME_BASE + '/' + encodeURIComponent(id); }

  function turnParams(sinceSeq, options) {
    var params = {};
    if (typeof sinceSeq === 'number') { params.since_seq = sinceSeq; }
    if (options && typeof options.limit === 'number') { params.limit = options.limit; }
    return params;
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
    start: function (payload, options) { return call('post', '', payload, options); },

    /* 存檔快照：{ session: { id, title, content_tier, status, ended, protagonist, version, completed_turns, opening: { state, text }, world, notebook（只有用量摘要，沒有全文） } } */
    snapshot: function (id, options) { return send('get', gamePath(id), undefined, options); },
    /* 序號大於 `sinceSeq` 的回合（遞增）：{ turns: [{ seq, client_msg_id, turn_number, status, action, story }], has_more }；只有 `complete` 的回合含文字。 */
    turns: function (id, sinceSeq, options) { return send('get', gamePath(id) + '/turns', undefined, options, turnParams(sinceSeq, options)); },
    /* 記事簿全文與用量：{ notebook: { text, version, length, capacity, reserve, full } } */
    notebook: function (id, options) { return send('get', gamePath(id) + '/notebook', undefined, options); },
    /* 儲存玩家編輯的記事簿（`expectedVersion` 是開啟編輯時的版本）：{ notebook }；版本不符時以 409 `notebook_conflict` 拒絕並附後端最新的記事簿。 */
    saveNotebook: function (id, text, expectedVersion, options) { return send('put', gamePath(id) + '/notebook', { text: text, expected_version: expectedVersion }, options); },

    /* 錢包：{ ok, wallet: { free_balance, paid_balance, total, free_cap, turn_cost: { standard }, checkin: { claimable, grant, would_gain, would_lose, next_reset_at } } } */
    wallet: function (options) { return send('get', WALLET_BASE, undefined, options); },
    /* 每日簽到：成功回 { ok, wallet }；會使免費幣超過上限而未確認時 409 `checkin_overflow`（附預覽）；`confirmOverflow` 為真才帶 `confirm_overflow: true`。 */
    checkin: function (confirmOverflow, options) { return send('post', WALLET_BASE + '/checkin', { confirm_overflow: confirmOverflow === true }, options); }
  };
})(typeof self !== 'undefined' ? self : this);
