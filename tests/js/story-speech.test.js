'use strict';
/* rpg-story-tts 規格的朗讀狀態機（story-speech.js）：
 * 播放與停止切換、自然結束與錯誤回到可播放、過期事件被忽略、連續兩次朗讀只有最後一次有效、不支援與丟例外時回到可播放、
 * 語音清單（只含中文、語言層級與本機優先、缺 localService、稍後才載入、只在內容改變時通知）、玩家的選擇（三態、儲存壞掉與不可用、
 * 比對 voiceURI 與名稱、選的語音不存在不改寫儲存）、試聽、長文字切段、destroy、不留痕跡。
 * 以假的 speechSynthesis、發音物件與儲存跑，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS_DIR = path.join(__dirname, '..', '..', 'app', 'static', 'js');
const SPEECH_FILE = path.join(JS_DIR, 'game', 'story-speech.js');
const { createStorySpeech, SPEECH_STORAGE_KEY, SPEECH_PREVIEW_TEXT, SPEECH_CHUNK_MAX } = require(SPEECH_FILE);

// ---- 替身 ---------------------------------------------------------------------------------------------------------------------

class FakeUtterance {
  constructor(text) { this.text = text; this.lang = ''; this.voice = null; this.onend = null; this.onerror = null; }
}

/** 假的 speechSynthesis：記錄每一次呼叫的順序，語音清單可以稍後才載入，事件由測試手動觸發。 */
function fakeSynth(options = {}) {
  const listeners = new Set();
  const synth = {
    calls: [],                 // 'cancel'、'speak:<文字>'，依發生順序
    queue: [],                 // 目前排隊中的發音物件（cancel 會清空）
    voices: options.voices || [],
    throwOnSpeak: options.throwOnSpeak === true,
    getVoices() { return this.voices; },
    speak(utterance) {
      if (this.throwOnSpeak) { throw new Error('speak failed'); }
      this.calls.push('speak:' + utterance.text);
      this.queue.push(utterance);
    },
    cancel() { this.calls.push('cancel'); this.queue = []; },
    addEventListener(type, handler) { if (type === 'voiceschanged') { listeners.add(handler); } },
    removeEventListener(type, handler) { if (type === 'voiceschanged') { listeners.delete(handler); } },
    setVoices(voices) { this.voices = voices; Array.from(listeners).forEach((handler) => handler({ type: 'voiceschanged' })); },
    get listenerCount() { return listeners.size; },
    get speaks() { return this.calls.filter((call) => call.startsWith('speak:')); },
    get cancels() { return this.calls.filter((call) => call === 'cancel').length; }
  };
  return synth;
}

function voice(name, lang, local = true, uri = name) {
  return { name, lang, voiceURI: uri, localService: local, default: false };
}

/** 假的儲存：可設定讀或寫丟例外，記錄所有寫入。 */
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
    }
  };
}

function setup(options = {}) {
  const synth = 'synth' in options ? options.synth : fakeSynth({ voices: options.voices });
  const storage = options.storage === undefined ? fakeStorage(options.stored || null) : options.storage;
  const changes = [];
  const speech = createStorySpeech({
    synth,
    Utterance: options.Utterance === undefined ? FakeUtterance : options.Utterance,
    storage,
    lang: options.lang,
    onChange: (state) => changes.push(state)
  });
  return { speech, synth, storage, changes };
}

const ZH_TW_LOCAL = voice('Microsoft Hanhan', 'zh-TW', true);
const ZH_TW_ONLINE = voice('Google 國語（臺灣）', 'zh-TW', false);
const ZH_CN_LOCAL = voice('Microsoft Huihui', 'zh-CN', true);
const EN_US = voice('Microsoft David', 'en-US', true);

/** 玩家已選過「自動」的儲存值：讓 toggle 直接朗讀。 */
const AUTO = JSON.stringify({ mode: 'auto' });

// ---- 播放與停止 ---------------------------------------------------------------------------------------------------------------

test('speak starts speaking: cancel first, then one utterance with the text and the zh-TW language, and the change is announced', () => {
  const { speech, synth, changes } = setup({ voices: [ZH_TW_LOCAL] });
  assert.equal(speech.speak('他推開了門。'), true);
  assert.deepEqual(synth.calls, ['cancel', 'speak:他推開了門。']);
  assert.equal(synth.queue[0].lang, 'zh-TW');
  assert.equal(speech.state.speaking, true);
  assert.equal(changes.length, 1);
  assert.equal(changes[0].speaking, true);
});

