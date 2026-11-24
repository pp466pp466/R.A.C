'use strict';
/* 遊戲主畫面的掛載腳本（game.js）——側欄與 Modal 的接線：
 * 預設收合（桌面與手機都是）、頂列的開關按鈕、桌面推擠與手機覆蓋各自的模式、Esc 與遮罩關閉、不記住狀態（沙盒的儲存物件沒有任何寫入）、
 * 離開確認與放棄在途回合的 Modal 狀態、pagehide 與連線事件的接線，以及 setup() 回傳的名稱與樣板約定完全一致。
 * 環境見 tests/js/support/game-sandbox.js：真的 Vue、Auth、UI 與全部真的 game-*.js 模組，沒有瀏覽器。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setupGame, STATE } = require('./support/game-sandbox.js');
const { settle } = require('./support/vue-runtime.js');
const { CONTRACT_NAMES, CONTRACT } = require('./support/game-contract.js');
/* 頂列顯示的餘額：全站共用的餘額標籤讀的就是 UI.wallet（樣板不再有自己的餘額欄位）。 */
const shownBalance = (game) => game.sandbox.UI.walletLabelOf(game.sandbox.UI.wallet.state).amount;

const DESKTOP = 1280;
const PHONE = 360;
const noEvent = () => ({ key: 'Escape' });

test('the sidebar starts collapsed on a desktop and on a phone, with nothing written to the browser', () => {
  for (const width of [DESKTOP, PHONE, 991, 992]) {
    const { page, storageWrites } = setupGame({ width });
    assert.equal(page.sidebarOpen.value, false, `${width}px`);
    assert.deepEqual(storageWrites, []);
  }
});

test('the toggle button opens and closes the sidebar; on a desktop it pushes the main area, below 992px it overlays with a scrim', () => {
  const desktop = setupGame({ width: DESKTOP });
  assert.equal(desktop.page.sidebarMode.value, 'push');
  assert.equal(desktop.page.toggleSidebar(), true);
  assert.equal(desktop.page.sidebarOpen.value, true);
  assert.equal(desktop.page.sidebarMode.value, 'push');
  assert.equal(desktop.page.toggleSidebar(), false);
  assert.equal(desktop.page.sidebarOpen.value, false);

  const phone = setupGame({ width: PHONE });
  assert.equal(phone.page.sidebarMode.value, 'overlay');
  phone.page.toggleSidebar();
  assert.equal(phone.page.sidebarOpen.value, true);
  assert.equal(phone.page.sidebarMode.value, 'overlay');

  assert.equal(setupGame({ width: 991 }).page.sidebarMode.value, 'overlay');          // 斷點是 992px
  assert.equal(setupGame({ width: 992 }).page.sidebarMode.value, 'push');
});

test('the scrim click (closeSidebar) and Escape both close an open sidebar', () => {
  const phone = setupGame({ width: PHONE });
  phone.page.toggleSidebar();
  phone.page.closeSidebar();                                                          // 遮罩與關閉按鈕
  assert.equal(phone.page.sidebarOpen.value, false);
  phone.page.toggleSidebar();
  phone.fireDocument('keydown', noEvent());
  assert.equal(phone.page.sidebarOpen.value, false);
  phone.fireDocument('keydown', noEvent());                                           // 已收合時再按 Esc 沒有副作用
  assert.equal(phone.page.sidebarOpen.value, false);

  const desktop = setupGame({ width: DESKTOP });
  desktop.page.toggleSidebar();
  desktop.fireDocument('keydown', { key: 'Enter' });
  assert.equal(desktop.page.sidebarOpen.value, true);                                 // 別的鍵不關
  desktop.fireDocument('keydown', noEvent());
  assert.equal(desktop.page.sidebarOpen.value, false);
});

test('Escape does not close the sidebar while a modal is open (the modal takes the key first)', () => {
  const { page, fireDocument } = setupGame({ width: PHONE });
  page.toggleSidebar();
  page.modal.leave = true;
  fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, true);
  page.modal.leave = false;
  page.coinModalOpen.value = true;
  fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, true);
  page.coinModalOpen.value = false;
  fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, false);
});

test('resizing across 992px switches between the pushing and the overlay mode without changing whether the sidebar is open', () => {
  const game = setupGame({ width: DESKTOP });
  game.page.toggleSidebar();
  game.sandbox.innerWidth = PHONE;
  game.fire('resize');
  assert.equal(game.page.sidebarMode.value, 'overlay');
  assert.equal(game.page.sidebarOpen.value, true);
  game.sandbox.innerWidth = DESKTOP;
  game.fire('resize');
  assert.equal(game.page.sidebarMode.value, 'push');
});

test('the sidebar state is never remembered: no browser storage is written by any toggle, and a fresh page is collapsed again', () => {
  const first = setupGame({ width: DESKTOP });
  first.page.toggleSidebar();
  first.page.toggleSidebar();
  first.page.toggleSidebar();
  first.fireDocument('keydown', noEvent());
  first.page.toggleSidebar();
  first.page.closeSidebar();
  assert.deepEqual(first.storageWrites, []);                                          // localStorage、sessionStorage 與 cookie 都沒有被寫
  assert.deepEqual(first.logs, []);
  const reloaded = setupGame({ width: DESKTOP });                                     // 重新整理 = 全新的頁面
  assert.equal(reloaded.page.sidebarOpen.value, false);
  assert.deepEqual(reloaded.storageWrites, []);
});

test('leaving while a turn is running asks first; leaving otherwise follows the plain link', () => {
  const game = setupGame();
  let prevented = 0;
  assert.equal(game.page.leaveClick({ preventDefault: () => { prevented += 1; } }), false);
  assert.equal(prevented, 0);
  assert.equal(game.page.modal.leave, false);
  game.page.sending.value = true;                                                     // 這一輪還在進行
  assert.equal(game.page.leaveClick({ preventDefault: () => { prevented += 1; } }), true);
  assert.equal(prevented, 1);
  assert.equal(game.page.modal.leave, true);
  assert.equal(game.page.confirmLeave(), true);
  assert.equal(game.page.modal.leave, false);
  assert.deepEqual(game.navigations, ['/game']);
});

