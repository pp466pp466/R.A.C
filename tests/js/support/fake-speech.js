'use strict';
/* 劇情朗讀（rpg-story-tts）的測試替身：假的 speechSynthesis、發音物件、語音與儲存（不是測試檔；story-speech.test.js 有自己的同款替身，
 * 這份供「接進遊戲畫面」的測試使用：game-story-session.test.js、game-story-revision.test.js、game.test.js 與 game-sandbox.js）。 */
const path = require('node:path');

const { SPEECH_STORAGE_KEY } = require(path.join(__dirname, '..', '..', '..', 'app', 'static', 'js', 'game', 'story-speech.js'));

class FakeUtterance {
  constructor(text) { this.text = text; this.lang = ''; this.voice = null; this.onend = null; this.onerror = null; }
}

/** 假的 speechSynthesis：記錄呼叫順序，語音清單可稍後才載入。 */
function fakeSynth(options = {}) {
  const listeners = new Set();
  const synth = {
    calls: [],
    queue: [],
    voices: options.voices || [],
    getVoices() { return this.voices; },
    speak(utterance) { this.calls.push('speak:' + utterance.text); this.queue.push(utterance); },
    cancel() { this.calls.push('cancel'); this.queue = []; },
    addEventListener(type, handler) { if (type === 'voiceschanged') { listeners.add(handler); } },
    removeEventListener(type, handler) { if (type === 'voiceschanged') { listeners.delete(handler); } },
    setVoices(voices) { this.voices = voices; Array.from(listeners).forEach((handler) => handler({ type: 'voiceschanged' })); },
    get listenerCount() { return listeners.size; },
    get speaks() { return this.calls.filter((call) => call.startsWith('speak:')); },
    get cancels() { return this.calls.filter((call) => call === 'cancel').length; },
    /** 目前有沒有排著的發音（cancel 之後為空）。 */
    get speaking() { return this.queue.length > 0; },
  };
  return synth;
}

function voice(name, lang, local = true, uri = name) {
  return { name, lang, voiceURI: uri, localService: local, default: false };
}

/** 假的儲存：記錄所有寫入；可設定讀或寫丟例外。 */
function fakeStorage(initial = null, options = {}) {
  const store = { value: initial, writes: [], reads: 0 };
  return {
    store,
    getItem(key) {
      store.reads += 1;
      if (options.throwOnRead) { throw new Error('read blocked'); }
      return key === SPEECH_STORAGE_KEY ? store.value : null;
    },
    setItem(key, value) {
      if (options.throwOnWrite) { throw new Error('write blocked'); }
      store.writes.push([key, value]);
      if (key === SPEECH_STORAGE_KEY) { store.value = value; }
    },
  };
}

const ZH_TW_LOCAL = voice('Microsoft Hanhan', 'zh-TW', true);
const ZH_TW_ONLINE = voice('Google 國語（臺灣）', 'zh-TW', false);
const ZH_CN_LOCAL = voice('Microsoft Huihui', 'zh-CN', true);

/** 玩家已選過「自動」的儲存值：讓第一次按就直接朗讀（不先開選單）。 */
const AUTO = JSON.stringify({ mode: 'auto' });

/** 給 createHarness 的 `speech` 選項：一組已經有中文語音的朗讀環境；`stored` 是儲存裡已有的選擇。 */
function speechEnvironment({ voices = [ZH_TW_LOCAL, ZH_TW_ONLINE, ZH_CN_LOCAL], stored = AUTO } = {}) {
  return { synth: fakeSynth({ voices }), Utterance: FakeUtterance, storage: fakeStorage(stored) };
}

module.exports = { FakeUtterance, fakeSynth, fakeStorage, voice, speechEnvironment, ZH_TW_LOCAL, ZH_TW_ONLINE, ZH_CN_LOCAL, AUTO, SPEECH_STORAGE_KEY };
