/* 遊戲頁的同步引擎（與框架無關；API 與儲存層都由外部注入，Node 可以直接測試；見 rpg-game-sync 規格與 rpg-game-ui 的 design D3、D5）。
 *
 * 後端是權威：回合由伺服器寫入並指派單調遞增的序號，本機只有（1）可離線閱讀的快取、（2）每個存檔最多一筆在途回合（見 game-store.js）。
 * 這個檔案做三件事，**沒有任何一件會呼叫串流端點**（串流只由玩家按下送出、或明確按下「重送」才發生，每一輪都要消耗遊戲幣，所以不自動排隊重送）：
 *
 *   catchUp(sessionId, { signal })   以 `since_seq`（本機已定稿的最大序號）增量拉取後端的回合，直到 `has_more` 為假；後端的內容覆寫本機同序號的快取。
 *                                    **永不拋出**，回傳 `{ status, changed, added, turns, error? }`：
 *                                      ok        補齊完成（`changed`：本機有新增或被覆寫的回合；`added`：新增幾個）
 *                                      offline   連線失敗或逾時：快取不變，`turns` 是快取
 *                                      gone      後端回 404（存檔已不存在）：該存檔的快取與在途回合已清除
 *                                      canceled  呼叫端中止
 *                                      failed    其他失敗（401、403、5xx…）：`error` 是 game-api 整理過的失敗物件，交給 `Game.describeRequestFailure`
 *                                    `turns` 是顯示用的完整清單：本機快取與這次後端回的回合合併（以 `seq` 為準、後端覆寫），**含進行中（streaming）的回合**
 *                                    ——它沒有文字，也不會寫進快取。
 *                                    `options.fromSeq`（選用）：從這個序號之後重讀（只會把起點往前移，不會比本機最大序號更晚）。修訂（重新生成、編輯最後一輪）之後，
 *                                    與載入頁面時，用「最後一輪的前一個序號」呼叫，其他裝置的修訂與過期的快取內容就會被後端的內容覆寫；回合的比對納入 `revision`
 *                                    （沒有時視為 1），所以同序號但 `revision` 不同就是變動。後端的 `since_seq` 協定不變。
 *   resolveInflight(sessionId, { signal, result, onRevision })
 *                                    先補齊；若本機已有該 `client_msg_id` 的 `complete` 回合就移除在途紀錄並回傳 null；否則回傳在途紀錄（畫面提供「重送」與「放棄」，
 *                                    不自動送出）。補齊失敗（離線等）時同樣保留；存檔已不存在（404）時回傳 null。沒有在途回合回傳 null。永不拋出。
 *                                    呼叫端剛做完 `catchUp` 時，把它的結果以 `result` 傳進來，就不會再多問一次伺服器。
 *                                    **修訂的在途紀錄**（有 `kind`）：自己從目標回合的前一個序號起重讀（不用傳進來的 `result`），快取的回合 `revision` 大於 `expected_revision`
 *                                    視為成功，否則視為沒有完成；**兩種結果都清除紀錄、都回傳 null**（從不自動重送，也沒有「重送」），結果以 `onRevision({ kind, outcome, turns })`
 *                                    交給呼叫端。判定不了（離線等）時紀錄先留著。
 *   remember(sessionId, snapshot)    把快照的白名單欄位存入快取（記事簿與主角角色卡不會進去）。永不拋出。
 *
 * 前端中止不代表後端中止：中斷、逾時與取消之後，後端仍可能完成那一輪，所以串流結束後（不論結果）都要補齊一次——那是呼叫端（game-session.js）的事，這裡只提供 catchUp。
 * 這裡不記錄、不輸出任何內容（回合是玩家的劇情與行動）。
 * 載入順序：game-state.js 在前（回合的形狀與合併取自它）。
 */