test('the language option is used for the utterance, and defaults to zh-TW', () => {
  const custom = setup({ voices: [ZH_TW_LOCAL], lang: 'zh-Hant-TW' });
  custom.speech.speak('你好。');
  assert.equal(custom.synth.queue[0].lang, 'zh-Hant-TW');
});

test('a natural end returns to playable, and the same text can be played again', () => {
  const { speech, synth, changes } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('第一段。');
  synth.queue[0].onend({});
  assert.equal(speech.state.speaking, false);
  assert.equal(changes[changes.length - 1].speaking, false);
  assert.equal(speech.speak('第一段。'), true);
  assert.equal(speech.state.speaking, true);
});

test('an error returns to playable without throwing, and the rest of the queue is dropped', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('第一段。');
  const cancelsBefore = synth.cancels;
  assert.doesNotThrow(() => synth.queue[0].onerror({ error: 'synthesis-failed' }));
  assert.equal(speech.state.speaking, false);
  assert.equal(synth.cancels, cancelsBefore + 1);
  assert.equal(speech.speak('再聽一次。'), true);
});

test('stop sets speaking to false synchronously and cancels; the late end and error events of the old speech are ignored', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('第一段。');
  const old = synth.queue[0];
  speech.stop();
  assert.equal(speech.state.speaking, false);
  assert.equal(synth.calls[synth.calls.length - 1], 'cancel');
  speech.speak('第二段。');
  old.onend({});
  old.onerror({ error: 'interrupted' });
  assert.equal(speech.state.speaking, true, 'the old events must not turn the new speech off');
  synth.queue[0].onend({});
  assert.equal(speech.state.speaking, false);
});

test('after stop, a late error of the old speech does not touch the synthesizer again', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('第一段。');
  const old = synth.queue[0];
  speech.stop();
  const callsAfterStop = synth.calls.slice();
  old.onerror({ error: 'interrupted' });
  old.onend({});
  assert.deepEqual(synth.calls, callsAfterStop);
  assert.equal(speech.state.speaking, false);
});

test('two consecutive speaks: only the last one counts', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('甲。');
  const first = synth.queue[0];
  speech.speak('乙。');
  const second = synth.queue[0];
  assert.notEqual(first, second);
  first.onend({});
  assert.equal(speech.state.speaking, true);
  second.onend({});
  assert.equal(speech.state.speaking, false);
});

test('toggle plays, then stops', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL], stored: AUTO });
  assert.equal(speech.toggle('一段文字。'), 'started');
  assert.equal(speech.state.speaking, true);
  assert.equal(speech.toggle('一段文字。'), 'stopped');
  assert.equal(speech.state.speaking, false);
  assert.equal(synth.speaks.length, 1);
});

test('empty, blank and non-string text is never spoken and never cancels', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL], stored: AUTO });
  for (const text of ['', '   \n\t', null, undefined, 12, {}]) {
    assert.equal(speech.speak(text), false);
    assert.equal(speech.toggle(text), 'unavailable');
  }
  assert.deepEqual(synth.calls, []);
});

// ---- 不支援與例外 ---------------------------------------------------------------------------------------------------------------

test('without speechSynthesis or the utterance constructor the module is unsupported and every call is a harmless no-op', () => {
  const cases = [
    setup({ synth: null }),
    setup({ voices: [ZH_TW_LOCAL], Utterance: null }),
    setup({ synth: { getVoices: () => [] } }),
    setup({ synth: undefined, Utterance: undefined })
  ];
  for (const { speech } of cases) {
    assert.equal(speech.state.availability, 'unsupported');
    assert.equal(speech.speak('你好。'), false);
    assert.equal(speech.toggle('你好。'), 'unavailable');
    assert.equal(speech.preview('x'), false);
    assert.equal(speech.choose({ mode: 'auto' }), false);
    assert.doesNotThrow(() => { speech.stop(); speech.destroy(); });
    assert.equal(speech.state.speaking, false);
  }
});