test('abandoning an in-flight turn is asked first, and only when one exists and nothing is running', () => {
  const game = setupGame();
  assert.equal(game.page.askAbandon(), false);                                        // 沒有在途回合
  assert.equal(game.page.modal.abandon, false);
});

test('pagehide and the connection events are wired, and a pagehide never throws', async () => {
  const game = setupGame();
  for (const type of ['pagehide', 'online', 'offline', 'resize']) { assert.ok((game.listeners[type] || []).length >= 1, type); }
  assert.doesNotThrow(() => game.fire('pagehide'));
  assert.doesNotThrow(() => game.fire('offline'));
  await settle();
  assert.equal(game.page.offline.value, true);
  assert.equal(game.page.sendDisabled.value, true);
  assert.deepEqual(game.fetches, []);                                                 // 離線事件本身不會發出任何請求
});

test('pagehide aborts the request that is still running', async () => {
  const game = setupGame({ respond: () => new Promise(() => {}) });      // 快照請求一直沒有回應
  game.mount();
  await settle();
  assert.equal(game.requests.length, 1);
  assert.equal(game.requests[0].signal.aborted, false);
  game.fire('pagehide');
  assert.equal(game.requests[0].signal.aborted, true);                   // 離開頁面：中止進行中的請求
});

test('mounting loads the save through the snapshot endpoint and keeps the whole screen free of console output and browser storage writes', async () => {
  const snapshot = {
    session: { id: STATE.session.id, title: '青雲問道', content_tier: 'safe', status: 'active', ended: false, version: 1, completed_turns: 0,
      opening: { state: 'ready', text: '第零幕的獨特文字' }, world: { elapsed_months: 0, location: '山門', summary: '', goals: [], places: [], protagonist_age: 16 } },
  };
  const game = setupGame({ respond: (request) => {
    if (/\/api\/game\/sessions\/[^/]+$/.test(request.url)) { return Promise.resolve({ data: snapshot }); }
    if (/\/turns$/.test(request.url)) { return Promise.resolve({ data: { turns: [], has_more: false } }); }
    if (/\/api\/wallet$/.test(request.url)) { return Promise.resolve({ data: { ok: true, wallet: { total: 230, turn_cost: { standard: 30 }, checkin: { claimable: true } } } }); }
    return Promise.reject(new Error('unexpected ' + request.url));
  } });
  await game.mount();
  await settle(20);
  assert.equal(game.page.phase.value, 'ready');
  assert.equal(game.page.items.value.length, 1);
  assert.equal(game.page.items.value[0].text, '第零幕的獨特文字');
  assert.equal(shownBalance(game), '230');
  assert.deepEqual(game.requests.map((request) => request.method), ['get', 'get', 'get']);   // 快照、回合、錢包：全是唯讀
  assert.ok(game.requests.every((request) => request.method === 'get'));
  assert.deepEqual(game.fetches, []);                                                 // 第 0 幕已 ready：沒有串流
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);
});

// ---- 樣板約定 ------------------------------------------------------------------------------------------

test('game.js setup() really returns every name of the contract with game-template, with the kind the template needs', () => {
  const { page } = setupGame();
  const missing = CONTRACT_NAMES.filter((name) => !(name in page));
  assert.deepEqual(missing, [], `樣板用到但 setup() 沒有回傳：${missing}`);
  for (const name of CONTRACT.methods) { assert.equal(typeof page[name], 'function', `${name} 應該是函式`); }
  for (const name of CONTRACT.state) {
    assert.notEqual(typeof page[name], 'function', `${name} 應該是資料，不是函式`);
    assert.ok(page[name] !== undefined, name);
  }
});

test('game.js stays within the contract: it returns nothing the template does not use', () => {
  const { page } = setupGame();
  const extra = Object.keys(page).filter((name) => !CONTRACT_NAMES.includes(name));
  assert.deepEqual(extra, [], `setup() 多回傳了約定清單沒有的名稱：${extra}`);
});

// ---- 記事簿面板（側欄的第二個分頁）-----------------------------------------------------------------------------

const { fakeIndexedDB, keyRange, dump } = require('./support/fake-indexeddb.js');

const NOTEBOOK_MARK = '獨特的記事簿全文Q9';
const NB_PATH = /\/api\/game\/sessions\/[^/]+\/notebook$/;
const nbView = (text, extra) => Object.assign({ text, version: 4, length: Array.from(text).length, capacity: 2000, reserve: 500, full: false }, extra);
const serverError = (status, code, extra) => Promise.reject({ response: { status, data: Object.assign({ ok: false, error: { code, message: '' } }, extra) } });

