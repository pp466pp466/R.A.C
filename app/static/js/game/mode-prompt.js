/* 「要不要修改安全模式」的詢問：固定文案與狀態機，入口頁（目錄卡片的「開始」）與劇本詳情頁（「開始冒險」）共用
 * （見 rpg-entry-pages 規格「點選未綁定劇本時詢問是否修改安全模式」與 rpg-script-publishing 的 design D7）。
 *
 * 純函式與純資料，與頁面無關，Node 可以直接測試：
 * - `copy()`：詢問 Modal 的固定文案（標題、系統狀態碼、兩段說明、三個回答的按鈕文字）；回傳複本，呼叫端改動不影響下一次。文字不來自伺服器、
 *   劇本或模型。`Game.modePromptCopy()`（game-labels.js）保留為薄轉接，指向這裡。
 * - `needsAsk(target, mode)`：要不要先問。只有「安全模式開（生效模式為 safe）、緊急煞車未啟用，而且目標劇本**未綁定**安全模式（`content_tier` 為 adult）」
 *   才問；綁定的劇本、已關閉安全模式、緊急煞車、沒有拿到模式資料、卡片的值不認得時都不問（真正的生效模式永遠由伺服器現算，這裡只決定要不要先問一句）。
 * - `create(Vue, deps)`：狀態機，由頁面的 `setup()` 呼叫，回傳 `{ target, open, copy, begin, goToSettings, keepSafe, cancel }`。
 *   顯示完全由 Vue 狀態接管（`target` 有值就是開啟；`open` 是給 `<ui-modal v-model>` 的可寫計算屬性，Esc 與點遮罩把它設為 false，視同取消）。
 *   `deps`：`mode()`（回傳 `{ effective, forced }` 或 null）、`navigate(path)`、`startPath(id)`、`accountLink(returnPath)`，全由頁面提供，這裡不碰 `window`。
 *   - `begin(target)`：需要問就只開啟 Modal、不導向（回傳 false）；不需要就導向精靈頁（回傳 true）；目標沒有字串的 `id` 時什麼都不做（回傳 false）。
 *     同時只有一個詢問：再次 `begin` 會取代等待中的目標，不會疊加。
 *   - `goToSettings()`：導向帳號設定頁，返回位置是該劇本的精靈頁；沒有等待中的詢問時不導向（回傳 false）。
 *   - `keepSafe()`：安全模式設定不變，以安全模式進入精靈頁；沒有等待中的詢問時不導向。
 *   - `cancel()`：留在原頁，沒有導向。
 * 這只是詢問：不改變任何模式、不送任何請求、不寫任何瀏覽器儲存，也不會自動調高。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.modePrompt = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* 目錄點選未綁定安全模式的劇本、玩家目前是安全模式時的詢問（自製 Modal；Esc 與遮罩視同取消）。 */
  var MODE_PROMPT = {
    code: 'SAFE_MODE',
    title: '要調整安全模式嗎？',
    body: [
      '這個劇本沒有綁定安全模式，會跟隨您的設定。您目前是安全模式開，劇情不會出現性內容。',
      '如果想體驗更深入的內容，可以先到帳號設定關閉安全模式，改完會回到這個劇本；關閉之後是否出現露骨內容取決於模型，不保證。'
    ],
    goSettings: '前往設定',
    keepSafe: '維持安全模式繼續',
    cancel: '取消'
  };

  function copy() {
    return {
      code: MODE_PROMPT.code, title: MODE_PROMPT.title, body: MODE_PROMPT.body.slice(),
      goSettings: MODE_PROMPT.goSettings, keepSafe: MODE_PROMPT.keepSafe, cancel: MODE_PROMPT.cancel
    };
  }

  function needsAsk(target, mode) {
    return !!target && !!mode && mode.effective === 'safe' && mode.forced !== true && target.content_tier === 'adult';
  }

  function hasId(target) { return !!target && typeof target.id === 'string'; }

  function create(Vue, deps) {
    var target = Vue.ref(null);                      // 正在等玩家回答的劇本（有值就是 Modal 開啟）
    var open = Vue.computed({
      get: function () { return target.value !== null; },
      set: function (value) { if (!value) { target.value = null; } }
    });

    function begin(next) {
      if (!hasId(next)) { return false; }
      if (needsAsk(next, deps.mode())) { target.value = next; return false; }
      deps.navigate(deps.startPath(next.id));
      return true;
    }

    function goToSettings() {
      var current = target.value;
      if (!current) { return false; }
      target.value = null;
      deps.navigate(deps.accountLink(deps.startPath(current.id)));
      return true;
    }

    function keepSafe() {
      var current = target.value;
      if (!current) { return false; }
      target.value = null;
      deps.navigate(deps.startPath(current.id));
      return true;
    }

    function cancel() { target.value = null; }

    return { target: target, open: open, copy: copy(), begin: begin, goToSettings: goToSettings, keepSafe: keepSafe, cancel: cancel };
  }

  return { copy: copy, needsAsk: needsAsk, create: create };
});
