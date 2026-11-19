'use strict';
/* 儲值分頁的示意幣包（wallet-hub 3.1；design D9）的守門測試。
 *  1. 資料模組自己：一定是示意（IS_DEMO 為真）、每張卡片的欄位合格、id 不重複、數量不超過畫面上限。
 *  2. **示意資料只在那個檔案**：整個 app/（Python、樣板、其他 JS、CSS、設定）與 migrations/ 都找不到幣數、贈送數與價格；
 *     掃描條件寫成「數字加幣」「幣別記號加數字」兩種長相（純數字太常見，例如 1000 是免費幣上限、30 是每輪幣價），並附一個自我測試證明掃描不是空洞的。
 *  3. IS_DEMO 為真時畫面一定出現「示意，非正式價格」；資料不是示意就不顯示任何價格。
 *  4. 整個錢包頁的前端模組沒有任何能增加餘額的路徑：唯一的寫入呼叫是簽到，儲值與紀錄只讀。
 * 執行：node --test "tests/js/**" */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Demo = require('../../app/static/js/wallet/wallet-demo-packages.js');
const H = require('../../app/static/js/wallet/wallet-hub-state.js');

const ROOT = path.join(__dirname, '..', '..');
const DEMO_FILE = path.join('app', 'static', 'js', 'wallet', 'wallet-demo-packages.js');
const SKIPPED_DIRS = new Set(['__pycache__', 'vendor', 'fonts', 'node_modules']);
const SCANNED = new Set(['.py', '.html', '.js', '.css', '.json', '.ini', '.mako', '.txt', '.md', '.example']);

function walk(directory, found) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (SKIPPED_DIRS.has(entry.name)) { continue; }
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) { walk(full, found); } else if (SCANNED.has(path.extname(entry.name))) { found.push(full); }
  }
  return found;
}

/* 要掃描的檔案：app/ 與 migrations/ 全部（除了資料模組自己）。 */
function scannedFiles() {
  const files = [];
  for (const root of ['app', 'migrations']) { walk(path.join(ROOT, root), files); }
  for (const extra of ['alembic.ini', '.env.example']) { if (fs.existsSync(path.join(ROOT, extra))) { files.push(path.join(ROOT, extra)); } }
  return files.filter((file) => path.relative(ROOT, file) !== DEMO_FILE);
}

