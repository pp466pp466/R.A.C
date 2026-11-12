'use strict';
/* 真的 Vue（專案離線引入的 vue.global.prod.js）放在 `vm` 沙盒裡執行，讓 `createWallet`／`createSession` 這類「注入 Vue 的 ref／computed／watch」的模組
 * 可以直接用 `require` 載入並以真正的響應式測試（不是自製的替身）。沒有瀏覽器可以掛載，這裡只用到響應式與 watch，不需要 DOM。
 * 用法：`const Vue = loadVue();` 然後把 `{ ref: Vue.ref, computed: Vue.computed, watch: Vue.watch, reactive: Vue.reactive }` 傳給被測試的模組。
 * 注意：沙盒（另一個 realm）建立的物件與陣列原型不同，`assert.deepStrictEqual` 比較前先 `plain()`。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..', '..');
let cached = null;

function loadVue() {
  if (cached) { return cached; }
  const sandbox = { console };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = { createElement: () => ({}), querySelector: () => null };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app/static/vendor/vue/vue.global.prod.js'), 'utf8') + '\n;this.__Vue = Vue;', sandbox);
  cached = sandbox.__Vue;
  return cached;
}

/* 響應式的值轉成純資料（跨 realm 比較用）。 */
function plain(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }

/* 讓已排入的 Promise 與 Vue 的排程跑完（測試用的小等待）。 */
async function settle(turns = 6) {
  for (let index = 0; index < turns; index += 1) { await new Promise((resolve) => setImmediate(resolve)); }
}

module.exports = { loadVue, plain, settle, ROOT };