test('when synth.speak throws, the speech returns to playable and nothing escapes', () => {
  const synth = fakeSynth({ voices: [ZH_TW_LOCAL], throwOnSpeak: true });
  const { speech } = setup({ synth, stored: AUTO });
  assert.equal(speech.toggle('你好。'), 'unavailable');
  assert.equal(speech.state.speaking, false);
  assert.equal(synth.cancels >= 2, true, 'cancel is called before speaking and again after the failure');
  synth.throwOnSpeak = false;
  assert.equal(speech.toggle('你好。'), 'started');
});

test('a throwing onChange callback does not break the state machine', () => {
  const synth = fakeSynth({ voices: [ZH_TW_LOCAL] });
  const speech = createStorySpeech({ synth, Utterance: FakeUtterance, storage: fakeStorage(), onChange: () => { throw new Error('view broke'); } });
  assert.equal(speech.speak('你好。'), true);
  assert.equal(speech.state.speaking, true);
  synth.queue[0].onend({});
  assert.equal(speech.state.speaking, false);
});

// ---- 語音清單 -----------------------------------------------------------------------------------------------------------------

test('voices holds only Chinese voices, sorted by language tier, then local first, then name', () => {
  const { speech } = setup({
    voices: [
      EN_US,
      voice('B 簡體線上', 'zh-CN', false),
      voice('A 簡體本機', 'zh-CN', true),
      voice('C 臺灣線上', 'zh-TW', false),
      voice('Z 臺灣本機', 'zh-TW', true),
      voice('Y 臺灣本機', 'zh-TW', true),
      voice('H 繁體', 'zh-Hant', true),
      voice('Cantonese', 'yue-HK', true),
      voice('Japanese', 'ja-JP', true)
    ]
  });
  assert.deepEqual(speech.state.voices.map((item) => item.name), ['Y 臺灣本機', 'Z 臺灣本機', 'C 臺灣線上', 'H 繁體', 'A 簡體本機', 'B 簡體線上']);
  assert.deepEqual(Object.keys(speech.state.voices[0]).sort(), ['key', 'lang', 'local', 'name']);
});

test('language tiers: zh-TW (any case, underscore) < zh-Hant* < other zh*; lookalikes are not Chinese', () => {
  const { speech } = setup({
    voices: [
      voice('five', 'zh', true),
      voice('four', 'zh-HK', true),
      voice('three', 'zh-Hant-TW', true),
      voice('two', 'ZH-tw', true),
      voice('one', 'zh_TW', true),
      voice('lookalike', 'zhx-TW', true),
      voice('prefix', 'xzh-TW', true)
    ]
  });
  // 第 0 層（zh_TW、ZH-tw，同層級依名稱）→ 第 1 層（zh-Hant-TW）→ 第 2 層（zh、zh-HK，同層級依名稱）；zhx-TW 與 xzh-TW 不是中文
  assert.deepEqual(speech.state.voices.map((item) => item.name), ['one', 'two', 'three', 'five', 'four']);
});

test('auto-pick uses zh-TW when both zh-TW and zh-CN exist, and the utterance carries that voice object', () => {
  const { speech, synth } = setup({ voices: [ZH_CN_LOCAL, ZH_TW_LOCAL, EN_US] });
  speech.speak('你好。');
  assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
  assert.equal(synth.queue[0].lang, 'zh-TW');
});

test('auto-pick: a zh-TW online voice still beats a zh-CN local voice (language tier first)', () => {
  const { speech, synth } = setup({ voices: [ZH_CN_LOCAL, ZH_TW_ONLINE] });
  speech.speak('你好。');
  assert.equal(synth.queue[0].voice, ZH_TW_ONLINE);
});

test('auto-pick: within the same tier the local voice wins, whatever the input order', () => {
  for (const voices of [[ZH_TW_ONLINE, ZH_TW_LOCAL], [ZH_TW_LOCAL, ZH_TW_ONLINE]]) {
    const { speech, synth } = setup({ voices });
    speech.speak('你好。');
    assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
  }
});

test('only zh-CN voices: it is used, and the utterance language stays zh-TW', () => {
  const { speech, synth } = setup({ voices: [ZH_CN_LOCAL, EN_US] });
  assert.equal(speech.state.availability, 'ready');
  speech.speak('你好。');
  assert.equal(synth.queue[0].voice, ZH_CN_LOCAL);
  assert.equal(synth.queue[0].lang, 'zh-TW');
});

