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