/* 一個會回應快照、回合、錢包與記事簿的假伺服器。`server.put` 可以換成回傳 Promise 的函式；`server.notebook` 是目前的記事簿。 */
function notebookGame({ summary = { version: 4, length: 12, capacity: 2000, full: false }, text = `${NOTEBOOK_MARK}（已確立的設定）`, width = DESKTOP, indexedDB = null } = {}) {
  const server = { notebook: nbView(text), put: null, hang: false };
  const respond = (request) => {
    if (NB_PATH.test(request.url)) {
      if (request.method === 'get') { return server.hang ? new Promise(() => {}) : Promise.resolve({ data: { notebook: server.notebook } }); }
      return server.put ? server.put(request) : Promise.reject(new Error('unexpected PUT'));
    }
    if (/\/api\/game\/sessions\/[^/]+$/.test(request.url)) {
      return Promise.resolve({ data: { session: { id: STATE.session.id, title: '青雲問道', content_tier: 'safe', status: 'active', ended: false, version: 1, completed_turns: 0,
        opening: { state: 'ready', text: '第零幕。' }, world: { elapsed_months: 0, location: '山門', summary: '', goals: [], places: [], protagonist_age: 16 }, notebook: summary } } });
    }
    if (/\/turns$/.test(request.url)) { return Promise.resolve({ data: { turns: [], has_more: false } }); }
    if (/\/api\/wallet$/.test(request.url)) { return Promise.resolve({ data: { ok: true, wallet: { total: 230, turn_cost: { standard: 30 }, checkin: { claimable: true } } } }); }
    return Promise.reject(new Error('unexpected ' + request.method + ' ' + request.url));
  };
  const game = setupGame({ respond, width, indexedDB, keyRange: indexedDB ? keyRange : null });
  const notebookRequests = () => game.requests.filter((request) => NB_PATH.test(request.url));
  return { game, server, notebookRequests };
}

async function mounted(options) {
  const env = notebookGame(options);
  await env.game.mount();
  await settle(20);
  return env;
}

test('loading the page never asks for the notebook, and neither does opening the sidebar on the world tab or picking the notebook tab while it is closed', async () => {
  const { game, notebookRequests } = await mounted();
  const { page } = game;
  assert.equal(page.sideTab.value, 'world');
  assert.deepEqual(notebookRequests(), []);                                  // 載入頁面：沒有記事簿請求（全文只在開啟面板時才讀）
  page.toggleSidebar();                                                       // 開在「世界」分頁
  await settle();
  assert.deepEqual(notebookRequests(), []);
  page.toggleSidebar();                                                       // 收合
  assert.equal(page.selectSideTab('notebook'), true);                         // 側欄收合時切分頁：還不算開啟
  await settle();
  assert.deepEqual(notebookRequests(), []);
  assert.equal(page.selectSideTab('bogus'), false);                           // 不認得的分頁名稱：什麼都不做
  assert.equal(page.sideTab.value, 'notebook');
});

test('the notebook is read every time the panel is opened: the sidebar opens on the notebook tab, or the tab is picked while the sidebar is open', async () => {
  const { game, notebookRequests } = await mounted();
  const { page } = game;
  page.selectSideTab('notebook');
  page.toggleSidebar();                                                       // 開啟側欄且目前是記事簿分頁：讀一次
  await settle();
  assert.equal(notebookRequests().length, 1);
  assert.equal(notebookRequests()[0].method, 'get');
  assert.equal(page.notebook.value.text, `${NOTEBOOK_MARK}（已確立的設定）`);
  page.selectSideTab('world');
  await settle();
  assert.equal(notebookRequests().length, 1);                                 // 切到世界：不讀
  page.selectSideTab('notebook');                                             // 側欄開著時切回記事簿：再讀一次
  await settle();
  assert.equal(notebookRequests().length, 2);
  page.toggleSidebar();                                                       // 收合
  page.toggleSidebar();                                                       // 再開啟：再讀一次
  await settle();
  assert.equal(notebookRequests().length, 3);
  assert.deepEqual(notebookRequests().map((request) => request.method), ['get', 'get', 'get']);
});

test('the "notebook is full" entrance comes from the snapshot summary alone; one click opens the sidebar on the notebook tab and reads it', async () => {
  const full = await mounted({ summary: { version: 5, length: 2240, capacity: 2000, full: true } });
  assert.equal(full.game.page.notebookFull.value, true);                       // 頂列的入口與分頁徽章共用
  assert.equal(full.game.page.notebookUsage.value, '2240 / 2000');
  assert.deepEqual(full.notebookRequests(), []);                               // 只靠快照的摘要，沒有請求全文
  assert.equal(full.game.page.sidebarOpen.value, false);
  assert.equal(full.game.page.openNotebook(), true);
  assert.equal(full.game.page.sidebarOpen.value, true);
  assert.equal(full.game.page.sideTab.value, 'notebook');
  await settle();
  assert.equal(full.notebookRequests().length, 1);

  const exact = await mounted({ summary: { version: 5, length: 2000, capacity: 2000, full: false } });
  assert.equal(exact.game.page.notebookFull.value, false);                     // 剛好用完容量：不顯示
  const none = await mounted({ summary: null });
  assert.equal(none.game.page.notebookFull.value, false);                      // 快照沒有摘要：不顯示
});

test('editing through the real API sends exactly one PUT with the draft and the version, and a second click while it runs adds nothing', async () => {
  const { game, server, notebookRequests } = await mounted();
  const { page } = game;
  let release = null;
  server.put = () => new Promise((resolve) => { release = () => resolve({ data: { notebook: nbView('新的設定', { version: 5 }) } }); });
  page.openNotebook();
  await settle();
  assert.equal(page.startNotebookEdit(), true);
  page.notebookDraft.value = '新的設定';
  const first = page.saveNotebook();
  const second = page.saveNotebook();
  await settle();
  const puts = notebookRequests().filter((request) => request.method === 'put');
  assert.equal(puts.length, 1);                                                // 重複點擊只送一個
  assert.deepEqual(puts[0].data, { text: '新的設定', expected_version: 4 });
  assert.equal(page.notebookSaving.value, true);
  release();
  assert.equal(await first, true);
  assert.equal(await second, false);
  assert.equal(page.notebook.value.text, '新的設定');
  assert.equal(page.notebookEditing.value, false);
  assert.equal(page.notebookSaving.value, false);
  assert.deepEqual(game.toasts, ['記事簿已儲存。']);
  assert.ok(game.requests.every((request) => request.method === 'get' || (request.method === 'put' && NB_PATH.test(request.url))));   // 沒有別的寫入
});

