/* 客服的本機暫存（IndexedDB，開啟失敗自動退回記憶體；與框架無關，Node 可以直接測試）。
 *
 * 後端是權威：訊息由伺服器先寫入並指派序號，本機只是（1）離線時可閱讀的快取、（2）尚未同步成功的「待送」訊息。
 * 衝突時一律以後端為準（見 cs-sync.js），本機只保留尚未同步成功的待送紀錄。
 *
 * 四個資料集：
 *   conversations  對話。key = 對外識別碼；尚未在伺服器建立的對話用 'new:<client_id>'（離線建立，連線後冪等重送）
 *   messages       伺服器的訊息，key = <對話 key>|<補零的序號>，字典序就是序號順序
 *   pending        待送訊息，key = 補零的建立順序；依建立順序重送，以原本的訊息識別碼，不產生重複
 *   meta           雜項（建立順序的計數器）
 * 資料庫名稱含使用者的對外識別碼：同一個瀏覽器裡不同帳號的快取互相看不到。
 *
 * 暫存不可用（隱私模式、被禁止、配額用盡）時退回記憶體：頁面照常可用，只是離線與重新整理後的待送訊息不保證保存。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.CS = root.CS || {};
    root.CS.store = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DB_VERSION = 1;
  var COLLECTIONS = ['conversations', 'messages', 'pending', 'meta'];
  var NEW_PREFIX = 'new:';
  var SEQ_DIGITS = 10;
  var ORDINAL_DIGITS = 12;

  function pad(number, digits) {
    var text = String(number);
    while (text.length < digits) { text = '0' + text; }
    return text;
  }

  function messageKey(conversationKey, seq) { return conversationKey + '|' + pad(seq, SEQ_DIGITS); }
  function localConversationKey(clientId) { return NEW_PREFIX + clientId; }
  function isLocalConversationKey(key) { return typeof key === 'string' && key.indexOf(NEW_PREFIX) === 0; }

  // ---- 後端一：記憶體 ----------------------------------------------------------------

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

  // ---- 後端二：IndexedDB ---------------------------------------------------------------

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
        if (prefix && !keyRange) { return Promise.reject(new Error('沒有 IDBKeyRange，無法依前綴讀取')); } // 交給退路改用記憶體，不回傳別的對話的資料
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

  // ---- 具備退路的後端：任何一次操作失敗就改用記憶體 ----------------------------------------------

  function resilient(primary, onFallback) {
    var active = primary;
    var memory = null;
    function call(method, args) {
      return active[method].apply(active, args).catch(function (error) {
        if (active === memory) { throw error; }
        memory = createMemoryBackend();    // 暫存壞了（配額、被關閉）：本次使用期間改用記憶體，已寫入的快取不搬移
        active = memory;
        if (onFallback) { onFallback(error); }
        return active[method].apply(active, args);
      });
    }
    var wrapper = {
      get mode() { return active.mode; }
    };
    ['get', 'put', 'delete', 'all', 'clear'].forEach(function (method) {
      wrapper[method] = function () { return call(method, Array.prototype.slice.call(arguments)); };
    });
    wrapper.close = function () { primary.close(); };
    return wrapper;
  }

  /* 開啟暫存。options：indexedDB（工廠）、name、keyRange、openTimeoutMs、onFallback(error)。永遠成功。 */
  function openStore(options) {
    options = options || {};
    var name = options.name || 'cs-cache';
    var factory = null;
    try {                               // 連取得 indexedDB 這個全域物件都可能丟出例外（被禁止時）
      factory = options.indexedDB !== undefined ? options.indexedDB : (typeof indexedDB !== 'undefined' ? indexedDB : null);
    } catch (error) {
      factory = null;
    }
    return openIndexedDb(factory, name, options).then(
      function (backend) { return createStore(resilient(backend, options.onFallback)); },
      function (error) {
        if (options.onFallback) { options.onFallback(error); }
        return createStore(createMemoryBackend());
      }
    );
  }

  // ---- 領域操作 ---------------------------------------------------------------------------

  function createStore(backend) {
    function nextOrdinal() {
      return backend.get('meta', 'ordinal').then(function (current) {
        var next = (current || 0) + 1;
        return backend.put('meta', 'ordinal', next).then(function () { return next; });
      });
    }

    function removeByPrefix(collection, prefix, keyOf) {
      return backend.all(collection, prefix).then(function (values) {
        return Promise.all(values.map(function (value) { return backend.delete(collection, keyOf(value)); }));
      });
    }

    function pendingKey(entry) { return pad(entry.ordinal, ORDINAL_DIGITS); }

    var store = {
      get mode() { return backend.mode; },

      // -- 對話 --
      saveConversation: function (record) { return backend.put('conversations', record.key, record); },
      getConversation: function (key) { return backend.get('conversations', key); },
      listConversations: function () {
        return backend.all('conversations').then(function (list) {
          return list.slice().sort(function (a, b) {
            if (a.updated_at === b.updated_at) { return a.key < b.key ? 1 : -1; }
            return a.updated_at < b.updated_at ? 1 : -1;   // ISO 時間字串，字典序即時間序；新的在前
          });
        });
      },
      /* 以後端的列表覆寫已同步的對話；尚未在伺服器建立的（key 以 new: 開頭）保留。回傳被移除的對話 key。 */
      mergeServerConversations: function (serverList) {
        return backend.all('conversations').then(function (local) {
          var serverKeys = {};
          serverList.forEach(function (record) { serverKeys[record.key] = true; });
          var gone = local.filter(function (record) { return !isLocalConversationKey(record.key) && !serverKeys[record.key]; });
          return Promise.all(gone.map(function (record) { return store.removeConversation(record.key); }))
            .then(function () { return Promise.all(serverList.map(function (record) { return backend.put('conversations', record.key, record); })); })
            .then(function () { return gone.map(function (record) { return record.key; }); });
        });
      },
      /* 刪除對話：連同它的快取訊息與待送一起移除。 */
      removeConversation: function (key) {
        return backend.delete('conversations', key)
          .then(function () { return removeByPrefix('messages', key + '|', function (message) { return messageKey(key, message.seq); }); })
          .then(function () { return store.listPending(key); })
          .then(function (entries) { return Promise.all(entries.map(function (entry) { return backend.delete('pending', pendingKey(entry)); })); });
      },
      /* 離線建立的對話在伺服器建立成功後換成正式的 key：訊息與待送一併改掛。 */
      rekeyConversation: function (oldKey, record) {
        return backend.put('conversations', record.key, record)
          .then(function () { return backend.delete('conversations', oldKey); })
          .then(function () { return store.listPending(oldKey); })
          .then(function (entries) {
            return Promise.all(entries.map(function (entry) {
              entry.conversation_key = record.key;
              return backend.put('pending', pendingKey(entry), entry);
            }));
          });
      },

      // -- 訊息（伺服器的）--
      getMessages: function (conversationKey) {
        return backend.all('messages', conversationKey + '|').then(function (list) {
          return list.slice().sort(function (a, b) { return a.seq - b.seq; });
        });
      },
      /* 以序號為鍵寫入；同序號的舊資料被後端的版本覆寫。 */
      upsertMessages: function (conversationKey, messages) {
        return Promise.all(messages.map(function (message) {
          return backend.put('messages', messageKey(conversationKey, message.seq), message);
        }));
      },
      replaceMessages: function (conversationKey, messages) {
        return removeByPrefix('messages', conversationKey + '|', function (message) { return messageKey(conversationKey, message.seq); })
          .then(function () { return store.upsertMessages(conversationKey, messages); });
      },

      // -- 待送 --
      addPending: function (entry) {
        return nextOrdinal().then(function (ordinal) {
          var record = {
            client_msg_id: entry.client_msg_id, conversation_key: entry.conversation_key, content: entry.content,
            ordinal: ordinal, status: 'queued', attempts: 0, error: null
          };
          return backend.put('pending', pendingKey(record), record).then(function () { return record; });
        });
      },
      listPending: function (conversationKey) {
        return backend.all('pending').then(function (list) {
          return list
            .filter(function (entry) { return conversationKey === undefined || entry.conversation_key === conversationKey; })
            .sort(function (a, b) { return a.ordinal - b.ordinal; });
        });
      },
      findPending: function (clientMsgId) {
        return store.listPending().then(function (list) {
          return list.filter(function (entry) { return entry.client_msg_id === clientMsgId; })[0];
        });
      },
      updatePending: function (clientMsgId, patch) {
        return store.findPending(clientMsgId).then(function (entry) {
          if (!entry) { return null; }
          Object.keys(patch).forEach(function (field) { entry[field] = patch[field]; });
          return backend.put('pending', pendingKey(entry), entry).then(function () { return entry; });
        });
      },
      removePending: function (clientMsgId) {
        return store.findPending(clientMsgId).then(function (entry) {
          return entry ? backend.delete('pending', pendingKey(entry)) : undefined;
        });
      },

      clearAll: function () {
        return Promise.all(COLLECTIONS.map(function (collection) { return backend.clear(collection); }));
      },
      close: function () { backend.close(); }
    };
    return store;
  }

  return {
    DB_VERSION: DB_VERSION,
    messageKey: messageKey,
    localConversationKey: localConversationKey,
    isLocalConversationKey: isLocalConversationKey,
    createMemoryBackend: createMemoryBackend,
    openIndexedDb: openIndexedDb,
    createStore: createStore,
    openStore: openStore
  };
});
