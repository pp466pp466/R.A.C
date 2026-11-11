/* 遊戲頁的本機暫存（IndexedDB，開啟失敗自動退回記憶體；與框架無關，Node 可以直接測試；見 rpg-game-sync 規格與 rpg-game-ui 的 design D5）。
 *
 * 後端是權威：回合由伺服器寫入並指派序號，本機只是（1）離線時可閱讀的快取、（2）每個存檔最多一筆「在途回合」（玩家剛送出、尚未確認完成的行動）。
 * 衝突時一律以後端為準（見 game-sync.js）；本機沒有任何待送佇列，也不會自行產生回應或序號——RPG 每一輪都要連線生成並消耗遊戲幣，所以不自動排隊重送。
 *
 * 三個資料集（資料庫名稱 `game-cache-<使用者的對外識別碼>`，同一個瀏覽器裡不同帳號互相看不到）：
 *   sessions  存檔的快照摘要，key = 存檔識別碼：標題、內容模式、是否完結、已完成輪數、第 0 幕（狀態與文字）、世界狀態的玩家視圖
 *   turns     已定稿的回合，key = <存檔>|<補零的序號>（字典序即序號順序）：`Game.normalizeTurn` 的形狀（seq、client_msg_id、turn_number、status、action、story）；
 *             被擋與中斷的回合只存序號與狀態，沒有任何文字；**進行中（streaming）的回合不存**——它之後會變成別的狀態，若存下來，
 *             「以本機最大序號做 since_seq」就會跳過它完成後的內容
 *   inflight  在途回合，key = 存檔識別碼（所以每個存檔最多一筆）：client_msg_id、行動文字、建立時間（由呼叫端給）
 * 只寫入白名單欄位：**不存記事簿（全文或摘要）、主角角色卡、email、生日與年齡區間**；傳進來的物件有多餘欄位一律丟掉。
 * 這裡不記錄、不輸出任何內容（行動與劇情是玩家文字）。
 *
 * 暫存不可用（隱私模式、被禁止、配額用盡、交易同步拋出例外）時退回記憶體：頁面照常可用，只是離線與重新整理後的快取與在途回合不保證保存。
 * 所有 IndexedDB 的讀寫都包在 Promise 內，任何失敗（含同步丟出的例外）都改用記憶體，不拋出給畫面。
 * 載入順序：game-state.js 在前（回合與世界狀態的形狀取自它）。
 */