test('no Chinese voice at all: no-voice, and nothing is spoken or cancelled', () => {
  const { speech, synth } = setup({ voices: [EN_US, voice('Japanese', 'ja-JP')] });
  assert.equal(speech.state.availability, 'no-voice');
  assert.deepEqual(speech.state.voices, []);
  assert.equal(speech.speak('你好。'), false);
  assert.equal(speech.toggle('你好。'), 'unavailable');
  assert.equal(speech.preview('Microsoft David'), false);
  assert.deepEqual(synth.calls, []);
});

test('an empty list means "not known yet": ready, and the text is spoken with no voice set', () => {
  const { speech, synth } = setup({ voices: [] });
  assert.equal(speech.state.availability, 'ready');
  assert.deepEqual(speech.state.voices, []);
  assert.equal(speech.toggle('你好。'), 'started');
  assert.equal(synth.queue[0].voice, null);
  assert.equal(synth.queue[0].lang, 'zh-TW');
});

test('a voice list that loads later updates the state through voiceschanged and later speech uses the new voice', () => {
  const { speech, synth, changes } = setup({ voices: [] });
  assert.equal(changes.length, 0);
  synth.setVoices([ZH_TW_LOCAL, ZH_CN_LOCAL]);
  assert.equal(changes.length, 1);
  assert.equal(speech.state.voices.length, 2);
  speech.speak('你好。');
  assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
  synth.setVoices([EN_US]);
  assert.equal(speech.state.availability, 'no-voice');
});

test('voiceschanged notifies only when the content really changed', () => {
  const { synth, changes } = setup({ voices: [ZH_TW_LOCAL] });
  synth.setVoices([ZH_TW_LOCAL]);
  synth.setVoices([voice(ZH_TW_LOCAL.name, 'zh-TW', true)]);
  assert.equal(changes.length, 0);
  synth.setVoices([ZH_TW_LOCAL, ZH_CN_LOCAL]);
  assert.equal(changes.length, 1);
  synth.setVoices([ZH_CN_LOCAL, ZH_TW_LOCAL]);
  assert.equal(changes.length, 1, 'same set in another input order has the same sorted content');
});

test('a voice without localService counts as local and is not marked online; only an explicit false is online', () => {
  const noField = { name: '舊瀏覽器語音', lang: 'zh-TW', voiceURI: 'old' };
  const { speech } = setup({ voices: [noField, ZH_TW_ONLINE] });
  const byName = Object.fromEntries(speech.state.voices.map((item) => [item.name, item]));
  assert.equal(byName['舊瀏覽器語音'].local, true);
  assert.equal(byName[ZH_TW_ONLINE.name].local, false);
  assert.equal(speech.state.voices[0].name, '舊瀏覽器語音', 'it ranks as local');
});

test('voices without a voiceURI use the name as the key, duplicates collapse, and malformed entries are skipped', () => {
  const { speech } = setup({ voices: [{ name: '無識別', lang: 'zh-TW' }, { name: '無識別', lang: 'zh-TW' }, null, 'x', { lang: 12 }, { name: '', lang: 'zh-TW' }] });
  assert.deepEqual(speech.state.voices.map((item) => item.key), ['無識別']);
});

test('getVoices throwing is treated as an empty list', () => {
  const synth = fakeSynth();
  synth.getVoices = () => { throw new Error('nope'); };
  const { speech } = setup({ synth });
  assert.equal(speech.state.availability, 'ready');
  assert.deepEqual(speech.state.voices, []);
});

test('the voice list is re-read when speaking, in case a browser never fires voiceschanged', () => {
  const synth = fakeSynth({ voices: [] });
  const { speech } = setup({ synth, stored: AUTO });
  synth.voices = [ZH_TW_LOCAL];
  speech.speak('你好。');
  assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
});

// ---- 玩家的選擇 ---------------------------------------------------------------------------------------------------------------

test('a fresh player has no choice: toggle asks for one (voices present) and speaks nothing', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  assert.equal(speech.state.choice, 'none');
  assert.equal(speech.toggle('你好。'), 'needs-choice');
  assert.deepEqual(synth.calls, []);
  assert.equal(speech.state.speaking, false);
});

test('with an empty voice list there is nothing to choose, so toggle speaks right away', () => {
  const { speech } = setup({ voices: [] });
  assert.equal(speech.state.choice, 'none');
  assert.equal(speech.toggle('你好。'), 'started');
});

