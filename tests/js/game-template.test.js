'use strict';
/* 遊戲主畫面的樣板（app/templates/game/session.html）：
 *  1. 能用專案自己離線引入的 Vue 編譯 raw 區塊（沒有瀏覽器可以掛載，但運算式的語法錯誤在編譯時就會拋出）。
 *  2. 樣板用到的每個名稱，都在「與 game.js 的約定」清單內，反過來清單的每個名稱也都被樣板用到，且都寫在樣板的標頭說明裡。
 *     game.js 的 setup() 要回傳的就是這份清單（tests/js/support/game-contract.js，兩邊共用）；「setup() 實際回傳了每一個名稱」的測試在 tests/js/game.test.js（以 `vm` 沙盒執行）。
 * 正式版的編譯器對沒關閉的標籤不會拋錯，標籤是否成對由 tests/test_game_pages.py 檢查。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const TEMPLATE = 'app/templates/game/session.html';

const { CONTRACT, CONTRACT_NAMES } = require('./support/game-contract.js');

const JS_WORDS = new Set(['true', 'false', 'null', 'undefined', 'in', 'of', 'typeof']);
const LOOP_VARS = new Set(['item', 'index', 'line', 'goal', 'place']);

/* 取出 Vue 樣板裡所有運算式（插值與 v-*、:、@ 屬性）與元素參照（ref="…"），回傳其中最外層的識別字（不含屬性存取與字串內容）。 */
function templateNames(html) {
  const body = html.slice(html.indexOf('{% raw %}'), html.indexOf('{% endraw %}'));
  const expressions = [];
  const names = new Set();
  for (const match of body.matchAll(/\{\{([\s\S]*?)\}\}/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\s(?:v-if|v-else-if|v-show|v-model|:[\w-]+|@[\w.-]+)="([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sv-for="[^"]*?\sin\s([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sref="(\w+)"/g)) { names.add(match[1]); }
  for (const expression of expressions) {
    const stripped = expression.replace(/'[^']*'/g, "''");
    for (const match of stripped.matchAll(/(?<![\w.$])([A-Za-z_$][\w$]*)/g)) { names.add(match[1]); }
  }
  return [...names].filter((name) => !JS_WORDS.has(name) && !LOOP_VARS.has(name)).sort();
}

/* 與 tests/js/wizard-template.test.js 相同的做法：編譯器在瀏覽器裡用 DOM 解開 HTML 實體，本範本的屬性沒有使用實體，以原樣回傳的替身滿足它。 */
function loadVueWithCompiler() {
  const sandbox = { console };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  sandbox.document = {
    createElement: () => ({
      set innerHTML(value) { this.html = value; },
      get textContent() { return this.html; },
      get children() {
        const match = /^<div foo="([\s\S]*)">$/.exec(this.html || '');
        return [{ getAttribute: () => (match ? match[1].replace(/&quot;/g, '"') : null) }];
      },
    }),
  };
  vm.createContext(sandbox);
  vm.runInContext(read('app/static/vendor/vue/vue.global.prod.js') + '\n;this.__Vue = Vue;', sandbox);
  return sandbox.__Vue;
}

function rawTemplate() {
  const html = read(TEMPLATE);
  const start = html.indexOf('{% raw %}');
  const end = html.lastIndexOf('{% endraw %}');
  assert.ok(start >= 0 && end > start, '找不到 raw 區塊');
  return '<div>' + html.slice(start + '{% raw %}'.length, end) + '</div>';
}

test('the game template compiles with the offline Vue', () => {
  assert.equal(typeof loadVueWithCompiler().compile(rawTemplate()), 'function');
});

test('a broken expression would be caught by the same check (self test)', () => {
  const Vue = loadVueWithCompiler();
  assert.throws(() => Vue.compile('<div :a="b +"></div>'));
  assert.throws(() => Vue.compile('<div @click="go("></div>'));
});

test('the template uses exactly the names of the contract with game.js, no more and no fewer', () => {
  const used = templateNames(read(TEMPLATE));
  assert.ok(used.length >= 40, `只掃到 ${used.length} 個名稱，掃描可能是空洞的：${used}`);
  const unknown = used.filter((name) => !CONTRACT_NAMES.includes(name));
  assert.deepEqual(unknown, [], `樣板用到約定清單沒有的名稱：${unknown}`);
  const unused = CONTRACT_NAMES.filter((name) => !used.includes(name));
  assert.deepEqual(unused, [], `約定清單有樣板沒用到的名稱：${unused}`);
});

test('every name of the contract is written in the header comment of the template, where game.js work starts from', () => {
  const html = read(TEMPLATE);
  const header = html.slice(0, html.indexOf('{% block title'));
  assert.ok(header.includes('與 game.js 的約定'), '標頭說明沒有約定的段落');
  const missing = CONTRACT_NAMES.filter((name) => !new RegExp(`(?<![\\w])${name}(?![\\w])`).test(header));
  assert.deepEqual(missing, [], `標頭說明缺少：${missing}`);
});

test('the contract has no duplicate names', () => {
  assert.equal(new Set(CONTRACT_NAMES).size, CONTRACT_NAMES.length);
});

test('the template has no inline handler that would write anywhere, and no storage, network or console call', () => {
  const body = read(TEMPLATE);
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie', 'axios', 'fetch(', 'console.', 'location.', 'window.', 'document.']) {
    assert.ok(!body.includes(word), word);
  }
});

test('player and author text is rendered through interpolation, never as HTML', () => {
  const body = read(TEMPLATE);
  assert.ok(!/v-html|innerHTML|\|\s*safe/.test(body));
  // 劇情與行動靠 CSS 的 pre-wrap 保留換行，不靠轉成標籤
  const css = read('app/static/css/game.css');
  assert.ok(/\.gm-story[^{]*\{[^}]*white-space:\s*pre-wrap/.test(css));
  assert.ok(/\.gm-action__text[^{]*\{[^}]*white-space:\s*pre-wrap/.test(css));
});

