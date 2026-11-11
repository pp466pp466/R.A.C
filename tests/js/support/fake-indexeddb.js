'use strict';
/* 最小的假 IndexedDB（game-store.test.js 與 game-sync.test.js 共用，不是測試檔；`cs-store.test.js` 有自己的一份，沒有改動）。
 * 只實作 game-store.js 用到的部分：非同步回呼、依 key 排序的 getAll、交易完成事件。
 * behaviour：neverOpens、errorOnOpen、blocked、failWrites（寫入時交易中止，像配額用盡）、transactionThrows（`db.transaction()` 同步丟出例外，像連線已關閉）。
 * `dump(factory)` 傾印整個假資料庫（每個資料庫、每個集合、每一筆），供「檢查整個資料庫內容」的守門測試使用。 */

const keyRange = { bound: (lower, upper) => ({ lower, upper }) };

function fakeIndexedDB(behaviour) {
  behaviour = behaviour || {};
  const databases = new Map();
  return {
    databases,
    open(name) {
      const request = {};
      setTimeout(() => {
        if (behaviour.neverOpens) { return; }
        if (behaviour.errorOnOpen) { request.error = new Error('denied'); request.onerror && request.onerror(); return; }
        if (behaviour.blocked) { request.onblocked && request.onblocked(); return; }
        let record = databases.get(name);
        const isNew = !record;
        if (isNew) { record = { stores: new Map() }; databases.set(name, record); }
        const db = {
          closed: false,
          objectStoreNames: { contains: (store) => record.stores.has(store) },
          createObjectStore(store) { record.stores.set(store, new Map()); },
          close() { db.closed = true; },
          transaction(storeName) {
            if (behaviour.transactionThrows) { throw new Error('InvalidStateError'); }
            const transaction = {};
            let scheduled = false;
            const finish = () => {
              if (scheduled) { return; }
              scheduled = true;
              setTimeout(() => {
                if (behaviour.failWrites && transaction.wrote) {
                  transaction.error = new Error('QuotaExceededError');
                  transaction.onabort && transaction.onabort();
                } else {
                  transaction.oncomplete && transaction.oncomplete();
                }
              }, 0);
            };
            const map = record.stores.get(storeName);
            const reply = (value) => {
              const req = {};
              setTimeout(() => { req.result = value; req.onsuccess && req.onsuccess(); }, 0);
              return req;
            };
            transaction.objectStore = () => ({
              get: (key) => { finish(); return reply(map.get(key)); },
              put: (value, key) => { transaction.wrote = true; if (!behaviour.failWrites) { map.set(key, JSON.parse(JSON.stringify(value))); } finish(); return reply(undefined); },
              delete: (key) => { transaction.wrote = true; map.delete(key); finish(); return reply(undefined); },
              clear: () => { transaction.wrote = true; map.clear(); finish(); return reply(undefined); },
              getAll: (range) => {
                finish();
                const keys = [...map.keys()].filter((key) => !range || (key >= range.lower && key <= range.upper)).sort();
                return reply(keys.map((key) => JSON.parse(JSON.stringify(map.get(key)))));
              }
            });
            return transaction;
          }
        };
        request.result = db;
        if (isNew) { request.onupgradeneeded && request.onupgradeneeded(); }
        request.onsuccess && request.onsuccess();
      }, 0);
      return request;
    }
  };
}

/* 假資料庫裡的每一筆：{ database, collection, key, value }。 */
function dump(factory) {
  const rows = [];
  for (const [database, record] of factory.databases) {
    for (const [collection, map] of record.stores) {
      for (const [key, value] of map) { rows.push({ database, collection, key, value }); }
    }
  }
  return rows;
}

module.exports = { fakeIndexedDB, keyRange, dump };
