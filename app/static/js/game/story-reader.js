/* 劇情閱讀器（純狀態機，不碰 DOM、不碰瀏覽器儲存；打字機與時鐘由外部注入，Node 可以用假時鐘完整測試；
 * 見 rpg-story-pages 與 rpg-stream-client 規格、rpg-story-view 的 design D2）。
 *
 * 一個閱讀器只管「當前輪」：
 *   - 頁面 = （有玩家行動時）第 0 段「你」的行動頁 + 依 `paginate`（story-pages.js）釋出的劇情段（說話者「旁白」）。
 *     第 0 幕沒有玩家行動，所以只有劇情段。
 *   - 一次只顯示一段（`index`）：翻到沒看過的段才逐字打字，回看或再翻到看過的段一律直接完整顯示；
 *     打字中 `next()` 先補完該段，第二下才翻頁；最後一段 `next()` 不動作；第一段 `prev()` 不動作。
 *   - 玩家以自己的節奏翻頁，系統不會自動翻頁。唯一的例外：送出行動後畫面停在「你」的行動頁，
 *     第一個劇情段釋出時自動換成劇情（規格「玩家行動先顯示為『你』的一段」）。
 *   - 翻到目前最後一個已釋出的段而串流尚未結束時 `waiting` 為真（顯示等待狀態，不是錯誤）。
 *   - `prefers-reduced-motion`（`options.reducedMotion`）時一律直接完整顯示，不呼叫打字機。
 *
 * 用法：
 *   var reader = Game.createStoryReader({ createTypewriter: function (h) { return new UI.TypewriterBuffer(h); },
 *                                         reducedMotion: function () { return mediaQuery.matches; }, onChange: function (state) { ... } });
 *   reader.reset('turn:17', { action: '推開門' });   // 送出行動：立即顯示「你」的行動頁（不打字）
 *   reader.feed(片段, false); ...; reader.feed('', true);  // 串流：`token` 的片段與 `done`
 *   reader.discard('一行轉場文字', { willRetry: true });   // `transition`：丟棄劇情段，只顯示一行說明
 *   reader.load('turn:17', { action, story });              // 載入既有的回合：最後一段、不打字
 *   reader.snapshot() / reader.restore(snapshot);           // 修訂失敗時還原（只在記憶體內，**絕不寫入任何儲存**）
 *
 * `createTypewriter(handlers)` 回傳有 `play(text)`、`flush()`、`cancel()` 的物件（UI.TypewriterBuffer 即是）；`handlers` 是
 * `{ onUpdate(已顯示的文字), onDone(), scheduler }`，`scheduler` 是 `options.scheduler` 原樣轉交（測試的假時鐘）。
 * 沒有提供 `createTypewriter` 時等同減少動態。已被取消或已被補完的打字機之後才到的回呼會被忽略。
 *
 * 狀態（`reader.state` 或 `onChange` 的參數；每次都是新的純物件）：
 *   `key`、`speaker`（`'you'`｜`'narrator'`）、`text`（當前段的完整文字，給輔助科技與歷史用）、`shown`（目前顯示到哪裡）、
 *   `index`、`total`（含行動頁）、`hasAction`、`typing`、`waiting`、`finished`、`canPrev`、`canNext`、
 *   `note`（有轉場或中斷說明時為該行文字，此時 `text` 與 `shown` 就是這一行，其餘導覽一律停用）。
 * 所有文字是純文字，由頁面以 {{ }} 插值顯示，絕不使用 v-html。這裡不記錄、不輸出任何內容（文字是玩家的劇情與行動）。
 */
