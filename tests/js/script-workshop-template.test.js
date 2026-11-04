'use strict';
/* 創作頁的 Vue 範本：用專案自己離線引入的 Vue（static/vendor/vue）編譯 `scripts/workshop.html` 的 raw 區塊。
 * 沒有瀏覽器可以掛載，但運算式的語法錯誤（括號沒對、少了運算元）在編譯時就會拋出，這是最便宜也最容易漏掉的一類錯誤。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');

function loadVue() {
  const sandbox = { console };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  // Vue 的編譯器在瀏覽器裡用 DOM 解開 HTML 實體；範本的屬性沒有使用實體，這裡以原樣回傳的替身滿足它
  sandbox.document = {
    createElement: () => ({
      set innerHTML(value) { this.html = value; },
      get textContent() { return this.html; },
      get children() {
        const match = /^<div foo="([\s\S]*)">$/.exec(this.html || '');
        const value = match ? match[1].replace(/&quot;/g, '"') : null;
        return [{ getAttribute: () => value }];
      },
    }),
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'app/static/vendor/vue/vue.global.prod.js'), 'utf8') + '\n;this.__Vue = Vue;', sandbox);
  return sandbox.__Vue;
}

function vueTemplate(file) {
  const html = fs.readFileSync(path.join(ROOT, file), 'utf8');
  const start = html.indexOf('{% raw %}');
  const end = html.lastIndexOf('{% endraw %}');
  assert.ok(start >= 0 && end > start, '找不到 raw 區塊');
  return '<div>' + html.slice(start + '{% raw %}'.length, end) + '</div>';
}

test('the workshop template compiles', () => {
  const Vue = loadVue();
  const render = Vue.compile(vueTemplate('app/templates/scripts/workshop.html'));
  assert.equal(typeof render, 'function');
});

test('a broken expression would be caught by the same check (self test)', () => {
  const Vue = loadVue();
  assert.throws(() => Vue.compile('<div :a="b +"></div>'));
  assert.throws(() => Vue.compile('<div @click="go("></div>'));
});

test('every name the template uses from the setup result is returned by the workshop script', () => {
  const template = vueTemplate('app/templates/scripts/workshop.html');
  const script = fs.readFileSync(path.join(ROOT, 'app/static/js/scripts/script-workshop.js'), 'utf8');
  const returned = script.slice(script.lastIndexOf('return {'));
  const names = new Set();
  for (const match of template.matchAll(/(?:@click|v-model|v-if|v-else-if|v-for|:disabled|:class|:kind|:key)="([^"]+)"/g)) {
    for (const word of match[1].matchAll(/(?<![\w.'"\[-])([A-Za-z_]\w*)/g)) { names.add(word[1]); }
  }
  const ignored = new Set([
    'in', 'of', 'index', 'script', 'version', 'character', 'place', 'milestone', 'item', 'tier', 'function', 'return', 'true', 'false',
    'null', 'undefined', 'kind', 'status', 'length', 'every', 'version_no', 'author_locked', 'draft', 'pending_review', 'age', 'year',
    'sw', 'chip', 'pending', 'removeTarget',
  ]);
  const missing = [...names].filter((name) => !ignored.has(name) && !new RegExp('\\b' + name + '\\b').test(returned));
  assert.deepEqual(missing, []);
});
