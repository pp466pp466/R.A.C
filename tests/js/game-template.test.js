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
const LOOP_VARS = new Set(['item', 'index', 'line', 'goal', 'place', 'option']);

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

test('the notebook text, the comparison text and the editor are plain interpolations and a textarea bound to the draft, kept readable by pre-wrap', () => {
  const body = read(TEMPLATE);
  assert.ok(/\{\{\s*notebook\.text\s*\}\}/.test(body));
  assert.ok(/\{\{\s*notebookConflict\.text\s*\}\}/.test(body));
  assert.ok(/<textarea[^>]*v-model="notebookDraft"/.test(body));
  assert.ok(/<textarea[^>]*:readonly="notebookLocked"/.test(body));                       // 已完結或儲存中：輸入框唯讀
  const css = read('app/static/css/game.css');
  assert.ok(/\.gm-notebook__text[^{]*\{[^}]*white-space:\s*pre-wrap/.test(css));
  assert.ok(/\.gm-notebook__text[^{]*\{[^}]*overflow-wrap:\s*anywhere/.test(css));         // 長字不撐破側欄
  assert.ok(/\.gm-notebook__input[^{]*\{[^}]*resize:\s*vertical/.test(css));
});

test('the notebook is reachable only through the second sidebar tab and the full-notebook entrance, and the entrance sits outside the collapsible sidebar', () => {
  const body = read(TEMPLATE);
  const calls = [...body.matchAll(/@click="([^"]*)"/g)].map((match) => match[1]);
  assert.ok(calls.includes("selectSideTab('notebook')") && calls.includes('openNotebook'));
  assert.ok(body.indexOf('@click="openNotebook"') < body.indexOf('<aside id="gm-sidebar"'));   // 入口在頂列：側欄收合時也看得到
  for (const method of ['startNotebookEdit', 'saveNotebook', 'cancelNotebookEdit', 'reloadNotebook']) { assert.ok(calls.includes(method), method); }
  assert.ok(!/@(?:input|change|blur|keyup)="[^"]*(?:saveNotebook|notebook)/i.test(body));       // 沒有任何會自動儲存的事件
});

test('player and author text is rendered through interpolation, never as HTML', () => {
  const body = read(TEMPLATE);
  assert.ok(!/v-html|innerHTML|\|\s*safe/.test(body));
  // 劇情與行動靠 CSS 的 pre-wrap 保留換行，不靠轉成標籤
  const css = read('app/static/css/game.css');
  assert.ok(/\.gm-story[^{]*\{[^}]*white-space:\s*pre-wrap/.test(css));
  assert.ok(/\.gm-action__text[^{]*\{[^}]*white-space:\s*pre-wrap/.test(css));
});

// ---- 重新生成與編輯最後一輪（rpg-turn-revision 4.4）-------------------------------------------------------

function modalBlock(html, model) {
  const start = html.indexOf(`<ui-modal v-model="${model}"`);
  assert.ok(start >= 0, model);
  return html.slice(start, html.indexOf('</ui-modal>', start));
}

test('the revision buttons sit under the story of the one turn whose key is reviseKey, and nowhere else (not on the opening, notes or other turns)', () => {
  const html = read(TEMPLATE);
  const turn = html.slice(html.indexOf("<template v-else-if=\"item.kind === 'turn'\">"), html.indexOf("<p v-else-if=\"item.kind === 'note'\""));
  assert.ok(turn.includes('<div v-if="item.key === reviseKey" class="gm-revise">'));
  for (const call of ['openRegenerate', 'openEdit', 'cancelRevision']) { assert.ok(turn.includes(`@click="${call}"`), call); }
  assert.ok(turn.indexOf('{{ item.story }}') < turn.indexOf('class="gm-revise"'));                         // 在劇情的下方
  const outside = html.replace(turn, '');
  assert.ok(!outside.includes('@click="openRegenerate"') && !outside.includes('@click="openEdit"'));      // 只有這一處入口
  assert.equal(html.split('item.key === reviseKey').length - 1, 1);                                       // 按鈕列與說明在同一個區塊裡，只用這一個 key 判斷
});

test('the revision buttons are disabled by the reactive state, the cancel button only shows while a revision runs, and the hint is plain text', () => {
  const html = read(TEMPLATE);
  assert.ok(/<button[^>]*:disabled="reviseDisabled"[^>]*@click="openRegenerate"/.test(html));
  assert.ok(/<button[^>]*:disabled="reviseDisabled"[^>]*@click="openEdit"/.test(html));
  assert.ok(/<button v-if="revisionBusy"[^>]*@click="cancelRevision"/.test(html));
  assert.ok(/\{\{\s*reviseHint\s*\}\}/.test(html) && /\{\{\s*regenerateNote\s*\}\}/.test(html) && /\{\{\s*editNote\s*\}\}/.test(html));
});

test('the regenerate modal is a self-made Vue modal that explains where the reason goes, needs a category and a reason, and shows no hard-coded number', () => {
  const html = read(TEMPLATE);
  const block = modalBlock(html, 'regenerateOpen');
  assert.ok(block.includes('理由會提供營運團隊檢視，不會送給 AI'));
  assert.ok(/<input[^>]*type="text"[^>]*v-model="regenerateReason"/.test(block));                          // 單行輸入：理由不含換行
  assert.ok(block.includes('v-for="option in regenerateOptions"') && block.includes('{{ option.label }}'));
  assert.ok(/<input[^>]*type="radio"[^>]*@change="selectRegenerateCategory\(option\.value\)"/.test(block));
  assert.ok(/:disabled="!regenerateCanSubmit"[^>]*@click="submitRegenerate"/.test(block));
  assert.ok(block.includes('@click="closeRegenerate"'));                                                   // 取消不送出任何請求
  for (const name of ['regenerateCostLine', 'regenerateRule', 'regenerateCount', 'regenerateError']) { assert.ok(block.includes(`{{ ${name} }}`), name); }
  assert.ok(!/(?<![\w.-])\d+(?![\w.-])/.test(block.replace(/status-code="[^"]*"/, '').replace(/class="[^"]*"/g, '')), '理由字數範圍與幣價只能來自程式的狀態，樣板不寫死數字');
});

test('the edit modal takes its free-or-paid explanation from one computed line, no longer carries the old fixed reminder or the reset of the free count, and offers the check-in only when the balance is short', () => {
  const html = read(TEMPLATE);
  const block = modalBlock(html, 'editOpen');
  // 2026-11-16 規則修訂：編輯與重新生成共用免費次數、編輯不歸零；免費或需付費的說明（含「原本那輪已扣的幣不退」）由 editCostLine 依免費次數決定，樣板不再寫死提醒。
  for (const phrase of ['再次消耗遊戲幣', '原本那輪已扣的幣不退', '免費重新生成次數會歸零', '歸零']) { assert.ok(!block.includes(phrase), phrase); }
  assert.equal(block.split('{{ editCostLine }}').length - 1, 1);
  assert.ok(/<p[^>]*role="status"[^>]*>\{\{ editCostLine \}\}<\/p>/.test(block));
  assert.ok(/<textarea[^>]*v-model="editDraft"/.test(block));
  assert.ok(/<button v-if="editInsufficient"[^>]*@click="claimCheckin"/.test(block));
  assert.ok(/:disabled="!editCanSubmit"[^>]*@click="submitEdit"/.test(block));
  assert.ok(/\{\{\s*editError\s*\}\}/.test(block) && /\{\{\s*editHint\s*\}\}/.test(block));
  assert.ok(!/(?<![\w.-])\d+(?![\w.-])/.test(block.replace(/status-code="[^"]*"/, '').replace(/rows="\d+"/, '').replace(/class="[^"]*"/g, '')), '樣板不寫死數字');
});

test('both revision modals come before the wallet modal, so the check-in or insufficient-coins modal stacks on top of them', () => {
  const html = read(TEMPLATE);
  assert.ok(html.indexOf('<ui-modal v-model="regenerateOpen"') < html.indexOf('<ui-modal v-model="editOpen"'));
  assert.ok(html.indexOf('<ui-modal v-model="editOpen"') < html.indexOf('<ui-modal v-model="coinModalOpen"'));
});

test('the revision stylesheet rules keep a long category list and long text inside a 360px screen', () => {
  const css = read('app/static/css/game.css');
  assert.ok(/\.gm-revise__bar[^{]*\{[^}]*flex-wrap:\s*wrap/.test(css));
  assert.ok(/\.gm-revise__row[^{]*\{[^}]*flex-wrap:\s*wrap/.test(css));
  assert.ok(/\.gm-revise__choice[^{]*\{[^}]*display:\s*inline-flex/.test(css));
  assert.ok(!/\.gm-revise[^{]*\{[^}]*(?<![\w-])(?:width|min-width):\s*\d{3,}px/.test(css));
});

