/* 客服的同步引擎（與框架無關；API 與儲存層都由外部注入，Node 可以直接測試）。
 *
 * 客服以後端為權威：訊息由後端先寫入並指派單調遞增的序號，本機只有（1）可離線閱讀的快取、（2）尚未同步成功的待送訊息。
 *
 *  - 待送依輸入順序、以原本的訊息識別碼逐一重送；後端以（對話、識別碼）去重，所以不會產生重複訊息。
 *    整個重送過程是循序的（同一時間只有一則在途中），對話內的順序因此一定正確。
 *  - 成功後以回應中的兩則訊息取代待送；網路錯誤、逾時與伺服器暫時不可用：待送保留並停止本輪（連線恢復後再來）；
 *    409 conversation_busy：依序退避後重試，仍忙就保留並停止；401：停止並通知登入已失效；
 *    其他 4xx（內容不合法、閘門、對話不存在……）：標記為「無法送出」，不再自動重送，使用者可以重試或移除。
 *  - 前端中止（取消、切換對話、離開頁面）不代表後端中止：待送保留，之後以 since_seq 補齊後端已寫入的結果。
 *  - 以 since_seq 增量拉取時，後端的訊息覆寫本機同序號的快取；已經有完整回覆的待送隨之移除。
 *  - 離線建立的對話以 client_id 冪等地重送建立，成功後改用正式識別碼。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.CS = root.CS || {};
    root.CS.sync = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULT_BACKOFF_MS = [1000, 2000, 4000];
  var PAGE_SIZE = 200;
  var RETRY_LATER_STATUSES = { 408: true, 429: true, 500: true, 502: true, 503: true, 504: true };

  function conversationRecord(conversation, extra) {
    var record = {
      key: conversation.id, id: conversation.id, client_id: null, title: conversation.title,
      state_version: conversation.state_version, updated_at: conversation.updated_at, created_at: conversation.created_at
    };
    Object.keys(extra || {}).forEach(function (field) { record[field] = extra[field]; });
    return record;
  }

  /* 把一次失敗歸類：retry（稍後再試）、busy、auth、canceled、fail（這則訊息無法送出）。 */
  function classify(error) {
    var kind = error && error.kind;
    var status = error && error.status;
    if (kind === 'canceled') { return 'canceled'; }
    if (kind === 'network' || kind === 'timeout') { return 'retry'; }
    if (status === 401) { return 'auth'; }
    if (status === 409 && error.code === 'conversation_busy') { return 'busy'; }
    if (RETRY_LATER_STATUSES[status]) { return 'retry'; }
    return 'fail';
  }

  function createSync(options) {
    var api = options.api;
    var store = options.store;
    var sleep = options.sleep || function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };
    var backoff = options.backoffMs || DEFAULT_BACKOFF_MS;
    var emit = options.onChange || function () {};
    var onAuthLost = options.onAuthLost || function () {};
    var newId = options.newId;
    var CSStore = options.storeApi;           // 提供 localConversationKey／isLocalConversationKey

    var running = null;
    var again = false;

    // ---- 待送 ----------------------------------------------------------------------

    function enqueue(conversationKey, content, clientMsgId) {
      return store.addPending({ client_msg_id: clientMsgId || newId(), conversation_key: conversationKey, content: content })
        .then(function (entry) { emit({ type: 'pending', key: conversationKey }); return entry; });
    }

    function removeFailed(clientMsgId) {
      return store.findPending(clientMsgId).then(function (entry) {
        return store.removePending(clientMsgId).then(function () { if (entry) { emit({ type: 'pending', key: entry.conversation_key }); } });
      });
    }

    function retryFailed(clientMsgId) {
      return store.updatePending(clientMsgId, { status: 'queued', error: null }).then(function (entry) {
        if (entry) { emit({ type: 'pending', key: entry.conversation_key }); }
        return entry;
      });
    }

    // ---- 建立離線對話 ------------------------------------------------------------------

    function createRemote(local, signal) {
      return api.createConversation(local.client_id, { signal: signal }).then(function (data) {
        return store.rekeyConversation(local.key, conversationRecord(data.conversation, { client_id: local.client_id })).then(function () {
          emit({ type: 'conversations' });
        });
      });
    }

    // ---- 重送 ---------------------------------------------------------------------------

    function applySent(entry, data) {
      var key = entry.conversation_key;
      return store.upsertMessages(key, [data.user_message, data.assistant_message])
        .then(function () { return store.removePending(entry.client_msg_id); })
        .then(function () { return store.getConversation(key); })
        .then(function (record) {
          if (record) { return store.saveConversation(conversationRecord(data.conversation, { client_id: record.client_id })); }
          return undefined;
        })
        .then(function () { emit({ type: 'sent', key: key }); });
    }

    function markAttempt(entry, error) {
      return store.updatePending(entry.client_msg_id, { attempts: (entry.attempts || 0) + 1, error: (error && error.code) || null });
    }

    /* 送出一則待送。回傳 'sent'、'failed'，或停止本輪的原因：'network'、'busy'、'auth'、'canceled'。 */
    function sendOne(entry, signal) {
      return store.getConversation(entry.conversation_key).then(function (record) {
        if (!record) { return store.removePending(entry.client_msg_id).then(function () { return 'dropped'; }); }   // 對話已不在：待送沒有意義，丟棄而不是送出
        var body = { client_msg_id: entry.client_msg_id, content: entry.content };
        var attempt = 0;
        function trySend() {
          return api.sendMessage(record.id, body, { signal: signal }).then(
            function (data) { return applySent(entry, data).then(function () { return 'sent'; }); },
            function (error) {
              var verdict = classify(error);
              if (verdict === 'busy') {
                if (attempt < backoff.length) {
                  var wait = backoff[attempt++];
                  return sleep(wait).then(trySend);
                }
                return markAttempt(entry, error).then(function () { return 'busy'; });
              }
              if (verdict === 'retry') { return markAttempt(entry, error).then(function () { return 'network'; }); }
              if (verdict === 'canceled') { return 'canceled'; }
              if (verdict === 'auth') { onAuthLost(); return 'auth'; }
              return store.updatePending(entry.client_msg_id, {
                status: 'failed', attempts: (entry.attempts || 0) + 1, error: error.code || 'error', message: error.message || ''
              }).then(function () { emit({ type: 'pending', key: entry.conversation_key }); return 'failed'; });
            }
          );
        }
        return trySend();
      });
    }

    function pass(signal, summary) {
      return store.listConversations().then(function (conversations) {
        var locals = conversations.filter(function (record) { return CSStore.isLocalConversationKey(record.key); });
        var chain = Promise.resolve(null);
        locals.forEach(function (local) {
          chain = chain.then(function (stopped) {
            if (stopped) { return stopped; }
            return createRemote(local, signal).then(function () { return null; }, function (error) {
              var verdict = classify(error);
              if (verdict === 'auth') { onAuthLost(); }
              return verdict === 'fail' ? null : (verdict === 'retry' ? 'network' : verdict);
            });
          });
        });
        return chain;
      }).then(function (stopped) {
        if (stopped) { return stopped; }
        return store.listPending().then(function (entries) {
          var queue = entries.filter(function (entry) { return entry.status !== 'failed' && !CSStore.isLocalConversationKey(entry.conversation_key); });
          var chain = Promise.resolve(null);
          queue.forEach(function (entry) {
            chain = chain.then(function (halt) {
              if (halt) { return halt; }
              return sendOne(entry, signal).then(function (result) {
                if (result === 'sent') { summary.sent++; return null; }
                if (result === 'failed') { summary.failed++; return null; }
                if (result === 'dropped') { return null; }
                return result;
              });
            });
          });
          return chain;
        });
      });
    }

    /* 重送所有待送。同一時間只會有一輪在跑；跑的期間再呼叫會讓目前這輪結束後再補一輪。 */
    function flush(flushOptions) {
      if (running) { again = true; return running; }
      var signal = flushOptions && flushOptions.signal;
      var summary = { sent: 0, failed: 0, stopped: null };
      running = (function loop() {
        again = false;
        return pass(signal, summary).then(function (stopped) {
          if (stopped) { summary.stopped = stopped; return summary; }
          return again ? loop() : summary;
        });
      })().then(
        function (result) { running = null; return result; },
        function (error) { running = null; throw error; }
      );
      return running;
    }

    // ---- 以後端為準的拉取 ----------------------------------------------------------------------

    function dropAnswered(key) {
      return Promise.all([store.getMessages(key), store.listPending(key)]).then(function (both) {
        var messages = both[0];
        return Promise.all(both[1].map(function (entry) {
          var user = messages.filter(function (m) { return m.role === 'user' && m.client_msg_id === entry.client_msg_id; })[0];
          var answered = user && messages.some(function (m) { return m.role === 'assistant' && m.seq === user.seq + 1; });
          return answered ? store.removePending(entry.client_msg_id) : null;
        }));
      });
    }

    /* 以 since_seq 增量拉取（從本機已有的最大序號之後開始），後端的訊息覆寫本機同序號的快取。回傳新取得的訊息。 */
    function pull(conversationKey, pullOptions) {
      var signal = pullOptions && pullOptions.signal;
      return store.getConversation(conversationKey).then(function (record) {
        if (!record || CSStore.isLocalConversationKey(conversationKey)) { return []; }
        return store.getMessages(conversationKey).then(function (cached) {
          var since = cached.length ? cached[cached.length - 1].seq : 0;
          var collected = [];
          function page() {
            return api.listMessages(record.id, { since_seq: since, limit: PAGE_SIZE, signal: signal }).then(function (data) {
              return store.upsertMessages(conversationKey, data.messages).then(function () {
                collected = collected.concat(data.messages);
                if (data.has_more && data.messages.length) { since = data.messages[data.messages.length - 1].seq; return page(); }
                return undefined;
              });
            });
          }
          return page().then(function () { return dropAnswered(conversationKey); }).then(function () {
            emit({ type: 'messages', key: conversationKey });
            return collected;
          });
        });
      }).catch(function (error) {
        if (error && error.status === 404) {       // 對話在別的裝置被刪除
          return store.removeConversation(conversationKey).then(function () { emit({ type: 'conversations' }); return []; });
        }
        throw error;
      });
    }

    /* 以後端的對話列表覆寫本機（尚未建立的離線對話保留）。 */
    function refreshConversations(refreshOptions) {
      return api.listConversations({ limit: 100, offset: 0, signal: refreshOptions && refreshOptions.signal }).then(function (data) {
        var records = data.conversations.map(function (conversation) {
          return store.getConversation(conversation.id).then(function (existing) {
            return conversationRecord(conversation, { client_id: existing ? existing.client_id : null });
          });
        });
        return Promise.all(records).then(function (list) { return store.mergeServerConversations(list); });
      }).then(function (removed) { emit({ type: 'conversations' }); return removed; });
    }

    // ---- 對話的建立、改名與刪除 -----------------------------------------------------------------------

    /* 建立對話：先寫入本機（離線也能開始輸入），再嘗試在伺服器建立；失敗時保留為離線對話，之後 flush 會冪等地重送。 */
    function createConversation(clientId) {
      var id = clientId || newId();
      var now = options.now ? options.now() : new Date().toISOString();
      var local = { key: CSStore.localConversationKey(id), id: null, client_id: id, title: '新對話', state_version: 1, updated_at: now, created_at: now };
      return store.saveConversation(local).then(function () {
        emit({ type: 'conversations' });
        return createRemote(local).then(function () { return id; }, function () { return id; });
      }).then(function () {
        return store.listConversations().then(function (list) {
          return list.filter(function (record) { return record.client_id === id; })[0];
        });
      });
    }

    function rename(conversationKey, title) {
      return store.getConversation(conversationKey).then(function (record) {
        return api.renameConversation(record.id, title, record.state_version).then(
          function (data) {
            return store.saveConversation(conversationRecord(data.conversation, { client_id: record.client_id })).then(function () {
              emit({ type: 'conversations' });
              return { ok: true, conflict: false };
            });
          },
          function (error) {
            if (error && error.status === 409 && error.code === 'version_conflict' && error.conversation) {   // 以後端為準
              return store.saveConversation(conversationRecord(error.conversation, { client_id: record.client_id })).then(function () {
                emit({ type: 'conversations' });
                return { ok: false, conflict: true };
              });
            }
            throw error;
          }
        );
      });
    }

    function deleteConversation(conversationKey) {
      return store.getConversation(conversationKey).then(function (record) {
        var remote = record && record.id ? api.deleteConversation(record.id).catch(function (error) {
          if (error && error.status === 404) { return null; }       // 已經不存在：目的達成
          throw error;
        }) : Promise.resolve(null);
        return remote.then(function () { return store.removeConversation(conversationKey); }).then(function () { emit({ type: 'conversations' }); });
      });
    }

    return {
      enqueue: enqueue, flush: flush, pull: pull, refreshConversations: refreshConversations,
      createConversation: createConversation, rename: rename, deleteConversation: deleteConversation,
      removeFailed: removeFailed, retryFailed: retryFailed, classify: classify
    };
  }

  /* 把 axios 包成同步引擎要的 API。錯誤一律轉成 describeError 的結果（plain object）：kind、status、code、message。
     送出訊息的逾時 = 伺服器放進初始狀態的 request_timeout_ms（總預算 + 5 秒），這裡不寫死任何數字。 */
  function createAxiosApi(axios, describeError, settings) {
    var sendTimeout = settings.sendTimeoutMs;
    var base = '/api/cs/conversations';

    function wrap(promise) {
      return promise.then(function (response) { return response.data; }, function (error) {
        var info = describeError(error);
        var data = error && error.response && error.response.data;
        if (data && typeof data === 'object' && data.conversation) { info.conversation = data.conversation; }
        throw info;
      });
    }

    return {
      createConversation: function (clientId, opts) {
        return wrap(axios.post(base, { client_id: clientId }, { signal: opts && opts.signal }));
      },
      listConversations: function (opts) {
        return wrap(axios.get(base, { params: { limit: opts.limit, offset: opts.offset }, signal: opts.signal }));
      },
      renameConversation: function (id, title, version) {
        return wrap(axios.patch(base + '/' + encodeURIComponent(id), { title: title, state_version: version }));
      },
      deleteConversation: function (id) {
        return wrap(axios.delete(base + '/' + encodeURIComponent(id)));
      },
      sendMessage: function (id, body, opts) {
        return wrap(axios.post(base + '/' + encodeURIComponent(id) + '/messages', body, { timeout: sendTimeout, signal: opts && opts.signal }));
      },
      listMessages: function (id, opts) {
        return wrap(axios.get(base + '/' + encodeURIComponent(id) + '/messages', {
          params: { since_seq: opts.since_seq, limit: opts.limit }, signal: opts.signal
        }));
      }
    };
  }

  return { classify: classify, createSync: createSync, createAxiosApi: createAxiosApi, conversationRecord: conversationRecord, DEFAULT_BACKOFF_MS: DEFAULT_BACKOFF_MS };
});
