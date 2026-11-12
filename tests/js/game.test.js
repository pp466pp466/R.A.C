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
  assert.equal(game.page.balanceText.value, '230');
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