test('choosing auto is stored, takes effect at once, and a later toggle speaks without asking', () => {
  const { speech, synth, storage } = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL] });
  assert.equal(speech.choose({ mode: 'auto' }), true);
  assert.equal(speech.state.choice, 'auto');
  assert.equal(speech.state.selectedKey, '');
  assert.deepEqual(storage.store.writes, [[SPEECH_STORAGE_KEY, '{"mode":"auto"}']]);
  assert.equal(speech.toggle('你好。'), 'started');
  assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
});

test('choosing a voice is stored with its voiceURI and name only, is used even when a better-ranked voice exists, and is remembered after a reload', () => {
  const first = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL] });
  assert.equal(first.speech.choose({ mode: 'voice', key: ZH_CN_LOCAL.voiceURI }), true);
  assert.equal(first.speech.state.choice, 'voice');
  assert.equal(first.speech.state.selectedKey, ZH_CN_LOCAL.voiceURI);
  assert.deepEqual(JSON.parse(first.storage.store.value), { mode: 'voice', voiceURI: ZH_CN_LOCAL.voiceURI, name: ZH_CN_LOCAL.name });
  first.speech.toggle('你好。');
  assert.equal(first.synth.queue[0].voice, ZH_CN_LOCAL);

  const reloaded = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL], storage: first.storage });
  assert.equal(reloaded.speech.state.choice, 'voice');
  assert.equal(reloaded.speech.state.selectedKey, ZH_CN_LOCAL.voiceURI);
  assert.equal(reloaded.speech.toggle('你好。'), 'started', 'no menu, no automatic speech before the press');
  assert.equal(reloaded.synth.queue[0].voice, ZH_CN_LOCAL);
});

test('after a reload the choice is remembered but nothing is spoken by itself', () => {
  const { synth } = setup({ voices: [ZH_TW_LOCAL], stored: JSON.stringify({ mode: 'voice', voiceURI: ZH_TW_LOCAL.voiceURI, name: ZH_TW_LOCAL.name }) });
  assert.deepEqual(synth.calls, []);
});

test('the voiceURI changed but the name is the same: matched by name, and the stored value is not rewritten', () => {
  const stored = JSON.stringify({ mode: 'voice', voiceURI: 'old-uri', name: ZH_TW_LOCAL.name });
  const { speech, synth, storage } = setup({ voices: [voice(ZH_TW_LOCAL.name, 'zh-TW', true, 'new-uri'), ZH_CN_LOCAL], stored });
  assert.equal(speech.state.selectedKey, 'new-uri');
  speech.toggle('你好。');
  assert.equal(synth.queue[0].voice.voiceURI, 'new-uri');
  assert.deepEqual(storage.store.writes, []);
});

test('the voiceURI match wins over a same-name voice', () => {
  const stored = JSON.stringify({ mode: 'voice', voiceURI: 'b', name: '甲' });
  const { speech } = setup({ voices: [voice('甲', 'zh-TW', true, 'a'), voice('乙', 'zh-TW', true, 'b')], stored });
  assert.equal(speech.state.selectedKey, 'b');
});

test('a stored voice with an empty voiceURI is matched by name only, never by the empty URI of another voice', () => {
  // 名稱排序時「乙」在「甲」之前，所以若把空 URI 當成相符，會誤選排第一的「乙」
  const stored = JSON.stringify({ mode: 'voice', voiceURI: '', name: '甲' });
  const { speech, synth } = setup({ voices: [{ name: '甲', lang: 'zh-TW' }, { name: '乙', lang: 'zh-TW' }], stored });
  assert.deepEqual(speech.state.voices.map((item) => item.key), ['乙', '甲']);
  assert.equal(speech.state.selectedKey, '甲');
  speech.toggle('你好。');
  assert.equal(synth.queue[0].voice.name, '甲');
});

test('a chosen voice that no longer exists falls back to auto-pick, without an error and without rewriting the storage', () => {
  const stored = JSON.stringify({ mode: 'voice', voiceURI: 'gone', name: '已消失的語音' });
  const { speech, synth, storage } = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL], stored });
  assert.equal(speech.state.choice, 'voice');
  assert.equal(speech.state.selectedKey, '');
  assert.equal(speech.toggle('你好。'), 'started');
  assert.equal(synth.queue[0].voice, ZH_TW_LOCAL);
  assert.deepEqual(storage.store.writes, []);
  assert.equal(storage.store.value, stored);
});