(function (root, factory) {
  'use strict';
  var state = (typeof module === 'object' && module.exports) ? require('./game-state.js') : root.Game;
  var api = factory(state);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.store = api;
  }
})(typeof self !== 'undefined' ? self : this, function (Game) {
  'use strict';

  var DB_VERSION = 1;
  var COLLECTIONS = ['sessions', 'turns', 'inflight'];
  var NAME_PREFIX = 'game-cache-';
  var SEQ_DIGITS = 10;
  var ID_MAX = 100;
  var CLIENT_MSG_ID_MAX = 200;
  var FINAL_STATUSES = { complete: true, blocked: true, interrupted: true };

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function text(value) { return typeof value === 'string' ? value : ''; }

  function pad(number, digits) {
    var value = String(number);
    while (value.length < digits) { value = '0' + value; }
    return value;
  }

  /* 存檔識別碼是 UUID；含 `|` 的值會讓「依前綴讀取」混到別的存檔，所以直接視為無效（不讀不寫）。 */
  function validId(value) { return typeof value === 'string' && value !== '' && value.length <= ID_MAX && value.indexOf('|') === -1; }
  function turnKey(sessionId, seq) { return sessionId + '|' + pad(seq, SEQ_DIGITS); }
  function turnPrefix(sessionId) { return sessionId + '|'; }

  /* 資料庫名稱：含使用者的對外識別碼（public_id）。識別碼格式不對回傳 null——呼叫端不要用共用的名稱，改用記憶體。 */
  function databaseName(publicId) {
    return typeof publicId === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(publicId) ? NAME_PREFIX + publicId : null;
  }

  // ---- 白名單 ------------------------------------------------------------------------------------------

  /* 存檔快照 -> 快取紀錄。只取頁面離線閱讀需要的欄位；記事簿、主角角色卡與其他欄位一律不進來。 */
  function sessionRecord(sessionId, snapshot) {
    var source = isObject(snapshot) ? snapshot : {};
    var opening = isObject(source.opening) ? source.opening : {};
    return {
      id: sessionId,
      title: text(source.title),
      content_tier: text(source.content_tier),
      ended: source.ended === true,
      completed_turns: isCount(source.completed_turns) ? source.completed_turns : 0,
      opening: { state: typeof opening.state === 'string' && /^[a-z_]{1,20}$/.test(opening.state) ? opening.state : null, text: text(opening.text) },
      world: Game.worldView(source.world)
    };
  }

  function inflightRecord(entry) {
    if (!isObject(entry) || typeof entry.client_msg_id !== 'string' || entry.client_msg_id === '' || entry.client_msg_id.length > CLIENT_MSG_ID_MAX) { return null; }
    return { client_msg_id: entry.client_msg_id, action: text(entry.action), created_at: text(entry.created_at) };
  }

  // ---- 後端一：記憶體 ----------------------------------------------------------------------------------

  function createMemoryBackend() {
    var data = {};
    COLLECTIONS.forEach(function (name) { data[name] = new Map(); });
    return {
      mode: 'memory',
      get: function (collection, key) { return Promise.resolve(data[collection].has(key) ? data[collection].get(key) : undefined); },
      put: function (collection, key, value) { data[collection].set(key, value); return Promise.resolve(); },
      delete: function (collection, key) { data[collection].delete(key); return Promise.resolve(); },
      /* 依 key 的字典序回傳；prefix 只取該前綴。 */
      all: function (collection, prefix) {
        var keys = Array.from(data[collection].keys()).filter(function (key) { return !prefix || key.indexOf(prefix) === 0; }).sort();
        return Promise.resolve(keys.map(function (key) { return data[collection].get(key); }));
      },
      clear: function (collection) { data[collection].clear(); return Promise.resolve(); },
      close: function () {}
    };
  }

  // ---- 後端二：IndexedDB -------------------------------------------------------------------------------

  function requestToPromise(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error || new Error('IndexedDB 要求失敗')); };
    });
  }

  function transactionDone(transaction) {
    return new Promise(function (resolve, reject) {
      transaction.oncomplete = function () { resolve(); };
      transaction.onerror = function () { reject(transaction.error || new Error('IndexedDB 交易失敗')); };
      transaction.onabort = function () { reject(transaction.error || new Error('IndexedDB 交易中止')); };
    });
  }

  /* 開啟資料庫；任何失敗（沒有 indexedDB、被禁止、被阻擋、逾時）都以拒絕結束，由呼叫端退回記憶體。 */
  function openIndexedDb(factory, name, options) {
    options = options || {};
    var keyRange = options.keyRange || (typeof IDBKeyRange !== 'undefined' ? IDBKeyRange : null);
    var timeoutMs = options.openTimeoutMs !== undefined ? options.openTimeoutMs : 3000;
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = null;
      function fail(error) {
        clearTimeout(timer);
        if (!settled) { settled = true; reject(error || new Error('無法開啟 IndexedDB')); }
      }
      timer = setTimeout(function () { fail(new Error('開啟 IndexedDB 逾時')); }, timeoutMs);
      try {
        if (!factory || typeof factory.open !== 'function') { throw new Error('此瀏覽器沒有 IndexedDB'); }
        var request = factory.open(name, DB_VERSION);
        request.onupgradeneeded = function () {
          var db = request.result;
          COLLECTIONS.forEach(function (collection) {
            if (!db.objectStoreNames.contains(collection)) { db.createObjectStore(collection); }
          });
        };
        request.onblocked = function () { fail(new Error('IndexedDB 被其他分頁阻擋')); };
        request.onerror = function () { fail(request.error); };
        request.onsuccess = function () {
          clearTimeout(timer);
          if (settled) { try { request.result.close(); } catch (error) { /* 已放棄 */ } return; }
          settled = true;
          resolve(wrapDatabase(request.result, keyRange));
        };
      } catch (error) {
        clearTimeout(timer);
        fail(error);
      }
    });
  }

  function wrapDatabase(db, keyRange) {
    function objectStore(collection, mode) {
      var transaction = db.transaction(collection, mode);
      return { store: transaction.objectStore(collection), done: transactionDone(transaction) };
    }
    return {
      mode: 'indexeddb',
      get: function (collection, key) {
        var handle = objectStore(collection, 'readonly');
        return requestToPromise(handle.store.get(key));
      },
      put: function (collection, key, value) {
        var handle = objectStore(collection, 'readwrite');
        handle.store.put(value, key);
        return handle.done;
      },
      delete: function (collection, key) {
        var handle = objectStore(collection, 'readwrite');
        handle.store.delete(key);
        return handle.done;
      },
      all: function (collection, prefix) {
        if (prefix && !keyRange) { return Promise.reject(new Error('沒有 IDBKeyRange，無法依前綴讀取')); } // 交給退路改用記憶體，不回傳別的存檔的資料
        var handle = objectStore(collection, 'readonly');
        var range = prefix ? keyRange.bound(prefix, prefix + '￿') : undefined;
        return requestToPromise(handle.store.getAll(range));
      },
      clear: function (collection) {
        var handle = objectStore(collection, 'readwrite');
        handle.store.clear();
        return handle.done;
      },
      close: function () { try { db.close(); } catch (error) { /* 已關閉 */ } }
    };
  }

  // ---- 具備退路的後端：任何一次操作失敗（含同步丟出的例外）就改用記憶體 ----------------------------------

  function resilient(primary, onFallback) {
    var active = primary;
    var memory = null;
    /* 包進 Promise：`db.transaction()` 在連線關閉時、`put()` 在值無法複製時會同步丟出例外，要和非同步的失敗同樣處理。 */
    function attempt(target, method, args) {
      return new Promise(function (resolve) { resolve(target[method].apply(target, args)); });
    }
    function call(method, args) {
      return attempt(active, method, args).catch(function (error) {
        if (active === memory) { throw error; }
        memory = createMemoryBackend();    // 暫存壞了（配額、被關閉）：本次使用期間改用記憶體，已寫入的快取不搬移
        active = memory;
        if (onFallback) { try { onFallback(error); } catch (ignored) { /* 回報失敗不影響使用 */ } }
        return attempt(active, method, args);
      });
    }
    var wrapper = {
      get mode() { return active.mode; }
    };
    ['get', 'put', 'delete', 'all', 'clear'].forEach(function (method) {
      wrapper[method] = function () { return call(method, Array.prototype.slice.call(arguments)); };
    });
    wrapper.close = function () { try { primary.close(); } catch (error) { /* 已關閉 */ } };
    return wrapper;
  }

  /* 開啟暫存。options：indexedDB（工廠）、name（用 `databaseName(public_id)` 組；沒有名稱就只用記憶體，不共用資料庫）、keyRange、openTimeoutMs、
     onFallback(error)。永遠成功。 */
  function openStore(options) {
    options = options || {};
    var report = function (error) { if (options.onFallback) { try { options.onFallback(error); } catch (ignored) { /* 回報失敗不影響使用 */ } } };
    if (typeof options.name !== 'string' || options.name === '') {
      report(new Error('沒有資料庫名稱，改用記憶體'));
      return Promise.resolve(createStore(createMemoryBackend()));
    }
    var factory = null;
    try {                               // 連取得 indexedDB 這個全域物件都可能丟出例外（被禁止時）
      factory = options.indexedDB !== undefined ? options.indexedDB : (typeof indexedDB !== 'undefined' ? indexedDB : null);
    } catch (error) {
      factory = null;
    }
    return openIndexedDb(factory, options.name, options).then(
      function (backend) { return createStore(resilient(backend, options.onFallback)); },
      function (error) {
        report(error);
        return createStore(createMemoryBackend());
      }
    );
  }

  // ---- 領域操作 ----------------------------------------------------------------------------------------

  function createStore(backend) {
    function turnsOf(sessionId) {
      if (!validId(sessionId)) { return Promise.resolve([]); }
      return backend.all('turns', turnPrefix(sessionId)).then(function (list) {
        return list.filter(function (turn) { return isObject(turn) && isCount(turn.seq); }).sort(function (a, b) { return a.seq - b.seq; });
      });
    }

    return {
      get mode() { return backend.mode; },

      // -- 存檔快照 --
      /* 存入快照的白名單欄位；回傳寫入的紀錄（識別碼無效時不寫入，回傳 null）。 */
      putSession: function (sessionId, snapshot) {
        if (!validId(sessionId)) { return Promise.resolve(null); }
        var record = sessionRecord(sessionId, snapshot);
        return backend.put('sessions', sessionId, record).then(function () { return record; });
      },
      getSession: function (sessionId) {
        if (!validId(sessionId)) { return Promise.resolve(null); }
        return backend.get('sessions', sessionId).then(function (record) { return isObject(record) ? record : null; });
      },

      // -- 回合（伺服器的）--
      /* 以序號為鍵寫入；同序號的舊資料被傳入的版本覆寫（呼叫端傳的是後端的回合）。進行中的回合與沒有整數序號的資料略過。回傳寫入的筆數。 */
      putTurns: function (sessionId, turns) {
        if (!validId(sessionId)) { return Promise.resolve(0); }
        var records = [];
        (Array.isArray(turns) ? turns : []).forEach(function (raw) {
          var turn = Game.normalizeTurn(raw);
          if (turn && FINAL_STATUSES[turn.status] === true) { records.push(turn); }
        });
        return Promise.all(records.map(function (turn) { return backend.put('turns', turnKey(sessionId, turn.seq), turn); })).then(function () { return records.length; });
      },
      /* 依序號遞增。 */
      turnsOf: turnsOf,
      /* 本機已有的最大序號（沒有為 0）。 */
      maxSeq: function (sessionId) {
        return turnsOf(sessionId).then(function (list) { return list.length ? list[list.length - 1].seq : 0; });
      },

      // -- 在途回合（每個存檔最多一筆）--
      /* 存入在途回合，取代該存檔原有的一筆；回傳寫入的紀錄（識別碼或內容無效時不寫入，回傳 null）。 */
      setInflight: function (sessionId, entry) {
        var record = inflightRecord(entry);
        if (!validId(sessionId) || !record) { return Promise.resolve(null); }
        return backend.put('inflight', sessionId, record).then(function () { return record; });
      },
      getInflight: function (sessionId) {
        if (!validId(sessionId)) { return Promise.resolve(null); }
        return backend.get('inflight', sessionId).then(function (record) { return inflightRecord(record); });
      },
      clearInflight: function (sessionId) {
        if (!validId(sessionId)) { return Promise.resolve(); }
        return backend.delete('inflight', sessionId);
      },

      /* 存檔不存在或已刪除：清掉該存檔的快照、全部回合與在途回合，不動其他存檔。 */
      clearSession: function (sessionId) {
        if (!validId(sessionId)) { return Promise.resolve(); }
        return turnsOf(sessionId).then(function (list) {
          return Promise.all(list.map(function (turn) { return backend.delete('turns', turnKey(sessionId, turn.seq)); }));
        }).then(function () { return backend.delete('sessions', sessionId); })
          .then(function () { return backend.delete('inflight', sessionId); });
      },
      close: function () { backend.close(); }
    };
  }

  return {
    DB_VERSION: DB_VERSION,
    COLLECTIONS: COLLECTIONS.slice(),
    databaseName: databaseName,
    turnKey: turnKey,
    createMemoryBackend: createMemoryBackend,
    openIndexedDb: openIndexedDb,
    createStore: createStore,
    openStore: openStore
  };
});