(function (root, factory) {
  'use strict';
  var state = (typeof module === 'object' && module.exports) ? require('./game-state.js') : root.Game;
  var api = factory(state);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.sync = api;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var STATUSES = { OK: 'ok', OFFLINE: 'offline', GONE: 'gone', CANCELED: 'canceled', FAILED: 'failed' };

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  /* 連線層的失敗（game-api 把 axios 錯誤整理成 `kind`）：連不上或逾時就是離線，快取照用。 */
  function statusOfFailure(error) {
    var kind = isObject(error) ? error.kind : undefined;
    if (kind === 'canceled') { return STATUSES.CANCELED; }
    if (kind === 'network' || kind === 'timeout') { return STATUSES.OFFLINE; }
    if (isObject(error) && error.status === 404) { return STATUSES.GONE; }
    return STATUSES.FAILED;
  }

  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  /* 兩份回合是否相同。`revision` 也要比（沒有時視為 1）：其他裝置重新生成或編輯之後，同序號的內容會變，快取據此被覆寫。 */
  function sameTurn(a, b) {
    return a.seq === b.seq && a.client_msg_id === b.client_msg_id && a.turn_number === b.turn_number && a.status === b.status && a.action === b.action && a.story === b.story &&
      Game.revisionOf(a) === Game.revisionOf(b);
  }

  /* 在途紀錄是不是修訂（重新生成、編輯最後一輪）的紀錄；沒有 `kind` 的舊紀錄與普通回合都不是。 */
  function isRevisionRecord(record) { return !!record && (record.kind === 'regenerate' || record.kind === 'edit'); }

  /* options：api（需要 `turns(id, sinceSeq, { signal })`）、store（game-store.js 建立的）。 */
  function createSync(options) {
    var api = options.api;
    var store = options.store;

    function catchUp(sessionId, callOptions) {
      var signal = callOptions && callOptions.signal;
      var received = [];          // 後端這次回的全部回合（含不存的進行中回合）
      var added = 0;
      var changed = false;

      function finish(status, extra) {
        return store.turnsOf(sessionId).then(
          function (stored) { return stored; },
          function () { return []; }
        ).then(function (stored) {
          var result = { status: status, changed: changed, added: added, turns: Game.mergeTurns(stored, received) };
          if (extra && extra.error !== undefined) { result.error = extra.error; }
          return result;
        });
      }

      return store.turnsOf(sessionId).then(function (cached) {
        var before = Object.create(null);
        cached.forEach(function (turn) { before[turn.seq] = turn; });
        var since = cached.length ? cached[cached.length - 1].seq : 0;
        if (callOptions && isCount(callOptions.fromSeq) && callOptions.fromSeq < since) { since = callOptions.fromSeq; }   // 重讀最後一輪：從它的前一個序號起，後端的內容覆寫快取

        function page() {
          return new Promise(function (resolve) { resolve(api.turns(sessionId, since, { signal: signal })); }).then(function (data) {   // 同步丟出的例外也走失敗的收尾
            var list = isObject(data) && Array.isArray(data.turns) ? data.turns : [];
            var normalized = Game.mergeTurns([], list);
            return store.putTurns(sessionId, list).then(function () {
              received = received.concat(list);
              normalized.forEach(function (turn) {
                if (turn.status === 'streaming') { return; }                              // 進行中的回合不存，也不算變動
                var previous = before[turn.seq];
                if (!previous) { added += 1; changed = true; } else if (!sameTurn(previous, turn)) { changed = true; }
                before[turn.seq] = turn;
              });
              var last = normalized.length ? normalized[normalized.length - 1].seq : since;
              if (isObject(data) && data.has_more === true && last > since) {            // 沒有前進就停止，避免伺服器異常時無限迴圈
                since = last;
                return page();
              }
              return undefined;
            });
          });
        }

        return page().then(
          function () { return finish(STATUSES.OK); },
          function (error) {
            var status = statusOfFailure(error);
            if (status === STATUSES.GONE) {                                                // 存檔在別的裝置被刪除：清掉它的快取與在途回合
              return store.clearSession(sessionId).then(function () { received = []; return finish(STATUSES.GONE); }, function () { received = []; return finish(STATUSES.GONE); });
            }
            return finish(status, { error: error });
          }
        );
      }).catch(function (error) {
        return { status: STATUSES.FAILED, changed: false, added: 0, turns: [], error: error };
      });
    }

    /* 修訂的在途紀錄（重新整理後才會遇到）：從目標回合的前一個序號起重讀，以後端的 `revision` 判定，**兩種結果都清除紀錄**，從不自動重送、也不提供「重送」。
       回傳 null（沒有東西要在時間軸顯示）；結果交給 `callOptions.onRevision({ kind, outcome, turns })`（outcome 是 'applied'｜'incomplete'），
       呼叫端用它換上最新的回合並在沒有完成時通知玩家。判定不了（離線等）時紀錄先留著，下次載入再判定。 */
    function resolveRevision(sessionId, inflight, callOptions) {
      var signal = callOptions && callOptions.signal;
      return store.turnsOf(sessionId).then(function (cached) {
        var target = null;
        cached.forEach(function (turn) { if (turn.client_msg_id === inflight.client_msg_id) { target = turn; } });
        return catchUp(sessionId, { signal: signal, fromSeq: target ? Math.max(0, target.seq - 1) : undefined });
      }).then(function (result) {
        if (result.status !== STATUSES.OK) { return null; }                               // 含 gone（clearSession 已一併清掉紀錄）
        var outcome = Game.revisionOutcome(result.turns, inflight);
        return store.clearInflight(sessionId).then(function () {
          if (callOptions && typeof callOptions.onRevision === 'function') {
            try { callOptions.onRevision({ kind: inflight.kind, outcome: outcome, turns: result.turns }); } catch (error) { /* 畫面端的錯誤不得影響判定 */ }
          }
          return null;
        });
      });
    }

    function resolveInflight(sessionId, callOptions) {
      return store.getInflight(sessionId).then(function (inflight) {
        if (!inflight) { return null; }
        if (isRevisionRecord(inflight)) { return resolveRevision(sessionId, inflight, callOptions); }
        var given = callOptions && isObject(callOptions.result) && Array.isArray(callOptions.result.turns) ? callOptions.result : null;
        return (given ? Promise.resolve(given) : catchUp(sessionId, callOptions)).then(function (result) {
          if (result.status === STATUSES.GONE) { return null; }                           // clearSession 已一併清掉在途回合
          if (Game.hasCompleteTurn(result.turns, inflight.client_msg_id)) {
            return store.clearInflight(sessionId).then(function () { return null; });
          }
          return inflight;
        });
      }).catch(function () { return null; });
    }

    function remember(sessionId, snapshot) {
      return store.putSession(sessionId, snapshot).then(function (record) { return record; }, function () { return null; });
    }

    return { catchUp: catchUp, resolveInflight: resolveInflight, remember: remember };
  }

  return { STATUSES: STATUSES, createSync: createSync, statusOfFailure: statusOfFailure };
});