test('a chosen voice that loads later is found once the list arrives', () => {
  const stored = JSON.stringify({ mode: 'voice', voiceURI: ZH_CN_LOCAL.voiceURI, name: ZH_CN_LOCAL.name });
  const { speech, synth } = setup({ voices: [], stored });
  synth.setVoices([ZH_TW_LOCAL, ZH_CN_LOCAL]);
  assert.equal(speech.state.selectedKey, ZH_CN_LOCAL.voiceURI);
});

test('a broken stored value is "never chosen"', () => {
  const broken = [
    'not json', '{', '[]', 'null', '"auto"', '12', 'true', '{}', '{"mode":"x"}', '{"mode":1}', '{"mode":"voice"}',
    '{"mode":"voice","voiceURI":1,"name":"a"}', '{"mode":"voice","voiceURI":"a","name":null}', '{"mode":"voice","voiceURI":"","name":""}', ''
  ];
  for (const value of broken) {
    const { speech } = setup({ voices: [ZH_TW_LOCAL], stored: value });
    assert.equal(speech.state.choice, 'none', value);
  }
});

test('extra fields in a stored value are ignored', () => {
  const { speech } = setup({ voices: [ZH_TW_LOCAL], stored: '{"mode":"auto","extra":1}' });
  assert.equal(speech.state.choice, 'auto');
});

test('storage that throws on read: never chosen, the page works', () => {
  const { speech } = setup({ voices: [ZH_TW_LOCAL], storage: fakeStorage(AUTO, { throwOnRead: true }) });
  assert.equal(speech.state.choice, 'none');
  assert.equal(speech.toggle('你好。'), 'needs-choice');
});

test('storage that throws on write: the choice still takes effect in memory', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL], storage: fakeStorage(null, { throwOnWrite: true }) });
  assert.doesNotThrow(() => speech.choose({ mode: 'voice', key: ZH_CN_LOCAL.voiceURI }));
  assert.equal(speech.state.choice, 'voice');
  assert.equal(speech.toggle('你好。'), 'started');
  assert.equal(synth.queue[0].voice, ZH_CN_LOCAL);
});

test('no storage at all (absent, null, or without functions): everything still works', () => {
  for (const storage of [undefined, null, {}, { getItem: 1, setItem: 2 }]) {
    const { speech, synth } = setup({ voices: [ZH_TW_LOCAL], storage });
    assert.equal(speech.state.choice, 'none');
    assert.equal(speech.choose({ mode: 'auto' }), true);
    assert.equal(speech.toggle('你好。'), 'started');
    assert.equal(synth.queue.length, 1);
  }
});

test('an invalid choice is refused and changes nothing', () => {
  const { speech, storage } = setup({ voices: [ZH_TW_LOCAL] });
  for (const selection of [null, undefined, 'auto', 5, [], {}, { mode: 'other' }, { mode: 'voice' }, { mode: 'voice', key: 'nobody' }, { mode: 'voice', key: 7 }]) {
    assert.equal(speech.choose(selection), false);
  }
  assert.equal(speech.state.choice, 'none');
  assert.deepEqual(storage.store.writes, []);
});

test('choosing stops the speech that is playing', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('你好。');
  const old = synth.queue[0];
  speech.choose({ mode: 'auto' });
  assert.equal(speech.state.speaking, false);
  assert.deepEqual(synth.queue, []);
  old.onend({});
  assert.equal(speech.state.speaking, false);
});

// ---- 試聽 ---------------------------------------------------------------------------------------------------------------------

test('preview stops the story speech first, then speaks the fixed sentence with the given voice, without touching the choice or the storage', () => {
  const { speech, synth, storage } = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL], stored: AUTO });
  speech.speak('劇情的一段文字，不應被試聽朗讀。');
  const story = synth.queue[0];
  const before = storage.store.value;
  assert.equal(speech.preview(ZH_CN_LOCAL.voiceURI), true);
  const calls = synth.calls;
  assert.equal(calls[calls.length - 2], 'cancel');
  assert.equal(calls[calls.length - 1], 'speak:' + SPEECH_PREVIEW_TEXT);
  assert.equal(SPEECH_PREVIEW_TEXT, '你好，這是語音試聽。');
  assert.equal(synth.queue[0].voice, ZH_CN_LOCAL);
  assert.equal(synth.queue[0].lang, 'zh-TW');
  story.onend({});
  assert.equal(speech.state.speaking, true, 'the old story speech cannot end the preview');
  synth.queue[0].onend({});
  assert.equal(speech.state.speaking, false);
  assert.equal(speech.state.choice, 'auto');
  assert.equal(storage.store.value, before);
  assert.deepEqual(storage.store.writes, []);
});