test('a tab that is editing the notebook checks the version when it comes back to the foreground (visibilitychange or focus); nothing is requested when it is hidden or not editing', async () => {
  const { game, server, notebookRequests } = await mounted();
  const { page } = game;
  game.fireDocument('visibilitychange');
  game.fire('focus');
  await settle();
  assert.deepEqual(notebookRequests(), []);                                    // 還沒開面板：回到前景不請求

  page.openNotebook();
  await settle();
  assert.equal(notebookRequests().length, 1);
  game.fireDocument('visibilitychange');
  game.fire('focus');
  await settle();
  assert.equal(notebookRequests().length, 1);                                  // 開著面板但在檢視：不在背景重讀

  assert.equal(page.startNotebookEdit(), true);
  game.sandbox.document.visibilityState = 'hidden';
  game.fireDocument('visibilitychange');                                       // 切走（分頁隱藏）時不檢查
  await settle();
  assert.equal(notebookRequests().length, 1);

  game.sandbox.document.visibilityState = 'visible';
  server.notebook = nbView('另一個分頁存的新設定', { version: 6 });
  game.fireDocument('visibilitychange');                                       // 只有 visibilitychange（切換分頁）：問一次最新版本
  await settle();
  assert.equal(notebookRequests().length, 2);
  assert.equal(notebookRequests()[1].method, 'get');
  assert.equal(page.notebook.value.version, 6);
  assert.equal(page.notebookDraft.value, '另一個分頁存的新設定');                // 玩家沒改過：換成最新內容
  assert.equal(page.notebookNotice.value.text, '另一個分頁或裝置剛更新了記事簿，編輯框已換成最新內容。');
  assert.deepEqual(game.toasts, ['記事簿剛在別處更新過，請看編輯框下方的最新內容。']);
  assert.equal(page.notebookSaveHint.value, '還沒有修改內容，沒有需要儲存的變更。');

  server.notebook = nbView('又被更新了', { version: 7 });
  game.fire('focus');                                                          // 只有 focus（視窗切換，分頁沒有被隱藏）：也要檢查
  await settle();
  assert.equal(notebookRequests().length, 3);
  assert.equal(page.notebook.value.version, 7);
  assert.equal(page.notebookDraft.value, '又被更新了');

  server.notebook = nbView('第三次更新', { version: 8 });
  game.fireDocument('visibilitychange');                                       // 兩個事件同時來：同一時間只有一個檢查
  game.fire('focus');
  await settle();
  assert.equal(notebookRequests().length, 4);
  assert.equal(page.notebookDraft.value, '第三次更新');
  assert.ok(game.requests.every((request) => request.method === 'get'));       // 檢查不儲存任何東西
});

test('failures from the real API: a conflict keeps the draft, a rule hit names only the category, an ended save turns the whole page into read-only', async () => {
  const { game, server } = await mounted();
  const { page } = game;
  page.openNotebook();
  await settle();
  page.startNotebookEdit();
  page.notebookDraft.value = `${NOTEBOOK_MARK}我的版本`;

  server.put = () => serverError(409, 'notebook_conflict', { notebook: nbView('AI 剛寫入', { version: 6 }) });
  await page.saveNotebook();
  assert.equal(page.notebook.value.text, 'AI 剛寫入');
  assert.equal(page.notebookConflict.value.version, 6);
  assert.equal(page.notebookDraft.value, `${NOTEBOOK_MARK}我的版本`);
  assert.match(page.notebookNotice.value.text, /AI 或另一個裝置剛更新了記事簿/);

  server.put = () => serverError(422, 'content_rejected', { categories: ['sexual_violence'] });
  await page.saveNotebook();
  assert.match(page.notebookNotice.value.text, /性暴力/);
  assert.ok(!page.notebookNotice.value.text.includes(NOTEBOOK_MARK));

  server.put = () => serverError(422, 'notebook_too_long', { length: 2100, capacity: 2000 });
  await page.saveNotebook();
  assert.match(page.notebookNotice.value.text, /2100 \/ 2000/);

  assert.equal(page.ended.value, false);
  server.put = () => serverError(409, 'session_ended');
  await page.saveNotebook();
  assert.equal(page.ended.value, true);                                         // 整個遊戲頁都標示為已完結（頂列、輸入區禁用）
  assert.equal(page.notebookLocked.value, true);
  assert.equal(page.sendDisabled.value, true);
  assert.equal(page.notebookDraft.value, `${NOTEBOOK_MARK}我的版本`);
});

test('the whole notebook flow keeps the text out of IndexedDB, the console, and every other browser storage', async () => {
  const factory = fakeIndexedDB();
  const { game, server } = await mounted({ indexedDB: factory, summary: { version: 5, length: 2240, capacity: 2000, full: true } });
  const { page } = game;
  server.put = () => Promise.resolve({ data: { notebook: nbView(`${NOTEBOOK_MARK}存好了`, { version: 6 }) } });
  page.openNotebook();
  await settle();
  page.selectSideTab('world');
  page.selectSideTab('notebook');
  await settle();
  page.startNotebookEdit();
  page.notebookDraft.value = `${NOTEBOOK_MARK}存好了`;
  await page.saveNotebook();
  await settle(40);
  const rows = dump(factory);
  assert.ok(rows.length >= 1, '暫存沒有寫入任何東西，這項檢查是空洞的');
  assert.ok(JSON.stringify(rows).includes('青雲問道'));                         // 存檔摘要確實進了暫存
  const stored = JSON.stringify(rows);
  assert.ok(!stored.includes(NOTEBOOK_MARK) && !/notebook/i.test(stored) && !stored.includes('2240'), '記事簿全文或摘要進了 IndexedDB');
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);                                      // 分頁與側欄都不記住
});

