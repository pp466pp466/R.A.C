// 客服 Sidebar 開關規則的單元測試（假的 storage 與假的 window）。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../../app/static/js/cs/cs-sidebar.js');

function memoryStorage(initial) {
  const data = new Map(Object.entries(initial || {}));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
    data
  };
}

const throwingStorage = {
  getItem() { throw new Error('SecurityError'); },
  setItem() { throw new Error('QuotaExceededError'); }
};

test('992px is desktop (open by default) and 991px is not (closed by default)', () => {
  assert.equal(S.DESKTOP_MIN_WIDTH, 992);
  assert.equal(S.initialOpen(992, memoryStorage()), true);
  assert.equal(S.initialOpen(991, memoryStorage()), false);
  assert.equal(S.initialOpen(1280, memoryStorage()), true);
  assert.equal(S.initialOpen(360, memoryStorage()), false);
  assert.equal(S.SIDEBAR_WIDTH_PX, 280);
});

test('on desktop the remembered state is used and closing is remembered across reloads', () => {
  const storage = memoryStorage();
  const first = S.createSidebar({ width: 1280, storage });
  assert.equal(first.state.open, true);
  first.close();
  assert.equal(storage.data.get(S.STORAGE_KEY), '0');
  const reloaded = S.createSidebar({ width: 1280, storage });
  assert.equal(reloaded.state.open, false);
  reloaded.toggle();
  assert.equal(S.createSidebar({ width: 1280, storage }).state.open, true);
});

test('on phones and tablets the state is never remembered and always starts closed', () => {
  const storage = memoryStorage({ [S.STORAGE_KEY]: '1' });         // 即使桌面記的是開啟
  const mobile = S.createSidebar({ width: 360, storage });
  assert.equal(mobile.state.open, false);
  assert.equal(mobile.state.overlay, true);
  mobile.open();
  assert.equal(mobile.state.open, true);
  assert.equal(storage.data.get(S.STORAGE_KEY), '1');               // 沒有被手機的操作改寫
  mobile.close();
  assert.equal(storage.data.get(S.STORAGE_KEY), '1');
  assert.equal(S.createSidebar({ width: 360, storage }).state.open, false);
});

test('a broken localStorage falls back to the default and never throws', () => {
  assert.doesNotThrow(() => S.createSidebar({ width: 1280, storage: throwingStorage }));
  assert.equal(S.createSidebar({ width: 1280, storage: throwingStorage }).state.open, true);
  assert.equal(S.createSidebar({ width: 360, storage: throwingStorage }).state.open, false);
  const sidebar = S.createSidebar({ width: 1280, storage: throwingStorage });
  assert.doesNotThrow(() => sidebar.toggle());
  assert.equal(sidebar.state.open, false);                          // 這次的操作仍然生效，只是記不住
  assert.equal(S.readStored(throwingStorage), null);
  assert.equal(S.writeStored(throwingStorage, true), false);
  assert.equal(S.createSidebar({ width: 1280, storage: null }).state.open, true);
});

test('unknown or corrupt stored values are ignored', () => {
  for (const value of ['', 'true', 'yes', '2', 'null', '[1]']) {
    assert.equal(S.readStored(memoryStorage({ [S.STORAGE_KEY]: value })), null, value);
    assert.equal(S.initialOpen(1280, memoryStorage({ [S.STORAGE_KEY]: value })), true, value);
  }
});

test('safeStorage survives a browser that throws when localStorage is touched', () => {
  const blocked = {};
  Object.defineProperty(blocked, 'localStorage', { get() { throw new Error('SecurityError'); } });
  assert.equal(S.safeStorage(blocked), null);
  assert.equal(S.safeStorage({}), null);
  assert.equal(S.safeStorage(null), null);
  const storage = memoryStorage();
  assert.equal(S.safeStorage({ localStorage: storage }), storage);
});