test('preview of an unknown voice does nothing', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  assert.equal(speech.preview('nobody'), false);
  assert.equal(speech.preview(undefined), false);
  assert.deepEqual(synth.calls, []);
});

// ---- 長文字 -------------------------------------------------------------------------------------------------------------------

test('text within the limit is a single utterance with the exact original text', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  const text = '他說：「你終於來了。」\n她沒有回答。'.padEnd(SPEECH_CHUNK_MAX, '。').slice(0, SPEECH_CHUNK_MAX);
  assert.equal(text.length, SPEECH_CHUNK_MAX);
  speech.speak(text);
  assert.equal(synth.queue.length, 1);
  assert.equal(synth.queue[0].text, text);
});

test('a long player action is cut into utterances that are queued in order and managed by one token: speaking until the last one ends', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  const sentence = '我緩緩推開那扇沉重的木門，小心翼翼地走進昏暗的大廳。';
  const text = sentence.repeat(30);                    // 約 800 字
  assert.ok(text.length > SPEECH_CHUNK_MAX * 4);
  assert.equal(speech.speak(text), true);
  const queued = synth.queue.slice();
  assert.ok(queued.length > 1);
  for (const utterance of queued) {
    assert.ok(utterance.text.length <= SPEECH_CHUNK_MAX, 'each utterance fits in the limit');
    assert.equal(utterance.lang, 'zh-TW');
    assert.equal(utterance.voice, ZH_TW_LOCAL);
  }
  assert.equal(queued.map((utterance) => utterance.text).join(''), text, 'no characters are lost or added');
  assert.equal(synth.cancels, 1, 'one cancel before the whole sequence');
  for (let index = 0; index < queued.length - 1; index += 1) {
    queued[index].onend({});
    assert.equal(speech.state.speaking, true);
  }
  queued[queued.length - 1].onend({});
  assert.equal(speech.state.speaking, false);
});

test('an error in the middle of a long speech drops the remaining utterances and returns to playable', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('我走了進去。'.repeat(60));
  const queued = synth.queue.slice();
  assert.ok(queued.length > 2);
  queued[0].onend({});
  queued[1].onerror({ error: 'synthesis-failed' });
  assert.equal(speech.state.speaking, false);
  assert.deepEqual(synth.queue, []);
  queued[2].onend({});
  assert.equal(speech.state.speaking, false);
});

test('a browser that fires end twice for one utterance cannot end a long speech early', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('我走了進去。'.repeat(60));
  const queued = synth.queue.slice();
  assert.ok(queued.length > 2);
  queued[0].onend({});
  queued[0].onend({});
  queued.slice(1, -1).forEach((utterance) => utterance.onend({}));
  assert.equal(speech.state.speaking, true, 'the last utterance has not ended yet');
  queued[queued.length - 1].onend({});
  assert.equal(speech.state.speaking, false);
});

test('stopping a long speech ignores every late event of its utterances', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  speech.speak('我走了進去。'.repeat(60));
  const queued = synth.queue.slice();
  speech.stop();
  speech.speak('新的一段。');
  queued.forEach((utterance) => { utterance.onend({}); utterance.onerror({}); });
  assert.equal(speech.state.speaking, true);
});

test('a long text without any sentence mark is still cut, with nothing lost', () => {
  const { speech, synth } = setup({ voices: [ZH_TW_LOCAL] });
  const text = '字'.repeat(SPEECH_CHUNK_MAX * 3 + 7);
  speech.speak(text);
  assert.ok(synth.queue.length >= 4);
  assert.equal(synth.queue.map((utterance) => utterance.text).join(''), text);
  assert.ok(synth.queue.every((utterance) => utterance.text.length <= SPEECH_CHUNK_MAX));
});

// ---- destroy ------------------------------------------------------------------------------------------------------------------