test('Escape does not close the sidebar while the "discard unsaved changes" confirmation is open', async () => {
  const { game } = await mounted();
  const { page } = game;
  page.openNotebook();
  await settle();
  page.startNotebookEdit();
  page.notebookDraft.value = '改到一半';
  assert.equal(page.cancelNotebookEdit(), true);
  assert.equal(page.notebookDiscardOpen.value, true);
  game.fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, true);                                    // Modal 先吃這個鍵
  assert.equal(page.confirmNotebookDiscard(), true);
  assert.equal(page.notebookDiscardOpen.value, false);
  assert.equal(page.notebookEditing.value, false);
  game.fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, false);
});

test('pagehide aborts a notebook read that is still running, and nothing is ever saved by leaving the page', async () => {
  const { game, server, notebookRequests } = await mounted();
  server.hang = true;
  game.page.openNotebook();
  await settle();
  assert.equal(notebookRequests().length, 1);
  assert.equal(notebookRequests()[0].signal.aborted, false);
  game.fire('pagehide');
  assert.equal(notebookRequests()[0].signal.aborted, true);
  assert.equal(notebookRequests().filter((request) => request.method === 'put').length, 0);
});

test('pagehide with an unsaved draft sends no save request', async () => {
  const { game, notebookRequests } = await mounted();
  game.page.openNotebook();
  await settle();
  game.page.startNotebookEdit();
  game.page.notebookDraft.value = '編輯到一半';
  game.fire('pagehide');
  await settle(30);
  assert.equal(notebookRequests().filter((request) => request.method === 'put').length, 0);
  assert.equal(game.page.notebookDraft.value, '編輯到一半');
});

// ---- 重新生成與編輯最後一輪（rpg-turn-revision 4.4）-------------------------------------------------------

const REVISION_REASON = '理由的獨特字樣Z7重複又無聊';
const SESSION_PATH = /\/api\/game\/sessions\/[^/]+$/;

/* 一個有兩輪、最後一輪可修訂的假伺服器；串流端點（`fetch`）的連線一直開著直到被中止。 */
function revisionGame({ indexedDB = null, lastTurn = {}, wallet = {} } = {}) {
  const streams = [];
  const respond = (request) => {
    if (SESSION_PATH.test(request.url)) {
      return Promise.resolve({ data: { session: {
        id: STATE.session.id, title: '青雲問道', content_tier: 'safe', status: 'active', ended: false, version: 1, completed_turns: 2,
        opening: { state: 'ready', text: '第零幕。' }, world: { elapsed_months: 0, location: '山門', summary: '', goals: [], places: [], protagonist_age: 16 },
        notebook: { version: 0, length: 0, capacity: 2000, full: false },
        last_turn: Object.assign({ client_msg_id: 't2', seq: 2, revision: 1, revisable: true, reason: null, free_left: 2, regenerate_cost: 0, edit_cost: 0 }, lastTurn),
      } } });
    }
    if (/\/turns$/.test(request.url)) {
      return Promise.resolve({ data: { has_more: false, turns: [
        { seq: 1, client_msg_id: 't1', turn_number: 1, status: 'complete', action: '行動1', story: '劇情1', revision: 1 },
        { seq: 2, client_msg_id: 't2', turn_number: 2, status: 'complete', action: '行動2', story: '劇情2', revision: 1 },
      ] } });
    }
    if (/\/api\/wallet$/.test(request.url)) { return Promise.resolve({ data: { ok: true, wallet: Object.assign({ total: 230, turn_cost: { standard: 30 }, checkin: { claimable: true } }, wallet) } }); }
    return Promise.reject(new Error('unexpected ' + request.method + ' ' + request.url));
  };
  const fetchImpl = (url, init) => {
    streams.push({ url, init, body: JSON.parse(init.body) });
    return new Promise((resolve, reject) => { init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))); });
  };
  const game = setupGame({ respond, fetchImpl, indexedDB, keyRange: indexedDB ? keyRange : null });
  return { game, streams };
}

async function revisionMounted(options) {
  const env = revisionGame(options);
  await env.game.mount();
  await waitFor(() => env.game.page.reviseKey.value && !env.game.page.reviseDisabled.value || options && options.lastTurn);      // 假的 IndexedDB 要多等一會兒才開好
  await settle(10);
  return env;
}

/* 以真實的計時輪詢等條件成立（最多約 2 秒）；假的 IndexedDB 用計時器排程，光靠 setImmediate 的輪數會不穩定。 */
async function waitFor(condition) {
  for (let round = 0; round < 400 && !condition(); round += 1) { await new Promise((resolve) => setTimeout(resolve, 5)); }
}

function startRegenerate(page) {
  assert.equal(page.openRegenerate(), true);
  page.selectRegenerateCategory('repetitive_or_dull');
  page.regenerateReason.value = REVISION_REASON;
  return page.submitRegenerate();
}

test('mounting the real page shows the revision buttons under the last turn of the snapshot, with the free count and the price', async () => {
  const { game } = await revisionMounted();
  const { page } = game;
  assert.equal(page.reviseKey.value, 'turn:t2');
  assert.equal(page.reviseDisabled.value, false);
  assert.equal(page.regenerateNote.value, '免費還剩 2 次');
  assert.equal(page.editNote.value, '免費還剩 2 次');                                // 編輯與重新生成共用同一組免費次數
  assert.deepEqual(Array.from(page.items.value.filter((item) => item.key === page.reviseKey.value).map((item) => item.story)), ['劇情2']);
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);
});

// admin-unlimited-coins：整頁掛載（真的 game.js 接線）——管理員餘額 0、免費次數用完，畫面不提示餘額不足，修訂的文字與按鈕狀態都說明不消耗遊戲幣。
const EXEMPT_PAGE = {
  wallet: { total: 0, turn_cost: { standard: 0 }, coin_exempt: true },
  lastTurn: { free_left: 0, regenerate_cost: 0, edit_cost: 0 },
};

