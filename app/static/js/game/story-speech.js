/* 劇情朗讀（純狀態機，不碰 DOM、不碰瀏覽器儲存的全域物件；語音合成、發音物件與儲存都由外部注入，Node 可以用替身完整測試；
 * 見 rpg-story-tts 規格與 design D1、D2、D3）。
 *
 * 用法：
 *   var speech = Game.createStorySpeech({ synth: window.speechSynthesis, Utterance: window.SpeechSynthesisUtterance,
 *                                         storage: { getItem, setItem }, lang: 'zh-TW', onChange: function (state) { ... } });
 *   speech.toggle(text)      // 'started'｜'stopped'｜'needs-choice'｜'unavailable'
 *   speech.speak(text) / speech.stop() / speech.preview(key) / speech.choose({ mode: 'auto' } 或 { mode: 'voice', key }) / speech.destroy()
 *
 * 狀態（`speech.state` 或 `onChange` 的參數；每次都是新的純物件）：
 *   `availability`：`'unsupported'`（沒有語音合成或發音物件）｜`'no-voice'`（語音清單已載入但沒有任何中文語音）｜`'ready'`
 *     （清單為空視為「還不知道」，也是 `'ready'`：交給瀏覽器依語言挑選）；
 *   `speaking`：是否正在朗讀（由這個模組自己維護，不採用瀏覽器的 `speaking`，因為 `cancel()` 之後各瀏覽器送出的事件並不一致）；
 *   `voices`：只含中文語音的清單，每項 `{ key, name, lang, local }`，排序為「語言層級（zh-TW、zh-Hant*、其他 zh*）、本機優先、名稱」；
 *     `key` 是 `voiceURI`（空的時候用名稱）；`local` 在瀏覽器沒有提供 `localService` 時視為真（只在確定為假時才是線上語音）；
 *   `choice`：`'none'`（從未選過）｜`'auto'`（玩家選了自動）｜`'voice'`（玩家選了某個語音）；
 *   `selectedKey`：`choice` 為 `'voice'` 且那個語音仍在清單中時的 `key`，否則為空字串（給語音選單標示目前的選項）。
 *
 * 規則：
 *   - 每次 `speak` 產生新的遞增代號，只有目前代號的 `end`、`error` 事件能改變狀態；`stop()` 先作廢代號、同步把 `speaking` 設為假，再 `cancel()`；
 *     `speak` 一律先 `cancel()`。
 *   - 玩家的選擇存在單一鍵 `rpg.speech.voice`，值只有 `{"mode":"auto"}` 或 `{"mode":"voice","voiceURI":"…","name":"…"}`，不存任何朗讀的文字。
 *     讀到壞掉的值視為 `none`；寫入失敗時仍以記憶體中的選擇為準。比對先 `voiceURI`、再名稱，都找不到就自動挑選，**不改寫**已儲存的選擇。
 *   - 自動挑選 = 排序後的第一個語音（`zh-TW` → `zh-Hant*` → 其他 `zh*`，同層級本機優先）；`utterance.lang` 一律是設定的語言，
 *     有挑到語音才設 `utterance.voice`。
 *   - 長文字：超過 `CHUNK_MAX`（與劇情段的字數上限相同）時，以劇情分段（story-pages.js）依句界切成數段，依序排進瀏覽器的朗讀佇列，
 *     全部由同一個代號管理（避免部分瀏覽器對長發音在約 15 秒後中斷）。劇情段本身不超過上限，所以一律是單一發音。
 *   - `choose` 與 `stop` 一樣會停止目前的朗讀（試聽或劇情），選擇立即生效。`preview` 不改變選擇、不寫入儲存。
 *
 * 這裡不記錄、不輸出任何內容（文字是玩家的劇情與行動），也不使用主控台。
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
  var STORAGE_KEY = 'rpg.speech.voice';
  var PREVIEW_TEXT = '你好，這是語音試聽。';
  var DEFAULT_LANG = 'zh-TW';
  var CHUNK_MAX = pagesApi.PAGE_MAX_CHARS;

  var AVAILABILITY_UNSUPPORTED = 'unsupported';
  var AVAILABILITY_NO_VOICE = 'no-voice';
  var AVAILABILITY_READY = 'ready';

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  /* 語言層級：0＝zh-TW（含 zh_TW、不分大小寫）、1＝zh-Hant*、2＝其他 zh*；不是中文回 -1。 */
  function tierOf(lang) {
    var normal = String(lang || '').replace(/_/g, '-').toLowerCase();
    if (normal === 'zh-tw') { return 0; }
    if (normal === 'zh-hant' || normal.indexOf('zh-hant-') === 0) { return 1; }
    if (normal === 'zh' || normal.indexOf('zh-') === 0) { return 2; }
    return -1;
  }

  function compareVoices(left, right) {
    if (left.tier !== right.tier) { return left.tier - right.tier; }
    if (left.local !== right.local) { return left.local ? -1 : 1; }
    if (left.name !== right.name) { return left.name < right.name ? -1 : 1; }
    if (left.key !== right.key) { return left.key < right.key ? -1 : 1; }
    return 0;
  }

  /* 把文字切成要朗讀的數段：不超過上限就是原文一段；超過時依劇情分段的句界規則切（不丟字）。 */
  function chunksOf(text) {
    if (text.length <= CHUNK_MAX) { return [text]; }
    var pages = paginate(text, { done: true }).pages;
    return pages.length ? pages : [text];
  }

  function createStorySpeech(options) {
    options = options || {};
    var synth = options.synth || null;
    var Utterance = typeof options.Utterance === 'function' ? options.Utterance : null;
    var storage = options.storage || null;
    var lang = typeof options.lang === 'string' && options.lang !== '' ? options.lang : DEFAULT_LANG;
    var onChange = typeof options.onChange === 'function' ? options.onChange : null;

    var supported = !!(synth && typeof synth.speak === 'function' && Utterance);
    var destroyed = false;
    var token = 0;                  // 每次朗讀、停止與銷毀都加一，過期的事件靠它辨識
    var speaking = false;
    var availability = supported ? AVAILABILITY_READY : AVAILABILITY_UNSUPPORTED;
    var voices = [];                // { key, name, lang, local, tier, uri, ref }（ref 是瀏覽器的語音物件，不對外）
    var choice = readChoice();      // { mode: 'none' } | { mode: 'auto' } | { mode: 'voice', uri, name }
    var lastSignature = '';
    var listening = false;

    // ---- 儲存 -------------------------------------------------------------------------------------------------------------

    function readChoice() {
      try {
        if (!storage || typeof storage.getItem !== 'function') { return { mode: 'none' }; }
        var raw = storage.getItem(STORAGE_KEY);
        if (typeof raw !== 'string' || raw === '') { return { mode: 'none' }; }
        var data = JSON.parse(raw);
        if (!isObject(data)) { return { mode: 'none' }; }
        if (data.mode === 'auto') { return { mode: 'auto' }; }
        if (data.mode === 'voice' && typeof data.voiceURI === 'string' && typeof data.name === 'string' && (data.voiceURI !== '' || data.name !== '')) {
          return { mode: 'voice', uri: data.voiceURI, name: data.name };
        }
      } catch (error) { /* 儲存不可用或內容壞掉：視為從未選過 */ }
      return { mode: 'none' };
    }

    function writeChoice(value) {
      try {
        if (storage && typeof storage.setItem === 'function') { storage.setItem(STORAGE_KEY, JSON.stringify(value)); }
      } catch (error) { /* 寫入失敗：本次載入內仍以記憶體中的選擇為準 */ }
    }

    // ---- 語音清單 -------------------------------------------------------------------------------------------------------------

    function readVoices() {
      var raw = [];
      try {
        var listed = synth.getVoices();
        raw = listed && typeof listed.length === 'number' ? Array.prototype.slice.call(listed) : [];
      } catch (error) { raw = []; }
      var chinese = [];
      var seen = {};
      raw.forEach(function (voice) {
        if (!isObject(voice) || typeof voice.lang !== 'string') { return; }
        var tier = tierOf(voice.lang);
        if (tier < 0) { return; }
        var name = typeof voice.name === 'string' ? voice.name : '';
        var uri = typeof voice.voiceURI === 'string' ? voice.voiceURI : '';
        var key = uri || name;
        if (key === '' || seen[key] === true) { return; }
        seen[key] = true;
        chinese.push({ key: key, name: name, lang: voice.lang, local: voice.localService !== false, tier: tier, uri: uri, ref: voice });
      });
      chinese.sort(compareVoices);
      return { total: raw.length, chinese: chinese };
    }

    function refresh() {
      if (!supported) { return; }
      var found = readVoices();
      voices = found.chinese;
      availability = found.total > 0 && voices.length === 0 ? AVAILABILITY_NO_VOICE : AVAILABILITY_READY;
    }

    /* 玩家選過的語音在目前清單中的位置：先 `voiceURI` 完全相同，再名稱相同（清單只含中文語音，所以等於「名稱加語言」）。 */
    function findChosen() {
      if (choice.mode !== 'voice') { return null; }
      var index;
      for (index = 0; index < voices.length; index += 1) {
        if (choice.uri !== '' && voices[index].uri === choice.uri) { return voices[index]; }
      }
      for (index = 0; index < voices.length; index += 1) {
        if (choice.name !== '' && voices[index].name === choice.name) { return voices[index]; }
      }
      return null;
    }

    function findByKey(key) {
      for (var index = 0; index < voices.length; index += 1) {
        if (voices[index].key === key) { return voices[index]; }
      }
      return null;
    }

    /* 這次朗讀要用的語音：玩家的選擇仍在清單中就用它，否則自動挑選（排序後的第一個）；沒有就不指定，交給瀏覽器依語言挑。 */
    function pickVoice() { return findChosen() || voices[0] || null; }

    // ---- 狀態 -------------------------------------------------------------------------------------------------------------

    function view() {
      var chosen = findChosen();
      return {
        availability: availability,
        speaking: speaking,
        voices: voices.map(function (voice) { return { key: voice.key, name: voice.name, lang: voice.lang, local: voice.local }; }),
        choice: choice.mode,
        selectedKey: chosen ? chosen.key : ''
      };
    }

    function signature() { return JSON.stringify(view()); }

    /* 只在狀態真的改變時通知（`voiceschanged` 可能重複觸發）。 */
    function notify() {
      if (destroyed) { return; }
      var current = signature();
      if (current === lastSignature) { return; }
      lastSignature = current;
      if (onChange) {
        try { onChange(view()); } catch (error) { /* 畫面端的錯誤不得破壞朗讀狀態 */ }
      }
    }

    function cancelSpeech() {
      try { if (synth && typeof synth.cancel === 'function') { synth.cancel(); } } catch (error) { /* 忽略 */ }
    }

    // ---- 朗讀 -------------------------------------------------------------------------------------------------------------

    function startSpeaking(text, voice) {
      token += 1;
      var mine = token;
      cancelSpeech();
      var chunks = chunksOf(text);
      var remaining = chunks.length;

      function fail() {                          // 任何一段出錯：作廢整個朗讀、丟掉佇列裡還沒讀的，回到可播放
        if (destroyed || mine !== token) { return; }
        token += 1;
        cancelSpeech();
        speaking = false;
        notify();
      }

      speaking = true;
      try {
        chunks.forEach(function (chunk) {
          var utterance = new Utterance(chunk);
          utterance.lang = lang;
          if (voice) { utterance.voice = voice.ref; }
          var finished = false;
          utterance.onend = function () {
            if (destroyed || mine !== token || finished) { return; }
            finished = true;
            remaining -= 1;
            if (remaining <= 0) { speaking = false; notify(); }
          };
          utterance.onerror = function () { fail(); };
          synth.speak(utterance);
        });
      } catch (error) {
        token += 1;
        cancelSpeech();
        speaking = false;
        notify();
        return false;
      }
      notify();
      return true;
    }

    function validText(text) { return typeof text === 'string' && text.replace(/\s/g, '') !== ''; }

    function speak(text) {
      if (destroyed || !supported) { return false; }
      refresh();
      if (availability !== AVAILABILITY_READY || !validText(text)) { notify(); return false; }
      return startSpeaking(text, pickVoice());
    }

    function stop() {
      if (destroyed || !supported) { return; }
      token += 1;
      speaking = false;
      cancelSpeech();
      notify();
    }

    function toggle(text) {
      if (destroyed || !supported) { return 'unavailable'; }
      if (speaking) { stop(); return 'stopped'; }
      refresh();
      if (availability !== AVAILABILITY_READY || !validText(text)) { notify(); return 'unavailable'; }
      if (choice.mode === 'none' && voices.length > 0) { notify(); return 'needs-choice'; }
      return startSpeaking(text, pickVoice()) ? 'started' : 'unavailable';
    }

    function preview(key) {
      if (destroyed || !supported) { return false; }
      refresh();
      var voice = findByKey(key);                 // 沒有中文語音（no-voice）時清單是空的，自然找不到
      if (!voice) { notify(); return false; }
      return startSpeaking(PREVIEW_TEXT, voice);
    }

    function choose(selection) {
      if (destroyed || !supported || !isObject(selection)) { return false; }
      refresh();
      var next = null;
      var stored = null;
      if (selection.mode === 'auto') {
        next = { mode: 'auto' };
        stored = { mode: 'auto' };
      } else if (selection.mode === 'voice') {
        var voice = findByKey(selection.key);
        if (!voice) { return false; }
        next = { mode: 'voice', uri: voice.uri, name: voice.name };
        stored = { mode: 'voice', voiceURI: voice.uri, name: voice.name };
      } else {
        return false;
      }
      token += 1;                                // 選擇立即生效，進行中的試聽或朗讀一併停止
      speaking = false;
      cancelSpeech();
      choice = next;
      writeChoice(stored);
      notify();
      return true;
    }

    // ---- 語音清單稍後才載入 --------------------------------------------------------------------------------------------------

    function onVoicesChanged() {
      if (destroyed) { return; }
      refresh();
      notify();
    }

    function destroy() {
      if (destroyed) { return; }
      var wasSupported = supported;
      destroyed = true;
      token += 1;
      speaking = false;
      if (wasSupported) { cancelSpeech(); }
      if (listening) {
        try {
          if (typeof synth.removeEventListener === 'function') { synth.removeEventListener('voiceschanged', onVoicesChanged); }
          else if (synth.onvoiceschanged === onVoicesChanged) { synth.onvoiceschanged = null; }
        } catch (error) { /* 忽略 */ }
        listening = false;
      }
    }

    if (supported) {
      refresh();
      try {
        if (typeof synth.addEventListener === 'function') { synth.addEventListener('voiceschanged', onVoicesChanged); listening = true; }
        else if ('onvoiceschanged' in synth) { synth.onvoiceschanged = onVoicesChanged; listening = true; }
      } catch (error) { listening = false; }
    }
    lastSignature = signature();

    return {
      speak: speak,
      stop: stop,
      toggle: toggle,
      preview: preview,
      choose: choose,
      destroy: destroy,
      get state() { return view(); }
    };
  }

  return {
    createStorySpeech: createStorySpeech,
    SPEECH_STORAGE_KEY: STORAGE_KEY,
    SPEECH_PREVIEW_TEXT: PREVIEW_TEXT,
    SPEECH_CHUNK_MAX: CHUNK_MAX
  };
});
