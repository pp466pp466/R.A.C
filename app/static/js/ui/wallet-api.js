/* 全站共用的遊戲幣 API 呼叫（`UI.walletApi`；離線引入的 axios；見 shared-wallet-balance 與 wallet-hub-page 規格，design D5）。
 *
 * 三個函式，每個回傳 Promise：成功時是回應本文，失敗時以 `Auth.describeError` 整理過的物件拒絕
 * （{ kind, message, fields, code, status }，再加下列只有固定形狀的整數欄位，供 `Game.describeCheckinFailure` 之類的函式對照成畫面訊息）：
 *   checkin           409 `checkin_overflow`：簽到預覽 `{ would_gain, would_lose }`（兩者都是非負整數才放進來）
 *   balance、needed   409 `insufficient_coins`：合計餘額與所需數量（非負整數）
 *   沒有這些內容時欄位不存在；型別不對的值一律不放進來，也不複製其他未列出的回應欄位。
 *
 * - `wallet(options)`：`GET /api/wallet`（`{ ok, wallet }`；這個端點會補發贈幣與退回逾時預扣，皆冪等，所以頁面載入時只由共用狀態庫呼叫一次）。
 * - `checkin(confirmOverflow, options)`：`POST /api/wallet/checkin`；本文永遠是 `{ confirm_overflow: <布林> }`，只有 `confirmOverflow === true` 才是 `true`。
 * - `entries(before, limit, options)`：`GET /api/wallet/entries`（遊戲幣記錄，由新到舊）；`before` 是上一頁回傳的游標（非負整數才帶），`limit` 是正整數才帶（範圍由伺服器驗證）。
 * `options.timeout`（毫秒）與 `options.signal`（AbortController）由呼叫端傳入，沒給就用 axios 的預設。
 *
 * **與 `game-api.js` 的關係**：遊戲頁的 `Game.api` 仍保有自己的 `wallet`／`checkin`（進行頁的簽到走它，「寫入呼叫恰好四個」的守門不動）；這裡是另一份給
 * 沒有載入整套遊戲 API 的頁面使用，`reject` 的整理邏輯因此有兩份（約十行），由 `tests/js/wallet-api.test.js` 的對照測試保證形狀一致。
 * **不記錄、不快取、不碰任何瀏覽器儲存、不輸出到主控台**；axios 與 Auth 在呼叫時才取用（載入順序：這個檔案在 base.html，Auth 由各頁自己載入）。
 */
(function (root) {
  'use strict';

  var WALLET_BASE = '/api/wallet';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  function describe(error) {
    var Auth = root.Auth;
    if (Auth && typeof Auth.describeError === 'function') { return Auth.describeError(error); }
    return { kind: 'network', message: '', fields: {}, code: 'network', status: 0 };   // Auth 沒載入（不應發生）：當作連線失敗
  }

  /* 把 axios 的錯誤整理成固定形狀的失敗物件（與 game-api.js 的 reject 在簽到預覽與餘額欄位上形狀相同）。 */
  function reject(error) {
    var info = describe(error);
    var body = error && error.response && error.response.data;
    if (isObject(body)) {
      if (isObject(body.checkin) && isCount(body.checkin.would_gain) && isCount(body.checkin.would_lose)) {
        info.checkin = { would_gain: body.checkin.would_gain, would_lose: body.checkin.would_lose };
      }
      if (isCount(body.balance)) { info.balance = body.balance; }
      if (isCount(body.needed)) { info.needed = body.needed; }
    }
    return Promise.reject(info);
  }

  /* 逾時與中止訊號只在呼叫端有給時才放進請求。 */
  function send(method, url, data, options, params) {
    var request = { method: method, url: url };
    if (data !== undefined) { request.data = data; }
    if (params) { request.params = params; }
    if (options && typeof options.timeout === 'number') { request.timeout = options.timeout; }
    if (options && options.signal) { request.signal = options.signal; }
    return root.axios(request).then(function (response) { return response.data; }, reject);
  }

  function entryParams(before, limit) {
    var params = {};
    if (isCount(before)) { params.before = before; }
    if (isCount(limit) && limit >= 1) { params.limit = limit; }
    return params;
  }

  root.UI = root.UI || {};
  root.UI.walletApi = {
    /* 錢包：{ ok, wallet: { free_balance, paid_balance, total, free_cap, turn_cost: { standard }, coin_exempt, checkin: { claimable, grant, would_gain, would_lose, next_reset_at } } } */
    wallet: function (options) { return send('get', WALLET_BASE, undefined, options); },
    /* 每日簽到：成功回 { ok, wallet }；會使免費幣超過上限而未確認時 409 `checkin_overflow`（附預覽）；`confirmOverflow` 為真才是 `confirm_overflow: true`。 */
    checkin: function (confirmOverflow, options) { return send('post', WALLET_BASE + '/checkin', { confirm_overflow: confirmOverflow === true }, options); },
    /* 記錄一頁：{ ok, entries: [{ seq, label, pending, free_delta, paid_delta, created_at }], next_before }；`next_before` 為 null 表示沒有更舊的了。 */
    entries: function (before, limit, options) { return send('get', WALLET_BASE + '/entries', undefined, options, entryParams(before, limit)); }
  };
})(typeof self !== 'undefined' ? self : this);
