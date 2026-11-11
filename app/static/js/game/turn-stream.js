/* RPG 回合與第 0 幕的串流客戶端（原生 fetch 搭配 ReadableStream；見 rpg-stream-client 規格與 rpg-game-ui 的 design D2、D3）。
 *
 * `createTurnStream(deps)` 回傳 `{ run }`。依賴全部由呼叫端注入（本檔不碰 `window`、`document`，Node 可以用假的 fetch 與假計時器測試）：
 *   deps.fetch、deps.setTimeout、deps.clearTimeout、deps.AbortController、deps.TextDecoder，
 *   以及 deps.describeHttpFailure(status, body)（開始前的 JSON 錯誤整理成畫面訊息，來自 game-state.js；它在載入順序上排在本檔之後，所以用注入）。
 *
 * `run({ url, body, csrf, idleMs, totalMs, signal, onEvent })` **永不 reject**，回傳 Promise<{ outcome, done, failure }>：
 *   - `done`：收到 `done` 事件（`result.done` 是它的資料）。**完整接收的唯一依據是 `done` 事件**，不是 HTTP 狀態、也不是 `reader.read()` 的結束旗標
 *     （AGENTS.md 第 5 節）。收到 `done` 就立即結案並取消讀取，不等連線關閉。
 *   - `error_event`：連線結束前沒有 `done`、但收到過 `error` 事件（`failure` 是 `{ kind: 'error_event', code, message }`）。
 *   - `interrupted`：連線在 `done` 之前結束（含讀取例外、連線建立前的網路失敗）。伺服器可能已經完成這一輪，呼叫端必須以 `since_seq` 補齊。
 *   - `timeout_idle`：連續 `idleMs` 沒有收到任何位元組（回應標頭與任何資料塊都重設；`ping` 只是一塊資料，所以也重設）。
 *   - `timeout_total`：從送出起算 `totalMs` 仍沒有 `done`（不重設）。
 *   - `aborted`：外部的 `signal` 中止（玩家取消、切換存檔、`pagehide`）。
 *   - `rejected`：伺服器在開始串流之前就以 HTTP 錯誤拒絕（401、403、404、409、400、429、503…），`failure` 是 `describeHttpFailure` 的結果。
 *     這時還沒有預扣遊戲幣。另外，`idleMs`／`totalMs`／`url` 不合法（程式錯誤）也回 `rejected`（`failure.kind === 'config'`），**不送出請求**——
 *     逾時數值必須來自頁面初始狀態，這裡沒有預設值，不會在沒有計時器的情況下靜默地跑下去。
 * 外部 `signal`、兩個計時器共用一個內部 AbortController；結果由「誰先觸發」決定，`settle` 只會執行一次，
 * **每一條結果路徑都清除兩個計時器並移除對外部 `signal` 的監聽**；計時器觸發時不等待 fetch 的 abort 傳回，直接結案（即使 fetch 的實作沒有回應 signal 也不會卡住）。
 * 優先順序：`done` > `error` 事件 > 觸發原因（逾時、取消）> `interrupted`。
 * 事件只轉交已知的七種名稱（meta、token、ping、transition、state、done、error）給 `onEvent`，未知名稱忽略；`onEvent` 拋出的例外被吞掉，不影響串流。
 * 這裡**不記錄、不輸出任何內容**（事件本文是玩家的劇情）。
 */