test('the real page of an exempt account with a zero balance shows no low-balance hint and says the revisions do not consume game coins', async () => {
  const { game } = await revisionMounted(EXEMPT_PAGE);
  const { page } = game;
  await waitFor(() => shownBalance(game) === '0' && page.reviseKey.value);
  assert.equal(shownBalance(game), '0');                                          // 餘額照實顯示
  assert.equal(page.lowBalanceText.value, '');
  assert.equal(page.reviseKey.value, 'turn:t2');
  assert.equal(page.reviseDisabled.value, false);
  assert.equal(page.regenerateNote.value, '不消耗遊戲幣');
  assert.equal(page.editNote.value, '不消耗遊戲幣');
  assert.equal(page.openRegenerate(), true);
  assert.equal(page.regenerateCostLine.value, '這次重新生成不消耗遊戲幣。');
  page.closeRegenerate();
  assert.equal(page.openEdit(), true);
  page.editDraft.value = '走後門';
  assert.equal(page.editCostLine.value, '這次編輯不消耗遊戲幣。');
  assert.equal(page.editInsufficient.value, false);
  assert.equal(page.editCanSubmit.value, true);
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);
});

test('the real page of an ordinary account in the same situation keeps the hint, the price and the blocked edit', async () => {
  const { game } = await revisionMounted({ wallet: { total: 0, turn_cost: { standard: 30 }, coin_exempt: false }, lastTurn: { free_left: 0, regenerate_cost: 30, edit_cost: 30 } });
  const { page } = game;
  await waitFor(() => shownBalance(game) === '0' && page.reviseKey.value);
  assert.ok(page.lowBalanceText.value.includes('每一輪需要 30 幣'));
  assert.equal(page.regenerateNote.value, '30 幣');
  assert.equal(page.editNote.value, '30 幣');
  assert.equal(page.openEdit(), true);
  page.editDraft.value = '走後門';
  assert.equal(page.editInsufficient.value, true);
  assert.equal(page.editCanSubmit.value, false);
});

test('the real page has no revision buttons when the snapshot says it is not the last turn, and disabled ones with a reason while something is in progress', async () => {
  const notLast = await revisionMounted({ lastTurn: { revisable: false, reason: 'not_last' } });
  assert.equal(notLast.game.page.reviseKey.value, '');
  const waiting = await revisionMounted({ lastTurn: { revisable: false, reason: 'in_progress' } });
  assert.equal(waiting.game.page.reviseKey.value, 'turn:t2');
  assert.equal(waiting.game.page.reviseDisabled.value, true);
  assert.ok(waiting.game.page.reviseHint.value.includes('處理中'));
});

test('Escape does not close the sidebar while the regenerate or the edit modal is open', async () => {
  const { game } = await revisionMounted({});
  const { page } = game;
  page.toggleSidebar();
  assert.equal(page.openRegenerate(), true);
  game.fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, true);
  page.closeRegenerate();
  assert.equal(page.openEdit(), true);
  game.fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, true);
  page.closeEdit();
  game.fireDocument('keydown', noEvent());
  assert.equal(page.sidebarOpen.value, false);
});

test('while a regenerate is running leaving asks first, a new action cannot be sent, and the request carries the right URL, body and token', async () => {
  const { game, streams } = await revisionMounted();
  const { page } = game;
  const running = startRegenerate(page);
  await settle(10);
  assert.equal(page.revisionBusy.value, true);
  assert.equal(streams.length, 1);
  assert.ok(streams[0].url.endsWith('/turns/t2/regenerate/stream'));
  assert.deepEqual(streams[0].body, { expected_revision: 1, category: 'repetitive_or_dull', reason: REVISION_REASON });
  assert.equal(streams[0].init.headers['X-CSRF-Token'], 'CSRF-TOKEN');
  assert.equal(page.sendDisabled.value, true);
  page.draft.value = '新的行動';
  assert.equal(await page.send(), false);
  assert.equal(streams.length, 1);                                                // 修訂中不得送出新行動
  let prevented = 0;
  assert.equal(page.leaveClick({ preventDefault: () => { prevented += 1; } }), true);
  assert.equal(prevented, 1);
  assert.equal(page.modal.leave, true);
  assert.equal(page.confirmLeave(), true);
  assert.equal(streams[0].init.signal.aborted, true);                             // 離開：中止請求
  assert.deepEqual(game.navigations, ['/game']);
  await running;
  assert.equal(page.revisionBusy.value, false);
});

test('pagehide aborts a running regenerate, and the page ends with the busy flag cleared and the old content shown', async () => {
  const { game, streams } = await revisionMounted();
  const { page } = game;
  const running = startRegenerate(page);
  await settle(10);
  assert.equal(streams[0].init.signal.aborted, false);
  game.fire('pagehide');
  assert.equal(streams[0].init.signal.aborted, true);
  await running;
  await settle(20);
  assert.equal(page.revisionBusy.value, false);
  assert.deepEqual(Array.from(page.items.value.filter((item) => item.key === 'turn:t2').map((item) => item.story)), ['劇情2']);
});

test('the cancel method stops a running edit, and the edit modal reopens with the original action', async () => {
  const { game, streams } = await revisionMounted();
  const { page } = game;
  assert.equal(page.openEdit(), true);
  assert.equal(page.editDraft.value, '行動2');
  page.editDraft.value = '改成別的行動';
  const running = page.submitEdit();
  await settle(10);
  assert.ok(streams[0].url.endsWith('/turns/t2/edit/stream'));
  assert.deepEqual(streams[0].body, { expected_revision: 1, action: '改成別的行動' });
  assert.equal(page.cancelRevision(), true);
  await running;
  await settle(20);
  assert.equal(page.revisionBusy.value, false);
  assert.equal(page.openEdit(), true);
  assert.equal(page.editDraft.value, '行動2');                                    // 取消之後還是原本的行動
});