test('choosing a conversation closes the overlay on phones but keeps the desktop sidebar open', () => {
  const desktop = S.createSidebar({ width: 1280, storage: memoryStorage() });
  assert.equal(desktop.afterSelect(), true);
  const mobile = S.createSidebar({ width: 360, storage: memoryStorage() });
  mobile.open();
  assert.equal(mobile.afterSelect(), false);
  assert.equal(mobile.state.open, false);
  assert.equal(S.openAfterSelect(1280, false), false);              // 桌面使用者關著就維持關著
});

test('the overlay (backdrop click or Esc) closes only in overlay mode', () => {
  const mobile = S.createSidebar({ width: 360, storage: memoryStorage() });
  mobile.open();
  assert.equal(mobile.dismissOverlay(), true);
  assert.equal(mobile.state.open, false);
  assert.equal(mobile.dismissOverlay(), false);
  const desktop = S.createSidebar({ width: 1280, storage: memoryStorage() });
  assert.equal(desktop.dismissOverlay(), false);
  assert.equal(desktop.state.open, true);
});

test('crossing the breakpoint re-applies the default of the new mode', () => {
  const storage = memoryStorage({ [S.STORAGE_KEY]: '0' });          // 桌面使用者曾經關閉
  const sidebar = S.createSidebar({ width: 1280, storage });
  assert.deepEqual({ ...sidebar.state }, { open: false, overlay: false });
  sidebar.setWidth(600);
  assert.deepEqual({ ...sidebar.state }, { open: false, overlay: true });
  sidebar.open();
  sidebar.setWidth(400);                                            // 同一模式內縮放：不動
  assert.equal(sidebar.state.open, true);
  sidebar.setWidth(1400);
  assert.deepEqual({ ...sidebar.state }, { open: false, overlay: false });   // 回到桌面：用記住的狀態
});

test('the state object can be made reactive by the caller', () => {
  const seen = [];
  const sidebar = S.createSidebar({ width: 1280, storage: memoryStorage(), reactive: (value) => { seen.push(value); return value; } });
  assert.equal(seen.length, 1);
  assert.equal(seen[0], sidebar.state);
});

test('watchWidth uses matchMedia and reports the current width; unsubscribing stops it', () => {
  let listener = null;
  let removed = false;
  const win = {
    innerWidth: 800,
    matchMedia: (query) => {
      assert.equal(query, '(min-width: 992px)');
      return {
        addEventListener: (_, fn) => { listener = fn; },
        removeEventListener: () => { removed = true; }
      };
    }
  };
  const widths = [];
  const stop = S.watchWidth(win, (width) => widths.push(width));
  win.innerWidth = 1200;
  listener();
  assert.deepEqual(widths, [1200]);
  stop();
  assert.equal(removed, true);
});

test('watchWidth falls back to resize when matchMedia is missing or throws', () => {
  for (const matchMedia of [undefined, () => { throw new Error('unsupported'); }]) {
    const handlers = [];
    const win = {
      innerWidth: 500,
      matchMedia,
      addEventListener: (type, fn) => handlers.push([type, fn]),
      removeEventListener: (type, fn) => { handlers.splice(handlers.findIndex((h) => h[0] === type && h[1] === fn), 1); }
    };
    const widths = [];
    const stop = S.watchWidth(win, (width) => widths.push(width));
    assert.equal(handlers.length, 1);
    assert.equal(handlers[0][0], 'resize');
    handlers[0][1]();
    assert.deepEqual(widths, [500]);
    stop();
    assert.equal(handlers.length, 0);
  }
});

test('watchWidth supports the legacy addListener API', () => {
  let attached = null;
  const win = { innerWidth: 700, matchMedia: () => ({ addListener: (fn) => { attached = fn; }, removeListener: () => { attached = null; } }) };
  const widths = [];
  const stop = S.watchWidth(win, (width) => widths.push(width));
  attached();
  assert.deepEqual(widths, [700]);
  stop();
  assert.equal(attached, null);
});