/* 這份資料的「數字加幣」與「幣別記號加數字」長相（含千分位寫法）。 */
function patternsOf(demo) {
  const numbers = new Set();
  for (const pack of demo.PACKAGES) { [pack.coins, pack.bonus].forEach((value) => { if (value > 0) { numbers.add(value); } }); }
  const forms = (value) => [String(value), H.groupDigits(value)].map((text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const coin = [...numbers].flatMap(forms).map((text) => new RegExp(`(?<![\\w,.])${text}\\s*(?:遊戲)?幣`));
  const price = demo.PACKAGES.flatMap((pack) => forms(pack.price)).map((text) => new RegExp(`(?:NT\\$|\\$|新台幣|新臺幣|TWD)\\s*${text}(?![\\w])|(?<![\\w,.])${text}\\s*元(?![\\w])`));
  return [...coin, ...price, /NT\$/, /新台幣|新臺幣/, /\bTWD\b/];
}

function matches(text, patterns) { return patterns.filter((pattern) => pattern.test(text)).map(String); }

// ---- 1. 資料模組 ------------------------------------------------------------------------------------------------

test('the demo data is marked as a demo and each package has a unique id, positive coins, a non-negative bonus and a positive whole price', () => {
  assert.equal(Demo.IS_DEMO, true);
  assert.equal(typeof Demo.CURRENCY, 'string');
  assert.ok(Array.isArray(Demo.PACKAGES) && Demo.PACKAGES.length >= 1 && Demo.PACKAGES.length <= H.MAX_PACKS);
  const ids = new Set();
  for (const pack of Demo.PACKAGES) {
    assert.deepEqual(Object.keys(pack).sort(), ['bonus', 'coins', 'id', 'price']);
    assert.ok(typeof pack.id === 'string' && pack.id !== '' && !ids.has(pack.id), pack.id);
    ids.add(pack.id);
    assert.ok(Number.isInteger(pack.coins) && pack.coins >= 1, pack.id);
    assert.ok(Number.isInteger(pack.bonus) && pack.bonus >= 0, pack.id);
    assert.ok(Number.isInteger(pack.price) && pack.price >= 1, pack.id);
  }
});

test('every package in the data module reaches the screen: none is dropped by the view', () => {
  assert.equal(H.topupViewOf(Demo, 30).cards.length, Demo.PACKAGES.length);
});

test('the data module is plain data: no request, no storage, no console, and it only exports the flag, the currency and the packages', () => {
  const source = fs.readFileSync(path.join(ROOT, DEMO_FILE), 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  for (const word of ['axios', 'fetch(', 'XMLHttpRequest', 'localStorage', 'sessionStorage', 'indexedDB', 'document', 'console.', 'window.', 'import ']) { assert.ok(!code.includes(word), word); }
  assert.deepEqual(Object.keys(Demo).sort(), ['CURRENCY', 'IS_DEMO', 'PACKAGES']);
  assert.match(source, /示意，非正式價格/);                                // 檔頭註明
  assert.match(source, /上線給真實玩家前須換掉或拿掉/);
});

// ---- 2. 資料只在一處 ---------------------------------------------------------------------------------------------

test('the demo packages appear nowhere else: not in the pages, the code, the settings, the migrations or the stylesheets', () => {
  const files = scannedFiles();
  assert.ok(files.length > 100, `只掃到 ${files.length} 個檔案，掃描可能是空洞的`);
  assert.ok(files.some((file) => file.endsWith('hub.html')) && files.some((file) => file.endsWith('wallet_service.py')) && files.some((file) => file.endsWith('wallet-hub.js')));
  const patterns = patternsOf(Demo);
  const found = [];
  for (const file of files) {
    const hits = matches(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''), patterns);
    if (hits.length > 0) { found.push(`${path.relative(ROOT, file)}: ${hits.join(' ')}`); }
  }
  assert.deepEqual(found, [], `示意幣包的數字不得出現在資料模組以外：\n${found.join('\n')}`);
});

test('the scan itself is not hollow: it finds every kind of leak when one is planted', () => {
  const patterns = patternsOf(Demo);
  const big = Demo.PACKAGES.find((pack) => pack.coins >= 1000);
  const withBonus = Demo.PACKAGES.find((pack) => pack.bonus > 0);
  assert.ok(big && withBonus);
  for (const planted of [
    `${big.coins} 幣`, `${H.groupDigits(big.coins)} 幣`, `${big.coins}幣`, `${withBonus.bonus} 遊戲幣`, `NT$${big.price}`, `NT$ ${H.groupDigits(big.price)}`, `${big.price} 元`, `新台幣${big.price}`, '價格 NT$', 'TWD 100',
  ]) {
    assert.ok(matches(`<p>${planted}</p>`, patterns).length > 0, planted);
  }
  // 沒有「示意數字加幣」或「幣別記號加數字」長相的句子不會被誤抓（每輪幣價、簽到數與逾時毫秒數都是別的數字）
  for (const innocent of ['每輪 30 幣', '每日簽到可領取 200 遊戲幣。', 'free_cap = 1000', 'timeout 3000', '第 2 元素', 'seq 500', '簽到 +200']) {
    assert.deepEqual(matches(innocent, patterns), [], innocent);
  }
});

// ---- 3. 示意的標示 -----------------------------------------------------------------------------------------------

test('whenever the data is a demo the screen carries the "not a real price" notice, and with anything else there is no price on screen', () => {
  const view = H.topupViewOf(Demo, 30);
  assert.equal(view.demo, true);
  assert.ok(view.notice.includes('示意，非正式價格'));
  assert.ok(view.cards.length > 0);
  const notDemo = H.topupViewOf({ IS_DEMO: false, CURRENCY: Demo.CURRENCY, PACKAGES: Demo.PACKAGES }, 30);
  assert.deepEqual(notDemo.cards, []);
  assert.ok(!JSON.stringify(notDemo).includes(Demo.CURRENCY));
});

// ---- 4. 沒有增加餘額的路徑 -----------------------------------------------------------------------------------------

test('the wallet page modules call only the three read and check-in functions of the wallet API, and nothing else can send a request', () => {
  const dir = path.join(ROOT, 'app', 'static', 'js', 'wallet');
  const sources = fs.readdirSync(dir).filter((name) => name.endsWith('.js')).map((name) => [name, fs.readFileSync(path.join(dir, name), 'utf8')]);
  assert.ok(sources.length >= 3);
  const calls = new Set();
  for (const [name, source] of sources) {
    const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
    for (const word of ['axios', 'fetch(', 'XMLHttpRequest', 'sendBeacon', 'WebSocket', 'EventSource']) { assert.ok(!code.includes(word), `${name}: ${word}`); }
    for (const match of code.matchAll(/walletApi\.(\w+)/g)) { calls.add(match[1]); }
  }
  assert.deepEqual([...calls].sort(), ['entries']);                    // 頁面直接呼叫的只有帳本（讀）；錢包與簽到經共用的狀態庫與簽到狀態機
  const checkin = fs.readFileSync(path.join(ROOT, 'app', 'static', 'js', 'ui', 'wallet-checkin.js'), 'utf8');
  const api = fs.readFileSync(path.join(ROOT, 'app', 'static', 'js', 'ui', 'wallet-api.js'), 'utf8');
  assert.equal((api.match(/send\('post'/g) || []).length, 1);           // 整個錢包 API 只有一個寫入：簽到
  assert.ok(/send\('post', WALLET_BASE \+ '\/checkin'/.test(api));
  assert.ok(checkin.includes('api.checkin('));
});