test('the whole revision flow keeps the reason and the category out of IndexedDB, the console and every other browser storage', async () => {
  const factory = fakeIndexedDB();
  const { game, streams } = await revisionMounted({ indexedDB: factory });
  const { page } = game;
  const running = startRegenerate(page);
  await waitFor(() => streams.length > 0);                                       // 假的 IndexedDB 寫完在途紀錄之後才會發出請求
  assert.equal(streams.length, 1);
  const during = dump(factory);
  const record = during.find((row) => row.collection === 'inflight');
  assert.ok(record, '修訂進行中應該有一筆在途紀錄');
  assert.equal(record.value.kind, 'regenerate');
  assert.equal(record.value.expected_revision, 1);
  game.fire('pagehide');
  await running;
  await settle(30);
  const stored = JSON.stringify(dump(factory)) + JSON.stringify(during);
  for (const marker of [REVISION_REASON, 'repetitive_or_dull', '"reason"', '"category"']) { assert.ok(!stored.includes(marker), marker); }
  assert.ok(stored.includes('青雲問道'), '暫存沒有寫入任何東西，這項檢查是空洞的');
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);
  assert.ok(JSON.stringify(streams[0].body).includes(REVISION_REASON));          // 理由只在請求本文
  assert.ok(!JSON.stringify(game.toasts).includes(REVISION_REASON));
});

// ---- 頂列的餘額標籤與頁面共用同一份錢包狀態（wallet-hub 1.5）-----------------------------------------------------

const walletPage = (wallet, options = {}) => {
  const snapshot = {
    session: { id: STATE.session.id, title: '青雲問道', content_tier: 'safe', status: 'active', ended: false, version: 1, completed_turns: 0,
      opening: { state: 'ready', text: '第零幕' }, world: { elapsed_months: 0, location: '山門', summary: '', goals: [], places: [], protagonist_age: 16 } },
  };
  return setupGame({ ...options, respond: (request) => {
    if (/\/api\/game\/sessions\/[^/]+$/.test(request.url)) { return Promise.resolve({ data: snapshot }); }
    if (/\/turns$/.test(request.url)) { return Promise.resolve({ data: { turns: [], has_more: false } }); }
    if (/\/api\/wallet$/.test(request.url)) { return wallet instanceof Error ? Promise.reject({ response: { status: 503, data: { error: { code: 'wallet_busy' } } } }) : Promise.resolve({ data: { ok: true, wallet } }); }
    return Promise.reject(new Error('unexpected ' + request.url));
  } });
};
const walletRequests = (game) => game.requests.filter((request) => /\/api\/wallet$/.test(request.url));

test('the page and the top-bar label share one wallet request, whichever of them asks first', async () => {
  const wallet = { total: 230, free_balance: 230, paid_balance: 0, turn_cost: { standard: 30 }, coin_exempt: false, checkin: { claimable: true } };
  const labelFirst = walletPage(wallet);
  const pending = labelFirst.sandbox.UI.wallet.load();                                // 標籤的 onMounted 先到：請求已經在途
  await labelFirst.mount();
  await settle(20);
  assert.equal(await pending, true);
  assert.equal(walletRequests(labelFirst).length, 1);                                  // 頁面自己的載入合併進同一個請求
  assert.equal(shownBalance(labelFirst), '230');

  const pageFirst = walletPage(wallet);
  await pageFirst.mount();
  await settle(20);
  assert.equal(await pageFirst.sandbox.UI.wallet.load(), true);                        // 標籤之後才掛載：已讀到，不再請求
  assert.equal(walletRequests(pageFirst).length, 1);
  assert.equal(shownBalance(pageFirst), '230');
});

test('a finished turn (done.coins) updates the label and the low-balance hint from the same state with no further wallet request', async () => {
  const game = walletPage({ total: 230, free_balance: 230, paid_balance: 0, turn_cost: { standard: 30 }, coin_exempt: false, checkin: { claimable: true } });
  await game.mount();
  await settle(20);
  assert.equal(shownBalance(game), '230');
  assert.equal(game.page.lowBalanceText.value, '');
  game.sandbox.UI.wallet.setTotal(20);                                                 // done.coins 經由 wallet.setBalance 寫入的就是這份狀態
  assert.equal(shownBalance(game), '20');
  assert.ok(game.page.lowBalanceText.value.includes('目前餘額 20 幣'));                  // 輸入區提示依同一份數字
  assert.equal(walletRequests(game).length, 1);
});

test('an exempt account with a zero balance shows 0 and the note on the label and no low-balance hint', async () => {
  const game = walletPage({ total: 0, free_balance: 0, paid_balance: 0, turn_cost: { standard: 0 }, coin_exempt: true, checkin: { claimable: true } });
  await game.mount();
  await settle(20);
  const label = game.sandbox.UI.walletLabelOf(game.sandbox.UI.wallet.state);
  assert.equal(label.amount, '0');
  assert.equal(label.exempt, true);
  assert.equal(game.page.lowBalanceText.value, '');
});

test('when the wallet cannot be read the label shows a dash while the page itself stays usable', async () => {
  const game = walletPage(new Error('busy'));
  await game.mount();
  await settle(20);
  assert.equal(shownBalance(game), '—');
  assert.equal(game.sandbox.UI.wallet.state.status, 'failed');
  assert.equal(game.page.phase.value, 'ready');
  assert.equal(game.page.inputDisabled.value, false);
  assert.deepEqual(game.logs, []);
  assert.deepEqual(game.storageWrites, []);
});