test('destroy stops the speech, removes the voiceschanged listener, and makes every later call and event inert', () => {
  const { speech, synth, changes } = setup({ voices: [ZH_TW_LOCAL], stored: AUTO });
  speech.speak('你好。');
  const utterance = synth.queue[0];
  assert.equal(synth.listenerCount, 1);
  const cancelsBefore = synth.cancels;
  speech.destroy();
  assert.equal(synth.cancels, cancelsBefore + 1);
  assert.equal(synth.listenerCount, 0);
  assert.equal(speech.state.speaking, false);
  const changesBefore = changes.length;
  const callsBefore = synth.calls.length;
  assert.equal(speech.speak('再聽。'), false);
  assert.equal(speech.toggle('再聽。'), 'unavailable');
  assert.equal(speech.preview(ZH_TW_LOCAL.voiceURI), false);
  assert.equal(speech.choose({ mode: 'auto' }), false);
  assert.doesNotThrow(() => { speech.stop(); speech.destroy(); });
  utterance.onend({});
  utterance.onerror({});
  synth.setVoices([ZH_TW_LOCAL, ZH_CN_LOCAL]);
  assert.equal(changes.length, changesBefore);
  assert.equal(synth.calls.length, callsBefore);
});

test('without addEventListener the onvoiceschanged property is used, and destroy clears it', () => {
  const synth = { voices: [], onvoiceschanged: null, getVoices() { return this.voices; }, speak() {}, cancel() {} };
  const changes = [];
  const speech = createStorySpeech({ synth, Utterance: FakeUtterance, storage: fakeStorage(), onChange: (state) => changes.push(state) });
  assert.equal(typeof synth.onvoiceschanged, 'function');
  synth.voices = [ZH_TW_LOCAL];
  synth.onvoiceschanged();
  assert.equal(speech.state.voices.length, 1);
  assert.equal(changes.length, 1);
  speech.destroy();
  assert.equal(synth.onvoiceschanged, null);
});

// ---- 規範：不留痕跡 --------------------------------------------------------------------------------------------------------------

test('only the voice-choice key is ever written, never any spoken text', () => {
  const { speech, synth, storage } = setup({ voices: [ZH_TW_LOCAL, ZH_CN_LOCAL] });
  speech.toggle('機密的劇情。');
  speech.choose({ mode: 'voice', key: ZH_TW_LOCAL.voiceURI });
  speech.toggle('機密的劇情。');
  synth.queue[0].onend({});
  speech.speak('機密的玩家行動。'.repeat(40));
  speech.preview(ZH_CN_LOCAL.voiceURI);
  speech.choose({ mode: 'auto' });
  speech.stop();
  speech.destroy();
  assert.ok(storage.store.writes.length >= 2);
  for (const [key, value] of storage.store.writes) {
    assert.equal(key, SPEECH_STORAGE_KEY);
    assert.ok(!value.includes('機密'), value);
    assert.ok(!value.includes('你好'), value);
  }
  assert.equal(SPEECH_STORAGE_KEY, 'rpg.speech.voice');
});

test('the module runs in a bare sandbox (no browser APIs), exposes Game.createStorySpeech, and writes nothing to the console', () => {
  const logged = [];
  const spy = (...args) => logged.push(args);
  const sandbox = { console: { log: spy, warn: spy, error: spy, info: spy, debug: spy }, Intl, Array, String, RegExp, Object, Symbol, Error, JSON };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'game', 'story-pages.js'), 'utf8'), sandbox);
  vm.runInContext(fs.readFileSync(SPEECH_FILE, 'utf8'), sandbox);
  assert.equal(typeof sandbox.Game.createStorySpeech, 'function');
  assert.equal(sandbox.Game.SPEECH_STORAGE_KEY, 'rpg.speech.voice');
  const synth = fakeSynth({ voices: [ZH_TW_LOCAL] });
  const speech = sandbox.Game.createStorySpeech({ synth, Utterance: FakeUtterance, storage: fakeStorage(AUTO), onChange() {} });
  speech.toggle('祕密的劇情。'.repeat(80));
  synth.queue[0].onerror({});
  speech.destroy();
  assert.deepEqual(logged, []);
});

test('the file holds no browser storage globals, network, DOM access, HTML rendering, console output, randomness or timers', () => {
  const source = fs.readFileSync(SPEECH_FILE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document', 'window', 'fetch(', 'XMLHttpRequest', 'innerHTML', 'v-html', 'console', 'Math.random', 'Date.now', 'setTimeout', 'setInterval', 'cookie']) {
    assert.ok(!source.includes(forbidden), forbidden);
  }
});
