/* Toast 的狀態庫（與框架無關）。
 *
 * 顯示與隱藏完全由這份「響應式狀態」決定：state.items 有東西就顯示，移除就消失。
 * 在瀏覽器中，ui-components.js 會把 Vue.reactive 注入進來；在 Node 測試中則使用一般物件與假計時器。
 *
 * 訊息一律當作純文字（String），顯示時只用 Vue 的 {{ }} 插值，絕不用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UI = root.UI || {};
    root.UI.createToastStore = api.createToastStore;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function createToastStore(options) {
    options = options || {};
    var reactive = options.reactive || function (value) { return value; };
    var timers = options.timers || { setTimeout: setTimeout.bind(globalThis), clearTimeout: clearTimeout.bind(globalThis) };
    var defaultDuration = options.defaultDuration !== undefined ? options.defaultDuration : 4000;
    var maxVisible = options.maxVisible !== undefined ? options.maxVisible : 5;

    var state = reactive({ items: [] });
    var handles = {};
    var nextId = 1;

    function indexOf(id) {
      for (var i = 0; i < state.items.length; i++) { if (state.items[i].id === id) { return i; } }
      return -1;
    }

    function dismiss(id) {
      if (handles[id] !== undefined) { timers.clearTimeout(handles[id]); delete handles[id]; }
      var index = indexOf(id);
      if (index !== -1) { state.items.splice(index, 1); }
    }

    function show(message, settings) {
      settings = settings || {};
      var id = nextId++;
      var duration = settings.duration !== undefined ? settings.duration : defaultDuration;
      state.items.push({ id: id, message: String(message), type: settings.type || 'info', duration: duration });
      while (state.items.length > maxVisible) { dismiss(state.items[0].id); } // 最舊的先移除
      if (duration > 0) { handles[id] = timers.setTimeout(function () { dismiss(id); }, duration); }
      return id;
    }

    function clear() {
      state.items.slice().forEach(function (item) { dismiss(item.id); });
    }

    return { state: state, show: show, dismiss: dismiss, clear: clear };
  }

  return { createToastStore: createToastStore };
});