(function (root, factory) {
  'use strict';
  var pagesApi = (typeof module === 'object' && module.exports) ? require('./story-pages.js') : root.Game;
  var api = factory(pagesApi);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    Object.keys(api).forEach(function (key) { root.Game[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function (pagesApi) {
  'use strict';

  var paginate = pagesApi.paginate;
  var SPEAKER_YOU = 'you';
  var SPEAKER_NARRATOR = 'narrator';

  function createStoryReader(options) {
    options = options || {};

    var key = null;
    var action = '';              // 玩家行動（第 0 段「你」）；第 0 幕為 ''
    var raw = '';                 // 目前收到的劇情全文
    var storyPages = [];          // 已釋出的劇情段（字串）
    var index = -1;               // 目前顯示的頁（含行動頁）；還沒有任何頁時為 -1
    var seen = -1;                // 開始顯示過的最高頁：翻到這以內的頁直接完整顯示
    var shown = '';
    var typing = false;
    var finished = false;
    var note = '';
    var awaitingFirst = true;     // 還沒有顯示過劇情段：第一個釋出的劇情段會自動接手畫面
    var typer = null;
    var generation = 0;           // 每次開始或取消打字就加一，過期的打字機回呼靠它辨識
    var destroyed = false;

    // ---- 頁面 -------------------------------------------------------------------------------------------------------------

    function hasAction() { return action !== ''; }
    function total() { return (hasAction() ? 1 : 0) + storyPages.length; }
    function lastIndex() { return total() - 1; }
    function firstStoryIndex() { return hasAction() ? 1 : 0; }
    function pageText(position) {
      if (position < 0 || position > lastIndex()) { return ''; }
      if (hasAction() && position === 0) { return action; }
      return storyPages[position - firstStoryIndex()];
    }
    function speakerAt(position) { return hasAction() && position === 0 ? SPEAKER_YOU : SPEAKER_NARRATOR; }

    function reduced() {
      if (!options.createTypewriter) { return true; }
      var value = options.reducedMotion;
      try { return typeof value === 'function' ? !!value() : !!value; } catch (error) { return true; }
    }

    // ---- 狀態 -------------------------------------------------------------------------------------------------------------

    function view() {
      var isNote = note !== '';
      var count = total();
      return {
        key: key,
        speaker: isNote ? SPEAKER_NARRATOR : speakerAt(index),
        text: isNote ? note : pageText(index),
        shown: isNote ? note : shown,
        index: isNote ? 0 : Math.max(index, 0),
        total: isNote ? 1 : count,
        hasAction: hasAction(),
        typing: isNote ? false : typing,
        waiting: !finished && index >= count - 1,
        finished: finished,
        canPrev: !isNote && index > 0,
        canNext: !isNote && index < count - 1,
        note: note
      };
    }

    function notify() {
      if (destroyed || typeof options.onChange !== 'function') { return; }
      try { options.onChange(view()); } catch (error) { /* 畫面端的錯誤不能弄壞閱讀器 */ }
    }

    // ---- 打字 -------------------------------------------------------------------------------------------------------------

    function cancelTyping() {
      generation += 1;
      var writer = typer;
      typer = null;
      typing = false;
      if (writer) { try { writer.cancel(); } catch (error) { /* 忽略 */ } }
    }

    function startTyping(text) {
      var mine = generation += 1;
      shown = '';
      typing = true;
      var writer = null;
      try {
        writer = options.createTypewriter({
          onUpdate: function (value) {
            if (mine !== generation || destroyed) { return; }
            shown = value;
            notify();
          },
          onDone: function () {
            if (mine !== generation || destroyed) { return; }
            shown = text;
            typing = false;
            typer = null;
            notify();
          },
          scheduler: options.scheduler
        });
      } catch (error) { writer = null; }
      if (!writer) { typing = false; shown = text; return; }
      typer = writer;
      try { writer.play(text); } catch (error) { if (mine === generation) { typer = null; typing = false; shown = text; } }
    }

    /* 補完目前正在打的那一段（第一下點擊）。 */
    function completeTyping() {
      var text = pageText(index);
      generation += 1;                                     // 之後才到的打字機回呼一律忽略
      var writer = typer;
      typer = null;
      typing = false;
      shown = text;
      if (writer) { try { writer.flush(); } catch (error) { /* 忽略 */ } }
    }

    function showPage(position, animate) {
      cancelTyping();
      index = position;
      if (position > seen) { seen = position; }
      var text = pageText(position);
      if (animate && text && !reduced()) { startTyping(text); } else { shown = text; }
      notify();
    }

    // ---- 動作 -------------------------------------------------------------------------------------------------------------

    /* 開始新的一輪。`opts.action` 是玩家的行動（立即顯示為「你」的行動頁，不打字）；第 0 幕不帶。 */
    function reset(newKey, opts) {
      cancelTyping();
      key = newKey === undefined || newKey === null ? null : String(newKey);
      action = opts && typeof opts.action === 'string' ? opts.action : '';
      raw = '';
      storyPages = [];
      finished = false;
      note = '';
      awaitingFirst = true;
      index = hasAction() ? 0 : -1;
      seen = index;
      shown = hasAction() ? action : '';
      notify();
    }

    /* 串流的片段（`token`）與結束（`done`）。`done` 為真時把剩餘文字釋出為最後的段。已結束或已被丟棄的輪次忽略後續片段。 */
    function feed(chunk, done) {
      if (destroyed || finished) { return; }
      if (typeof chunk === 'string' && chunk) { raw += chunk; }
      var before = storyPages.length;
      var current = pageText(index);
      storyPages = paginate(raw, { done: done === true }).pages;
      if (done === true) { finished = true; }
      if (before === 0 && storyPages.length > 0 && awaitingFirst) {
        awaitingFirst = false;
        note = '';
        showPage(firstStoryIndex(), true);
        return;
      }
      if (note === '' && pageText(index) !== current && index >= 0) { showPage(index, false); return; }   // 已釋出的段不會改變；萬一改變，直接顯示新內容而不是留著舊字
      notify();
    }

    function next() {
      if (destroyed || note !== '') { return 'none'; }
      if (typing) { completeTyping(); notify(); return 'completed'; }
      if (index < lastIndex()) { showPage(index + 1, index + 1 > seen); return 'advanced'; }
      return finished ? 'end' : 'waiting';
    }

    function prev() {
      if (destroyed || note !== '' || index <= 0) { return 'none'; }
      showPage(index - 1, false);
      return 'back';
    }

    /* 直接顯示目前最後一個已釋出的段（不打字）。 */
    function jumpToEnd() {
      if (destroyed || note !== '' || lastIndex() < 0) { return; }
      if (storyPages.length > 0) { awaitingFirst = false; }   // 只有行動頁時，第一個劇情段仍要自動接手畫面
      showPage(lastIndex(), false);
    }

    /* 載入既有的回合（重新整理、換裝置、歷史）：整段已完成，停在最後一段，不打字。 */
    function load(newKey, content) {
      cancelTyping();
      key = newKey === undefined || newKey === null ? null : String(newKey);
      action = content && typeof content.action === 'string' ? content.action : '';
      raw = content && typeof content.story === 'string' ? content.story : '';
      storyPages = paginate(raw, { done: true }).pages;
      finished = true;
      note = '';
      awaitingFirst = false;
      index = lastIndex();
      seen = index;
      shown = pageText(index);
      notify();
    }

    /* 被擋、轉場或中斷：丟棄尚未完成的劇情（不留任何部分內容），劇情卡只顯示一行說明。
     * `willRetry` 為真（自動重生中）時仍在等待新內容，第一個新的劇情段釋出時說明消失；否則這一輪就此結束。 */
    function discard(line, opts) {
      cancelTyping();
      raw = '';
      storyPages = [];
      awaitingFirst = true;
      note = typeof line === 'string' ? line : '';
      finished = !(opts && opts.willRetry === true);
      index = hasAction() ? 0 : -1;
      seen = index;
      shown = hasAction() ? action : '';
      notify();
    }

    /* 修訂失敗時還原用：只含基本型別、只放在記憶體，絕不寫入 localStorage、IndexedDB 或主控台。 */
    function snapshot() {
      return { key: key, action: action, raw: raw, finished: finished, note: note, awaitingFirst: awaitingFirst, index: index, seen: seen };
    }

    function restore(saved) {
      if (destroyed || !saved || typeof saved !== 'object') { return; }
      cancelTyping();
      key = saved.key === undefined ? null : saved.key;
      action = typeof saved.action === 'string' ? saved.action : '';
      raw = typeof saved.raw === 'string' ? saved.raw : '';
      finished = saved.finished === true;
      note = typeof saved.note === 'string' ? saved.note : '';
      awaitingFirst = saved.awaitingFirst === true;
      storyPages = raw ? paginate(raw, { done: finished }).pages : [];
      var wanted = typeof saved.index === 'number' ? saved.index : -1;
      index = Math.max(Math.min(wanted, lastIndex()), hasAction() ? 0 : -1);
      seen = Math.max(index, Math.min(typeof saved.seen === 'number' ? saved.seen : index, lastIndex()));
      shown = pageText(index);                              // 還原的是已經看過的內容：直接完整顯示
      notify();
    }

    /* 離開頁面：取消打字，之後的呼叫與回呼都不再有作用。 */
    function destroy() {
      cancelTyping();
      destroyed = true;
    }

    return {
      reset: reset,
      feed: feed,
      next: next,
      prev: prev,
      jumpToEnd: jumpToEnd,
      load: load,
      discard: discard,
      snapshot: snapshot,
      restore: restore,
      destroy: destroy,
      get state() { return view(); }
    };
  }

  return { createStoryReader: createStoryReader, SPEAKER_YOU: SPEAKER_YOU, SPEAKER_NARRATOR: SPEAKER_NARRATOR };
});