(function (root, factory) {
  'use strict';
  var parser = (typeof module === 'object' && module.exports) ? require('./sse-parser.js') : root.Game;
  var api = factory(parser);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    Object.keys(api).forEach(function (key) { root.Game[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function (parserApi) {
  'use strict';

  var OUTCOMES = {
    DONE: 'done',
    ERROR_EVENT: 'error_event',
    INTERRUPTED: 'interrupted',
    TIMEOUT_IDLE: 'timeout_idle',
    TIMEOUT_TOTAL: 'timeout_total',
    ABORTED: 'aborted',
    REJECTED: 'rejected'
  };
  var CAUSE_OUTCOMES = { idle: OUTCOMES.TIMEOUT_IDLE, total: OUTCOMES.TIMEOUT_TOTAL, external: OUTCOMES.ABORTED };
  var KNOWN_EVENTS = { meta: true, token: true, ping: true, transition: true, state: true, done: true, error: true };
  var CSRF_HEADER = 'X-CSRF-Token';
  var CODE_MAX = 64;
  var MESSAGE_MAX = 200;

  function positive(value) { return typeof value === 'number' && isFinite(value) && value > 0; }
  function known(name) { return Object.prototype.hasOwnProperty.call(KNOWN_EVENTS, name); }
  function swallow() { /* 取消讀取是盡力而為：串流已結束或已鎖定時的失敗都不重要 */ }

  function configFailure() {
    return { kind: 'config', status: 0, code: 'config', message: '', fields: {} };
  }

  function createTurnStream(deps) {
    var env = deps && typeof deps === 'object' ? deps : {};

    function ready() {
      return typeof env.fetch === 'function' && typeof env.setTimeout === 'function' && typeof env.clearTimeout === 'function' &&
        typeof env.AbortController === 'function' && typeof env.TextDecoder === 'function';
    }

    function httpFailure(status, body) {
      if (typeof env.describeHttpFailure === 'function') {
        try { return env.describeHttpFailure(status, body); } catch (error) { /* 退回下面的最小形狀 */ }
      }
      return { kind: 'unknown', status: status, code: '', message: '', fields: {} };
    }

    function run(options) {
      var opts = options && typeof options === 'object' ? options : {};
      if (!ready() || typeof opts.url !== 'string' || opts.url === '' || !positive(opts.idleMs) || !positive(opts.totalMs)) {
        return Promise.resolve({ outcome: OUTCOMES.REJECTED, done: null, failure: configFailure() });
      }
      if (opts.signal && opts.signal.aborted) {
        return Promise.resolve({ outcome: OUTCOMES.ABORTED, done: null, failure: null });
      }

      return new Promise(function (resolve) {
        var controller = new env.AbortController();
        var decoder = new env.TextDecoder('utf-8');
        var parser = parserApi.createParser();
        var idleTimer = null;
        var totalTimer = null;
        var reader = null;
        var cause = null;                 // 'idle' | 'total' | 'external'：第一個觸發的原因
        var doneData = null;
        var errorData = null;
        var settled = false;

        function clearTimers() {
          if (idleTimer !== null) { env.clearTimeout(idleTimer); idleTimer = null; }
          if (totalTimer !== null) { env.clearTimeout(totalTimer); totalTimer = null; }
        }

        function detach() {
          clearTimers();
          if (opts.signal && typeof opts.signal.removeEventListener === 'function') {
            try { opts.signal.removeEventListener('abort', onExternalAbort); } catch (error) { /* 忽略 */ }
          }
        }

        function release() {
          if (!reader) { return; }
          try {
            var cancelled = reader.cancel();
            if (cancelled && typeof cancelled.then === 'function') { cancelled.then(swallow, swallow); }
          } catch (error) { /* 忽略 */ }
        }

        /* 唯一的結案出口：清除計時器與監聽、釋放讀取、回傳結果。 */
        function settle(outcome, failure) {
          if (settled) { return; }
          settled = true;
          detach();
          release();
          resolve({ outcome: outcome, done: doneData, failure: failure || null });
        }

        function errorFailure() {
          return {
            kind: 'error_event',
            code: typeof errorData.code === 'string' ? errorData.code.slice(0, CODE_MAX) : '',
            message: typeof errorData.message === 'string' ? errorData.message.slice(0, MESSAGE_MAX) : ''
          };
        }

        /* 連線結束或中斷時的結果：done > error 事件 > 觸發原因 > interrupted。 */
        function settleBroken() {
          if (doneData) { settle(OUTCOMES.DONE); return; }
          if (errorData) { settle(OUTCOMES.ERROR_EVENT, errorFailure()); return; }
          settle(cause !== null ? CAUSE_OUTCOMES[cause] : OUTCOMES.INTERRUPTED);
        }

        function stop(reason) {
          if (settled) { return; }
          if (cause === null) { cause = reason; }
          try { controller.abort(); } catch (error) { /* 忽略 */ }
          settleBroken();                 // 不等 fetch 回報 abort：計時器與取消的結果在這一刻就確定
        }

        function onExternalAbort() { stop('external'); }

        function armIdle() {
          if (settled) { return; }
          if (idleTimer !== null) { env.clearTimeout(idleTimer); }
          idleTimer = env.setTimeout(function () { idleTimer = null; stop('idle'); }, opts.idleMs);
        }

        function deliver(event) {
          if (typeof opts.onEvent !== 'function') { return; }
          try { opts.onEvent(event); } catch (error) { /* 畫面端的錯誤不得中斷串流 */ }
        }

        function handleChunk(value) {
          var events = parser.feed(decoder.decode(value, { stream: true }));
          for (var index = 0; index < events.length && !settled; index += 1) {
            var event = events[index];
            if (!known(event.event)) { continue; }
            if (event.event === 'error') { errorData = event.data; }
            if (event.event === 'done') { doneData = event.data; }
            deliver(event);
            if (event.event === 'done') { settle(OUTCOMES.DONE); }            // 收到 done 就結案，不等連線關閉
          }
        }

        function pump() {
          if (settled) { return; }
          var step;
          try { step = reader.read(); } catch (error) { settleBroken(); return; }
          Promise.resolve(step).then(function (result) {
            if (settled) { return; }
            if (!result || result.done) { settleBroken(); return; }           // 連線結束：沒有 done 就是中斷（或 error 事件）
            armIdle();
            try { handleChunk(result.value); } catch (error) { settleBroken(); return; }
            pump();
          }, function () {
            if (!settled) { settleBroken(); }                                  // 讀取例外、abort 傳回：依觸發原因結案
          });
        }

        function onResponse(response) {
          if (settled) { return; }
          armIdle();                                                           // 收到回應標頭也算收到位元組
          if (!response || !response.ok) {
            var status = response && typeof response.status === 'number' ? response.status : 0;
            Promise.resolve().then(function () { return response.json(); }).then(function (body) { return body; }, function () { return null; })
              .then(function (body) { settle(OUTCOMES.REJECTED, httpFailure(status, body)); });
            return;
          }
          if (!response.body || typeof response.body.getReader !== 'function') { settle(OUTCOMES.INTERRUPTED); return; }
          reader = response.body.getReader();
          pump();
        }

        /* 組出請求（本文無法序列化是程式錯誤：不送出請求）。 */
        var headers = { 'Accept': 'text/event-stream' };
        headers[CSRF_HEADER] = typeof opts.csrf === 'string' ? opts.csrf : '';
        var init = { method: 'POST', headers: headers, credentials: 'same-origin', signal: controller.signal };
        if (opts.body !== undefined) {
          try { init.body = JSON.stringify(opts.body); } catch (error) { settle(OUTCOMES.REJECTED, configFailure()); return; }
          if (typeof init.body !== 'string') { settle(OUTCOMES.REJECTED, configFailure()); return; }
          headers['Content-Type'] = 'application/json';
        }

        if (opts.signal && typeof opts.signal.addEventListener === 'function') { opts.signal.addEventListener('abort', onExternalAbort); }
        totalTimer = env.setTimeout(function () { totalTimer = null; stop('total'); }, opts.totalMs);
        armIdle();

        var request;
        try { request = env.fetch(opts.url, init); } catch (error) { request = Promise.reject(error); }
        Promise.resolve(request).then(onResponse).then(undefined, function () { if (!settled) { settleBroken(); } });
      });
    }

    return { run: run };
  }

  return { createTurnStream: createTurnStream, STREAM_OUTCOMES: OUTCOMES };
});