test('a 401 on the wallet request still sends the player to the login page when the label started the request', async () => {
  const unauthorized = () => Promise.reject({ response: { status: 401, data: { error: { code: 'unauthorized' } } } });
  const game = setupGame({ respond: unauthorized });
  await game.sandbox.UI.wallet.load();                                                 // 標籤發起的請求（頁面自己的載入還沒開始）
  await settle();
  assert.equal(game.sandbox.UI.wallet.state.status, 'failed');
  assert.ok(game.navigations.some((target) => String(target).startsWith('/login')), '登入已過期應該導向登入頁');
});

// ---- 劇情朗讀的注入與生命週期（rpg-story-tts：game.js）------------------------------------------------------------------------

const Speech = require('./support/fake-speech.js');

test('game.js hands the browser speech objects to the session: the voice list is read and listened to, and nothing is spoken by itself', () => {
  const synth = Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] });
  const game = setupGame({ speech: { synth, Utterance: Speech.FakeUtterance } });
  assert.equal(synth.listenerCount, 1);                                               // 朗讀器已建立並在聽語音清單的變化
  assert.deepEqual(synth.speaks, []);
  assert.deepEqual(game.storageWrites, []);                                           // 載入頁面不寫任何瀏覽器儲存
  assert.deepEqual(game.logs, []);
});

test('a pagehide destroys the speaker: whatever is speaking stops and the voice-list listener is removed', () => {
  const synth = Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] });
  const game = setupGame({ speech: { synth, Utterance: Speech.FakeUtterance } });
  const before = synth.cancels;
  game.fire('pagehide');
  assert.ok(synth.cancels > before, 'pagehide 之後呼叫過 cancel()');
  assert.equal(synth.listenerCount, 0);
  assert.doesNotThrow(() => game.fire('pagehide'));                                   // 重複觸發不丟例外
});

test('leaving through the confirm window destroys the speaker too', () => {
  const synth = Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] });
  const game = setupGame({ speech: { synth, Utterance: Speech.FakeUtterance } });
  game.page.confirmLeave();
  assert.equal(synth.listenerCount, 0);
  assert.deepEqual(game.navigations, ['/game']);
});

test('without speech objects, or when reading them throws, or when localStorage is blocked, the page mounts and works as before', async () => {
  for (const options of [{}, { speechThrows: true }]) {
    const game = setupGame(options);
    assert.doesNotThrow(() => game.fire('pagehide'));
    assert.equal(game.page.phase.value, 'loading');
    assert.deepEqual(game.logs, []);
  }
  const blocked = {
    getItem() { throw new Error('SecurityError'); },
    setItem() { throw new Error('SecurityError'); },
  };
  const synth = Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] });
  const game = setupGame({ speech: { synth, Utterance: Speech.FakeUtterance }, localStorageImpl: blocked });
  assert.equal(game.page.phase.value, 'loading');
  assert.doesNotThrow(() => game.fire('pagehide'));
});

test('the voice menu is one of the windows: it is a modal flag, and while it is open Escape does not close the sidebar', () => {
  const game = setupGame({ width: 360, speech: { synth: Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] }), Utterance: Speech.FakeUtterance } });
  assert.equal(game.page.modal.voice, false);
  game.page.toggleSidebar();
  game.page.modal.voice = true;                                                       // 語音選單開啟（樣板以 v-model 綁這個旗標）
  game.fireDocument('keydown', { key: 'Escape' });
  assert.equal(game.page.sidebarOpen.value, true);                                    // 有 Modal 開啟時 Esc 不動側欄
  game.page.modal.voice = false;
  game.fireDocument('keydown', { key: 'Escape' });
  assert.equal(game.page.sidebarOpen.value, false);
});

test('loading the page with speech support writes nothing to localStorage until a voice is chosen', () => {
  const written = [];
  const memory = { getItem: () => null, setItem: (key, value) => { written.push([key, value]); } };
  const synth = Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL, Speech.ZH_TW_ONLINE] });
  const game = setupGame({ speech: { synth, Utterance: Speech.FakeUtterance }, localStorageImpl: memory });
  assert.deepEqual(written, []);                                                      // 還沒選語音：沒有任何寫入
  assert.deepEqual(game.storageWrites, []);
});

test('opening the voice menu puts the focus on the chosen option (or the first, "automatic"), and closing it gives the focus back to the menu button', async () => {
  const game = setupGame({ speech: { synth: Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] }), Utterance: Speech.FakeUtterance } });
  const focused = [];
  const element = (name) => ({ focus: () => focused.push(name) });
  game.page.voiceMenuButton.value = element('menu-button');
  const checked = element('checked-option');
  const first = element('first-option');
  let hasChecked = true;
  game.page.voiceList.value = { querySelector: (selector) => (selector === 'input:checked' ? (hasChecked ? checked : null) : (selector === 'input' ? first : null)) };

  game.page.modal.voice = true;
  await settle();
  assert.deepEqual(focused, ['checked-option']);                                       // 目前選中的選項

  game.page.modal.voice = false;
  await settle();
  assert.deepEqual(focused, ['checked-option', 'menu-button']);                        // 關閉之後回到選單按鈕

  hasChecked = false;                                                                  // 還沒有選中的：落在第一個（「自動」）
  game.page.modal.voice = true;
  await settle();
  assert.deepEqual(focused.slice(-1), ['first-option']);
});

test('the voice menu focus handling never throws when the elements are missing', async () => {
  const game = setupGame({ speech: { synth: Speech.fakeSynth({ voices: [Speech.ZH_TW_LOCAL] }), Utterance: Speech.FakeUtterance } });
  game.page.voiceMenuButton.value = null;
  game.page.voiceList.value = null;
  game.page.modal.voice = true;
  await settle();
  game.page.modal.voice = false;
  await settle();
  assert.equal(game.page.modal.voice, false);
});

test('closing a menu that was never open does not move the focus', async () => {
  const game = setupGame();
  const focused = [];
  game.page.voiceMenuButton.value = { focus: () => focused.push('menu-button') };
  game.page.modal.voice = false;
  await settle();
  assert.deepEqual(focused, []);
});
