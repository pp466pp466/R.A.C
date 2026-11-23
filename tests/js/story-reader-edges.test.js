'use strict';
/* rpg-story-pages 與 rpg-stream-client 規格的閱讀器邊界（story-reader.js）：變異驗證（rpg-story-view 8.1）找出原本沒有測試守住的判斷——
 * 第 0 幕（沒有玩家行動）的第一段說話者是旁白、已結束或被丟棄的輪次忽略後續片段、補完之後才到的打字機回呼不能改動畫面、
 * 說明狀態下 `jumpToEnd` 什麼都不做、說明只算一頁、還原要保留「還在等第一個劇情段」。不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const JS_DIR = path.join(__dirname, '..', '..', 'app', 'static', 'js');
const { createStoryReader, SPEAKER_YOU, SPEAKER_NARRATOR } = require(path.join(JS_DIR, 'game', 'story-reader.js'));

const P1 = '她推開門。屋裡很暗。有人在咳嗽。';
const P2 = '燈亮了。老人抬起頭。他笑了。';
const ACTION = '推開門';

/** 手動控制的打字機：測試自己決定何時呼叫回呼。 */
function manualWriters() {
  const writers = [];
  const factory = (handlers) => {
    const writer = { handlers, text: null, play(text) { this.text = text; }, flush() {}, cancel() {} };
    writers.push(writer);
    return writer;
  };
  return { writers, factory };
}

// ---- 第 0 幕沒有玩家行動：第一段是旁白，不是「你」 -----------------------------------------------------------------------

test('without a player action (the opening) the first page is the narrator, not "you"', () => {
  const reader = createStoryReader({});
  reader.reset('opening');
  reader.feed(P1 + '燈', false);
  assert.equal(reader.state.speaker, SPEAKER_NARRATOR);
  assert.equal(reader.state.index, 0);
  assert.equal(reader.state.text, P1);
  const loaded = createStoryReader({});
  loaded.load('opening', { story: P1 + P2 });
  assert.equal(loaded.state.speaker, SPEAKER_NARRATOR);
});

test('with a player action the action page is "you" and the story pages after it are the narrator', () => {
  const reader = createStoryReader({});
  reader.reset('turn:1', { action: ACTION });
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  reader.feed(P1 + '燈', false);
  assert.equal(reader.state.speaker, SPEAKER_NARRATOR);
  assert.equal(reader.state.index, 1);
});

// ---- 已結束或被丟棄的輪次忽略後續片段 ------------------------------------------------------------------------------------------

test('after done the round is closed: later chunks change nothing', () => {
  const reader = createStoryReader({});
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈亮了。', true);
  const before = JSON.stringify(reader.state);
  reader.feed(P2 + '再多一句。', false);
  reader.feed('更多。', true);
  assert.equal(JSON.stringify(reader.state), before);
  assert.equal(reader.state.total, 3);
});

test('after a discard without a retry the round is closed: late chunks cannot bring the story back over the one-line note', () => {
  const reader = createStoryReader({});
  reader.reset('turn:1', { action: ACTION });
  reader.discard('劇情轉場。');
  const before = JSON.stringify(reader.state);
  reader.feed(P1 + P2 + '燈', false);
  assert.equal(JSON.stringify(reader.state), before);
  assert.equal(reader.state.note, '劇情轉場。');
  assert.equal(reader.state.text, '劇情轉場。');
});

// ---- 補完之後才到的打字機回呼不能改動畫面 ----------------------------------------------------------------------------------------

test('a typewriter callback that arrives after the page was completed cannot change what is shown, even before the final done', () => {
  const { writers, factory } = manualWriters();
  const reader = createStoryReader({ createTypewriter: factory });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈亮了。', false);
  writers[0].handlers.onUpdate('她推');
  assert.equal(reader.next(), 'completed');
  assert.equal(reader.state.shown, P1);
  writers[0].handlers.onUpdate('她推開');                  // 補完之後才到：不能把已經補完的段退回部分文字
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.state.typing, false);
});

// ---- 說明狀態 ------------------------------------------------------------------------------------------------------------------

test('while a one-line note is shown, jumpToEnd does nothing and reports nothing', () => {
  const changes = [];
  const reader = createStoryReader({ onChange: (state) => changes.push(state) });
  reader.reset('turn:1', { action: ACTION });
  reader.discard('劇情轉場。', { willRetry: true });
  const count = changes.length;
  reader.jumpToEnd();
  assert.equal(changes.length, count);
  assert.equal(reader.state.note, '劇情轉場。');
});

test('a one-line note counts as exactly one page, with or without a player action', () => {
  const opening = createStoryReader({});
  opening.reset('opening');
  opening.discard('劇情轉場。');
  assert.equal(opening.state.total, 1);
  assert.equal(opening.state.index, 0);
  const turn = createStoryReader({});
  turn.reset('turn:1', { action: ACTION });
  turn.discard('劇情轉場。');
  assert.equal(turn.state.total, 1);
});

// ---- 還原：還在等第一個劇情段 ----------------------------------------------------------------------------------------------------

test('restoring a snapshot taken before any story page keeps waiting for the first page, which then takes over the card', () => {
  const reader = createStoryReader({});
  reader.reset('turn:1', { action: ACTION });
  const saved = reader.snapshot();
  reader.reset('turn:2', { action: '換一輪' });             // 修訂開始：從第一段重新閱讀，之後失敗還原
  reader.feed(P1 + '燈亮了。', false);
  assert.equal(reader.state.index, 1);
  reader.restore(saved);
  assert.equal(reader.state.key, 'turn:1');
  assert.equal(reader.state.index, 0);
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  reader.feed(P1 + '燈亮了。', false);                      // 還原之後第一個劇情段到達：接手畫面
  assert.equal(reader.state.index, 1);
  assert.equal(reader.state.speaker, SPEAKER_NARRATOR);
  assert.equal(reader.state.text, P1);
});
