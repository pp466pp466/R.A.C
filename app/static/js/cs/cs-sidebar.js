/* 客服 Sidebar 的開關規則（純函式與一個很小的狀態物件，與框架無關，Node 可以直接測試）。
 *
 * 規則（AGENTS.md 第 7 節）：
 *  - 寬度 992px 以上是「桌面」：預設開啟並推擠主區域，開關狀態記在 localStorage；
 *  - 未滿 992px 是「手機與平板」：預設關閉，以覆蓋方式顯示並附遮罩，**不記住**狀態，每次進入都是關閉；
 *  - 選擇對話後，桌面維持開啟，手機與平板自動關閉；
 *  - localStorage 的讀寫（連取得 window.localStorage 本身也可能丟出例外）一律包在 try/catch 內，失敗時退回預設值。
 * 顯示與隱藏由 Vue 的響應式變數（v-show／v-if）接管：Vue 只需要讀 `controller.state.open` 與 `controller.state.overlay`。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.CS = root.CS || {};
    root.CS.sidebar = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DESKTOP_MIN_WIDTH = 992;
  var SIDEBAR_WIDTH_PX = 280;
  var STORAGE_KEY = 'cs.sidebar.open';

  function isDesktop(width) {
    return typeof width === 'number' && width >= DESKTOP_MIN_WIDTH;
  }

  /* 取得 localStorage；被禁止（隱私模式、公司政策）時連讀取屬性都會丟出例外。 */
  function safeStorage(win) {
    try {
      return win && win.localStorage ? win.localStorage : null;
    } catch (error) {
      return null;
    }
  }

  /* 讀取記住的狀態：true、false，或 null（沒有紀錄／無法讀取）。 */
  function readStored(storage) {
    try {
      var value = storage ? storage.getItem(STORAGE_KEY) : null;
      if (value === '1') { return true; }
      if (value === '0') { return false; }
      return null;
    } catch (error) {
      return null;
    }
  }

  function writeStored(storage, open) {
    try {
      if (!storage) { return false; }
      storage.setItem(STORAGE_KEY, open ? '1' : '0');
      return true;
    } catch (error) {
      return false;
    }
  }

  /* 第一次載入（或跨過斷點）時的開關：桌面用記住的值（沒有就開啟），手機與平板一律關閉。 */
  function initialOpen(width, storage) {
    if (!isDesktop(width)) { return false; }
    var stored = readStored(storage);
    return stored === null ? true : stored;
  }

  /* 選擇對話之後的開關：桌面維持目前狀態，手機與平板自動關閉。 */
  function openAfterSelect(width, open) {
    return isDesktop(width) ? open : false;
  }

  /* 狀態物件：`state` 可以換成 Vue.reactive 的物件（傳入 options.reactive）。 */
  function createSidebar(options) {
    options = options || {};
    var reactive = options.reactive || function (value) { return value; };
    var storage = options.storage || null;
    var width = options.width;
    var state = reactive({ open: initialOpen(width, storage), overlay: !isDesktop(width) });

    function setWidth(next) {
      var wasDesktop = isDesktop(width);
      width = next;
      var nowDesktop = isDesktop(width);
      if (wasDesktop !== nowDesktop) {         // 跨過斷點：重新套用該模式的預設，並更新版面模式
        state.overlay = !nowDesktop;
        state.open = initialOpen(width, storage);
      }
    }

    function set(open) {
      state.open = !!open;
      if (isDesktop(width)) { writeStored(storage, state.open); } // 只有桌面記住
      return state.open;
    }

    return {
      state: state,
      setWidth: setWidth,
      toggle: function () { return set(!state.open); },
      open: function () { return set(true); },
      close: function () { return set(false); },
      /* 點遮罩或按 Esc：只在覆蓋模式有意義（桌面的 Sidebar 是版面的一部分，Esc 不應該收起它）。 */
      dismissOverlay: function () {
        if (state.overlay && state.open) { state.open = false; return true; }
        return false;
      },
      afterSelect: function () {
        state.open = openAfterSelect(width, state.open);
        return state.open;
      }
    };
  }

  /* 監看斷點：回傳取消監看的函式。沒有 matchMedia（或它丟出例外）時退回監看 resize。 */
  function watchWidth(win, callback) {
    function current() { return typeof win.innerWidth === 'number' ? win.innerWidth : 0; }
    try {
      if (typeof win.matchMedia === 'function') {
        var query = win.matchMedia('(min-width: ' + DESKTOP_MIN_WIDTH + 'px)');
        var listener = function () { callback(current()); };
        if (typeof query.addEventListener === 'function') {
          query.addEventListener('change', listener);
          return function () { query.removeEventListener('change', listener); };
        }
        if (typeof query.addListener === 'function') {
          query.addListener(listener);
          return function () { query.removeListener(listener); };
        }
      }
    } catch (error) { /* 退回 resize */ }
    var onResize = function () { callback(current()); };
    win.addEventListener('resize', onResize);
    return function () { win.removeEventListener('resize', onResize); };
  }

  return {
    DESKTOP_MIN_WIDTH: DESKTOP_MIN_WIDTH,
    SIDEBAR_WIDTH_PX: SIDEBAR_WIDTH_PX,
    STORAGE_KEY: STORAGE_KEY,
    isDesktop: isDesktop,
    safeStorage: safeStorage,
    readStored: readStored,
    writeStored: writeStored,
    initialOpen: initialOpen,
    openAfterSelect: openAfterSelect,
    createSidebar: createSidebar,
    watchWidth: watchWidth
  };
});
