'use strict';
/* rpg-story-pages、rpg-stream-client 與 rpg-revision-ui 規格的閱讀器（story-reader.js）：
 * 一次一段、打字中點擊先補完再翻頁、第一段停用 `‹`、回看與再翻到看過的段直接完整顯示、串流逐段到達與等待狀態、
 * `done` 釋出尾端、玩家行動先顯示為「你」、被擋與中斷只留一行說明（不留部分內容）、載入既有回合停在最後一段、
 * 修訂失敗還原、`prefers-reduced-motion`、過期的打字機回呼被忽略。以假時鐘與真正的 UI.TypewriterBuffer 跑，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS_DIR = path.join(__dirname, '..', '..', 'app', 'static', 'js');
const READER_FILE = path.join(JS_DIR, 'game', 'story-reader.js');
const { createStoryReader, SPEAKER_YOU, SPEAKER_NARRATOR } = require(READER_FILE);
const { paginate } = require(path.join(JS_DIR, 'game', 'story-pages.js'));
const { TypewriterBuffer } = require(path.join(JS_DIR, 'ui', 'typewriter.js'));

/** 假時鐘：與打字機的測試相同，不必真的等待。 */
function fakeClock() {
  let now = 0;
  let callback = null;
  let handle = 0;
  return {
    scheduler: {
      now: () => now,
      request(fn) { callback = fn; return ++handle; },
      cancel() { callback = null; }
    },
    advance(ms, step = 16) {
      const end = now + ms;
      while (now < end && callback) {
        now = Math.min(end, now + step);
        const fn = callback;
        callback = null;
        fn(now);
      }
      now = end;
    },
    get pending() { return callback !== null; }
  };
}

function setup(options = {}) {
  const clock = fakeClock();
  const changes = [];
  const writers = [];
  const reader = createStoryReader({
    createTypewriter: (handlers) => { const writer = new TypewriterBuffer({ ...handlers }); writers.push(writer); return writer; },
    scheduler: clock.scheduler,
    onChange: (state) => changes.push(state),
    ...options
  });
  return { reader, clock, changes, writers };
}

const P1 = '她推開門。屋裡很暗。有人在咳嗽。';
const P2 = '燈亮了。老人抬起頭。他笑了。';
const P3 = '你好。';
const STORY = P1 + P2 + P3;
const ACTION = '推開門';
const SETTLE = 4000;                                                            // 打字機補完一段的上限（finishSeconds 為 3 秒）

/** 把 P1、P2 釋出（P2 的第三句要等到下一個字元才算完整），玩家停在第一個劇情段。 */
function streamTwoPages(ctx) {
  ctx.reader.reset('turn:1', { action: ACTION });
  ctx.reader.feed(P1, false);
  ctx.reader.feed(P2, false);
  ctx.reader.feed('你', false);
}

// ---- 玩家行動先顯示為「你」---------------------------------------------------------------------------------------------

test('after reset with an action the card shows it as the player\'s own page: speaker "you", full text, no typing, no earlier page to go back to', () => {
  const { reader, writers } = setup();
  reader.reset('turn:1', { action: ACTION });
  assert.deepEqual(reader.state, {
    key: 'turn:1', speaker: SPEAKER_YOU, text: ACTION, shown: ACTION, index: 0, total: 1, hasAction: true,
    typing: false, waiting: true, finished: false, canPrev: false, canNext: false, note: ''
  });
  assert.equal(writers.length, 0, '行動頁不打字');
});

test('an opening scene has no action page: nothing is shown and the card is waiting until the first page arrives', () => {
  const { reader } = setup();
  reader.reset('opening');
  assert.deepEqual(reader.state, {
    key: 'opening', speaker: SPEAKER_NARRATOR, text: '', shown: '', index: 0, total: 0, hasAction: false,
    typing: false, waiting: true, finished: false, canPrev: false, canNext: false, note: ''
  });
});

test('with fewer than three complete sentences there is no page to turn to: the card keeps showing the action and the loading state', () => {
  const { reader, writers } = setup();
  reader.reset('turn:1', { action: ACTION });
  reader.feed('她推開門。屋裡很暗。有人', false);
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  assert.equal(reader.state.text, ACTION);
  assert.equal(reader.state.total, 1);
  assert.equal(reader.state.waiting, true);
  assert.equal(writers.length, 0);
});

test('the first released page replaces the action page by itself and starts typing; the action stays one step back', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈', false);
  let state = reader.state;
  assert.equal(state.speaker, SPEAKER_NARRATOR);
  assert.equal(state.text, P1);
  assert.equal(state.index, 1);
  assert.equal(state.total, 2);
  assert.equal(state.typing, true);
  assert.equal(state.shown, '');
  assert.equal(state.canPrev, true);
  clock.advance(SETTLE);
  state = reader.state;
  assert.equal(state.shown, P1);
  assert.equal(state.typing, false);
  assert.equal(reader.prev(), 'back');
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  assert.equal(reader.state.shown, ACTION);
  assert.equal(reader.state.canPrev, false);
});

test('the typewriter shows the page progressively, never ahead of the text and always a prefix of it', () => {
  const { reader, clock, changes } = setup();
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈', false);
  clock.advance(SETTLE);
  const typed = changes.map((state) => state.shown).filter((text) => text !== '' && text !== ACTION);
  assert.ok(typed.length > 3, '逐字而不是一次到位');
  for (const text of typed) { assert.ok(P1.startsWith(text), text); }
  assert.equal(typed[typed.length - 1], P1);
});

// ---- 點擊與翻頁 ---------------------------------------------------------------------------------------------------------

test('a click while a page is typing completes it first; only the second click turns the page', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(300);                                                                // 第一段還在打
  assert.equal(reader.state.typing, true);
  assert.ok(reader.state.shown.length < P1.length);
  assert.equal(reader.next(), 'completed');
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.state.typing, false);
  assert.equal(reader.state.index, 1, '第一下只補完，仍停在第一段');
  assert.equal(reader.next(), 'advanced');
  assert.equal(reader.state.index, 2);
  assert.equal(reader.state.text, P2);
  assert.equal(reader.state.typing, true);
});

test('the completed page stays complete: later typewriter frames cannot change it', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(200);
  reader.next();
  const before = reader.state.shown;
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, before);
  assert.equal(before, P1);
});

test('a page that has been typed once is shown in full when you go back to it and when you come forward again, with no new typing', () => {
  const ctx = setup();
  const { reader, clock, writers } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);                                                             // P1 打完
  reader.next();                                                                     // 翻到 P2：打字
  clock.advance(SETTLE);
  assert.equal(writers.length, 2);
  assert.equal(reader.prev(), 'back');
  assert.equal(reader.state.text, P1);
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.state.typing, false);
  assert.equal(reader.next(), 'advanced');
  assert.equal(reader.state.shown, P2);
  assert.equal(reader.state.typing, false);
  assert.equal(writers.length, 2, '回看與再翻回來都不再打字');
});

test('going back while the next page is still typing stops that typing and shows the earlier page in full', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);
  reader.next();
  clock.advance(200);
  assert.equal(reader.state.typing, true);
  reader.prev();
  assert.equal(reader.state.shown, P1);
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, P1, '被取消的打字不會再改寫畫面');
  assert.equal(reader.state.index, 1);
});

test('with no action page the first page disables going back, and so does the action page of a normal turn', () => {
  const opening = setup();
  opening.reader.reset('opening');
  opening.reader.feed(P1 + '燈', false);
  assert.equal(opening.reader.state.index, 0);
  assert.equal(opening.reader.state.canPrev, false);
  assert.equal(opening.reader.prev(), 'none');
  assert.equal(opening.reader.state.index, 0);
  const normal = setup();
  normal.reader.reset('turn:1', { action: ACTION });
  assert.equal(normal.reader.prev(), 'none');
  assert.equal(normal.reader.state.canPrev, false);
});

test('the last page does nothing when clicked again, and the action area stays: next reports end once the turn is finished', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1, false);
  reader.feed('', true);                                                             // P1 是最後一段
  clock.advance(SETTLE);
  const before = reader.state;
  assert.equal(reader.next(), 'end');
  assert.deepEqual(reader.state, before);
  assert.equal(reader.state.waiting, false);
  assert.equal(reader.state.canNext, false);
});

test('the player keeps their own pace: more pages arriving never move the card, and released pages keep their text', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);
  const first = reader.state;
  reader.feed('說：「早安。」她點頭。老人笑了。燈', false);
  const second = reader.state;
  assert.equal(second.index, first.index);
  assert.equal(second.text, first.text);
  assert.equal(second.shown, first.shown);
  assert.ok(second.total > first.total);
  assert.equal(second.canNext, true);
});

// ---- 串流期間的等待 -----------------------------------------------------------------------------------------------------

test('reaching the last released page while the stream is still open shows the waiting state, and more text lets you go on', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈', false);                                                    // 只有一個劇情段
  clock.advance(SETTLE);
  assert.equal(reader.state.waiting, true);
  assert.equal(reader.state.canNext, false);
  assert.equal(reader.next(), 'waiting');
  assert.equal(reader.state.index, 1);
  reader.feed('亮了。老人抬起頭。他笑了。你', false);
  assert.equal(reader.state.waiting, false);
  assert.equal(reader.state.canNext, true);
  assert.equal(reader.next(), 'advanced');
  assert.equal(reader.state.text, P2);
});

test('waiting is not set while earlier pages are being read, and clears for good when the stream is done', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);
  assert.equal(reader.state.waiting, false, '後面還有已釋出的段');
  reader.next();
  assert.equal(reader.state.waiting, true, '翻到目前最後一個已釋出的段');
  reader.feed('好。', true);
  assert.equal(reader.state.waiting, false);
  assert.equal(reader.state.finished, true);
});

test('done releases the unfinished tail as the last page, with no terminator needed', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed('她推開門。屋裡很暗', false);
  assert.equal(reader.state.total, 1);
  reader.feed('', true);
  assert.equal(reader.state.total, 2);
  assert.equal(reader.state.text, '她推開門。屋裡很暗');
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, '她推開門。屋裡很暗');
});

test('a done that arrives while a page is typing lets it finish within a few seconds instead of dumping the rest at once', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1, false);
  reader.feed('', true);
  clock.advance(100);
  assert.equal(reader.state.typing, true);
  assert.ok(reader.state.shown.length < P1.length / 2, '一開始不是一次噴出');
  clock.advance(SETTLE);
  assert.equal(reader.state.typing, false);
  assert.equal(reader.state.shown, P1);
});

test('a replayed turn (one big token and done) is paginated the same way as a streamed one', () => {
  const replayed = setup();
  replayed.reader.reset('turn:1', { action: ACTION });
  replayed.reader.feed(STORY, true);
  const streamed = setup();
  streamed.reader.reset('turn:1', { action: ACTION });
  for (const char of STORY) { streamed.reader.feed(char, false); }
  streamed.reader.feed('', true);
  assert.equal(replayed.reader.state.total, 4);
  assert.equal(streamed.reader.state.total, 4);
  for (const ctx of [replayed, streamed]) {
    const seenTexts = [];
    ctx.clock.advance(SETTLE);
    seenTexts.push(ctx.reader.state.text);
    while (ctx.reader.next() !== 'end') { ctx.clock.advance(SETTLE); seenTexts.push(ctx.reader.state.text); }
    assert.deepEqual(seenTexts.slice(-3), [P1, P2, P3]);
  }
});

// ---- 被擋、轉場與中斷 ---------------------------------------------------------------------------------------------------

test('a transition drops the unfinished story: the card shows only the one-line note, no navigation, nothing of the dropped text', () => {
  const ctx = setup();
  const { reader, clock, changes } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);
  const dropped = changes.length;
  reader.discard('場景轉換了。', { willRetry: true });
  const state = reader.state;
  assert.equal(state.note, '場景轉換了。');
  assert.equal(state.text, '場景轉換了。');
  assert.equal(state.shown, '場景轉換了。');
  assert.equal(state.speaker, SPEAKER_NARRATOR);
  assert.equal(state.canPrev, false);
  assert.equal(state.canNext, false);
  assert.equal(state.typing, false);
  assert.equal(state.waiting, true, '自動重生中');
  assert.equal(reader.next(), 'none');
  assert.equal(reader.prev(), 'none');
  for (const change of changes.slice(dropped)) { assert.ok(!change.text.includes('咳嗽') && !change.shown.includes('咳嗽')); }
  assert.deepEqual(reader.snapshot().raw, '', '丟棄的文字不留在閱讀器裡');
});

test('after a retrying transition the new answer replaces the note and is read from its first page, typed', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  reader.discard('場景轉換了。', { willRetry: true });
  reader.feed('新的第一句。新的第二句。新的第三句。新', false);
  const state = reader.state;
  assert.equal(state.note, '');
  assert.equal(state.text, '新的第一句。新的第二句。新的第三句。');
  assert.equal(state.index, 1);
  assert.equal(state.typing, true);
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, '新的第一句。新的第二句。新的第三句。');
  assert.ok(!JSON.stringify(reader.snapshot()).includes('咳嗽'));
});

test('a transition without a retry ends the turn: the note stays, later text is ignored, the input can unlock (finished, not waiting)', () => {
  const ctx = setup();
  const { reader } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed('一些尚未完成的內容。', false);
  reader.discard('換個行動試試。');
  reader.feed('', true);
  reader.feed('不該出現的內容。', false);
  const state = reader.state;
  assert.equal(state.note, '換個行動試試。');
  assert.equal(state.text, '換個行動試試。');
  assert.equal(state.finished, true);
  assert.equal(state.waiting, false);
  assert.equal(state.total, 1);
  assert.ok(!JSON.stringify(state).includes('不該出現'));
});

test('an interruption shows no partial content either, and a new turn starts clean', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(SETTLE);
  reader.discard('連線中斷了，這一輪沒有記下來。');
  assert.equal(reader.state.text, '連線中斷了，這一輪沒有記下來。');
  reader.reset('turn:2', { action: '再試一次' });
  assert.equal(reader.state.note, '');
  assert.equal(reader.state.text, '再試一次');
  assert.equal(reader.state.total, 1);
  assert.equal(reader.state.finished, false);
});

test('a discard while a page is typing cancels the typing for good', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(150);
  reader.discard('被擋了。');
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, '被擋了。');
});

// ---- 載入既有回合、跳到最後 -----------------------------------------------------------------------------------------------

test('loading an existing turn lands on its last page, shown in full without typing, and the pages before it can be read back', () => {
  const { reader, clock, writers } = setup();
  reader.load('turn:5', { action: ACTION, story: STORY });
  const state = reader.state;
  assert.equal(state.index, 3);
  assert.equal(state.total, 4);
  assert.equal(state.text, P3);
  assert.equal(state.shown, P3);
  assert.equal(state.typing, false);
  assert.equal(state.finished, true);
  assert.equal(state.waiting, false);
  assert.equal(state.canNext, false);
  assert.equal(state.canPrev, true);
  assert.equal(writers.length, 0);
  clock.advance(SETTLE);
  assert.equal(reader.prev(), 'back');
  assert.equal(reader.state.shown, P2);
  assert.equal(reader.prev(), 'back');
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.prev(), 'back');
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  assert.equal(reader.prev(), 'none');
  assert.equal(writers.length, 0, '回看不打字');
});

test('loading a turn with no story (blocked or interrupted earlier) shows the action page only; loading an opening scene has no action page', () => {
  const blocked = setup();
  blocked.reader.load('turn:5', { action: ACTION, story: '' });
  assert.equal(blocked.reader.state.speaker, SPEAKER_YOU);
  assert.equal(blocked.reader.state.total, 1);
  assert.equal(blocked.reader.state.finished, true);
  const opening = setup();
  opening.reader.load('opening', { story: STORY });
  assert.equal(opening.reader.state.hasAction, false);
  assert.equal(opening.reader.state.total, 3);
  assert.equal(opening.reader.state.index, 2);
  assert.equal(opening.reader.state.text, P3);
  const nothing = setup();
  nothing.reader.load('opening', {});
  assert.equal(nothing.reader.state.total, 0);
  assert.equal(nothing.reader.state.text, '');
});

test('jumpToEnd goes to the last released page without typing, and later pages do not take over the card', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  streamTwoPages(ctx);
  clock.advance(100);
  reader.jumpToEnd();
  assert.equal(reader.state.index, 2);
  assert.equal(reader.state.shown, P2);
  assert.equal(reader.state.typing, false);
  assert.equal(reader.state.waiting, true);
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, P2);
  reader.feed('好。', false);
  reader.feed('還有。', true);
  assert.equal(reader.state.index, 2, '不自動翻頁');
});

test('jumpToEnd on the action page alone does not stop the first story page from taking over later', () => {
  const { reader } = setup();
  reader.reset('turn:1', { action: ACTION });
  reader.jumpToEnd();
  assert.equal(reader.state.speaker, SPEAKER_YOU);
  reader.feed(P1 + '燈', false);
  assert.equal(reader.state.text, P1);
  assert.equal(reader.state.speaker, SPEAKER_NARRATOR);
});

// ---- 還原（修訂失敗）---------------------------------------------------------------------------------------------------

test('restore brings back the old content at the page where the player was, in full, and cancels the new content\'s typing', () => {
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.load('turn:3', { action: ACTION, story: STORY });
  reader.prev();                                                                     // 停在 P2
  const saved = reader.snapshot();
  reader.reset('turn:3', { action: ACTION });                                        // 重新生成：開始讀新的內容
  reader.feed('新的第一句。新的第二句。新的第三句。新', false);
  clock.advance(120);
  assert.equal(reader.state.typing, true);
  reader.restore(saved);
  const state = reader.state;
  assert.equal(state.text, P2);
  assert.equal(state.shown, P2);
  assert.equal(state.index, 2);
  assert.equal(state.typing, false);
  assert.equal(state.finished, true);
  assert.equal(state.total, 4);
  clock.advance(SETTLE);
  assert.equal(reader.state.shown, P2, '新內容被取消的打字不會再改寫畫面');
  assert.equal(reader.next(), 'advanced');
  assert.equal(reader.state.shown, P3);
  assert.equal(reader.state.typing, false, '還原前就讀過的段不再打字');
});

test('a snapshot is plain data that survives a JSON round trip (and it is never written anywhere by the reader)', () => {
  const ctx = setup();
  ctx.reader.load('turn:3', { action: ACTION, story: STORY });
  ctx.reader.prev();
  const saved = ctx.reader.snapshot();
  assert.deepEqual(JSON.parse(JSON.stringify(saved)), saved);
  const other = setup();
  other.reader.restore(JSON.parse(JSON.stringify(saved)));
  assert.equal(other.reader.state.text, P2);
  assert.equal(other.reader.state.index, 2);
});

test('restore ignores junk and clamps an out-of-range page', () => {
  const { reader } = setup();
  reader.load('turn:3', { action: ACTION, story: STORY });
  const before = reader.state;
  for (const junk of [undefined, null, 5, 'x']) { reader.restore(junk); assert.deepEqual(reader.state, before); }
  reader.restore({ key: 'k', action: ACTION, raw: P1, finished: true, note: '', awaitingFirst: false, index: 99, seen: 99 });
  assert.equal(reader.state.index, 1);
  reader.restore({ key: 'k', action: '', raw: '', finished: false, note: '', awaitingFirst: true, index: -5, seen: -5 });
  assert.equal(reader.state.total, 0);
  assert.equal(reader.state.index, 0);
});

test('restoring the state of a note brings the note back', () => {
  const { reader } = setup();
  reader.reset('turn:1', { action: ACTION });
  reader.discard('換個行動試試。');
  const saved = reader.snapshot();
  reader.reset('turn:2', { action: '別的' });
  reader.restore(saved);
  assert.equal(reader.state.note, '換個行動試試。');
  assert.equal(reader.state.key, 'turn:1');
});

// ---- 減少動態與沒有打字機 -----------------------------------------------------------------------------------------------

test('with reduced motion every page is shown in full at once and the typewriter is never created', () => {
  for (const reducedMotion of [true, () => true]) {
    const ctx = setup({ reducedMotion });
    const { reader, writers } = ctx;
    reader.reset('turn:1', { action: ACTION });
    reader.feed(STORY, true);
    assert.equal(reader.state.shown, P1);
    assert.equal(reader.state.typing, false);
    reader.next();
    assert.equal(reader.state.shown, P2);
    assert.equal(reader.state.typing, false);
    assert.equal(writers.length, 0);
  }
});

test('the reduced-motion preference is read each time a page is shown, so a change takes effect on the next page', () => {
  let reduce = false;
  const ctx = setup({ reducedMotion: () => reduce });
  const { reader, clock, writers } = ctx;
  reader.reset('turn:1', { action: ACTION });
  reader.feed(STORY, true);
  clock.advance(SETTLE);
  assert.equal(writers.length, 1);
  reduce = true;
  reader.next();
  assert.equal(reader.state.shown, P2);
  assert.equal(writers.length, 1);
});

test('a throwing preference or a missing typewriter factory falls back to showing the full text', () => {
  const throwing = setup({ reducedMotion: () => { throw new Error('x'); } });
  throwing.reader.reset('turn:1', { action: ACTION });
  throwing.reader.feed(STORY, true);
  assert.equal(throwing.reader.state.shown, P1);
  const bare = createStoryReader({});
  bare.reset('turn:1', { action: ACTION });
  bare.feed(STORY, true);
  assert.equal(bare.state.shown, P1);
  const failing = createStoryReader({ createTypewriter: () => { throw new Error('boom'); } });
  failing.reset('turn:1', { action: ACTION });
  failing.feed(STORY, true);
  assert.equal(failing.state.shown, P1);
  assert.equal(failing.state.typing, false);
});

// ---- 過期的打字機回呼 ---------------------------------------------------------------------------------------------------

/** 手動控制的打字機：測試自己決定何時呼叫回呼，用來證明過期的回呼被忽略。 */
function manualWriters() {
  const writers = [];
  const factory = (handlers) => {
    const writer = { handlers, text: null, flushed: 0, cancelled: 0, play(text) { this.text = text; }, flush() { this.flushed += 1; }, cancel() { this.cancelled += 1; } };
    writers.push(writer);
    return writer;
  };
  return { writers, factory };
}

test('callbacks from a typewriter that was completed, cancelled or replaced are ignored', () => {
  const { writers, factory } = manualWriters();
  const reader = createStoryReader({ createTypewriter: factory });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈亮了。老人抬起頭。他笑了。你', false);
  assert.equal(writers.length, 1);
  assert.equal(writers[0].text, P1);
  writers[0].handlers.onUpdate('她推');
  assert.equal(reader.state.shown, '她推');
  assert.equal(reader.next(), 'completed');
  assert.equal(writers[0].flushed, 1);
  assert.equal(reader.state.shown, P1);
  writers[0].handlers.onUpdate('她推開');                                       // 補完之後才到的回呼
  writers[0].handlers.onDone();
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.next(), 'advanced');
  assert.equal(writers.length, 2);
  writers[1].handlers.onUpdate('燈亮');
  reader.reset('turn:2', { action: '換一輪' });                                   // 換輪：第二個打字機被取消
  assert.equal(writers[1].cancelled, 1);
  writers[1].handlers.onUpdate('燈亮了。老人');
  writers[1].handlers.onDone();
  assert.equal(reader.state.text, '換一輪');
  assert.equal(reader.state.shown, '換一輪');
});

test('onDone ends the typing with the full text, and a typewriter that finishes synchronously inside play is handled', () => {
  const { writers, factory } = manualWriters();
  const reader = createStoryReader({ createTypewriter: factory });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈', false);
  writers[0].handlers.onUpdate('她推開');
  writers[0].handlers.onDone();
  assert.equal(reader.state.typing, false);
  assert.equal(reader.state.shown, P1);
  const sync = createStoryReader({
    createTypewriter: (handlers) => ({ play(text) { handlers.onUpdate(text); handlers.onDone(); }, flush() {}, cancel() {} })
  });
  sync.reset('turn:1', { action: ACTION });
  sync.feed(P1 + '燈', false);
  assert.equal(sync.state.shown, P1);
  assert.equal(sync.state.typing, false);
  assert.equal(sync.next(), 'waiting');
});

test('a typewriter that throws from play, flush or cancel never breaks the reader', () => {
  const reader = createStoryReader({
    createTypewriter: () => ({ play() { throw new Error('play'); }, flush() { throw new Error('flush'); }, cancel() { throw new Error('cancel'); } })
  });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈亮了。老人抬起頭。他笑了。你', false);
  assert.equal(reader.state.shown, P1);
  assert.equal(reader.state.typing, false);
  reader.next();
  assert.equal(reader.state.text, P2);
  reader.prev();
  assert.equal(reader.state.text, P1);
});

// ---- 離開頁面與畫面端的錯誤 ---------------------------------------------------------------------------------------------------

test('after destroy nothing changes and nothing is reported: typing is cancelled, input is ignored, callbacks are dropped', () => {
  const { writers, factory } = manualWriters();
  const reports = [];
  const reader = createStoryReader({ createTypewriter: factory, onChange: (state) => reports.push(state) });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(P1 + '燈', false);
  const count = reports.length;
  reader.destroy();
  assert.equal(writers[0].cancelled, 1);
  writers[0].handlers.onUpdate('她推開門');
  reader.feed('更多。更多。更多。更', false);
  assert.equal(reader.next(), 'none');
  assert.equal(reader.prev(), 'none');
  reader.reset('turn:9', { action: 'x' });
  reader.load('turn:9', { story: STORY });
  reader.discard('x');
  reader.restore({ key: 'x' });
  assert.equal(reports.length, count);
});

test('a throwing change listener does not break the reader', () => {
  const reader = createStoryReader({ onChange: () => { throw new Error('view'); } });
  reader.reset('turn:1', { action: ACTION });
  reader.feed(STORY, true);
  assert.equal(reader.state.total, 4);
  assert.equal(reader.next(), 'advanced');
});

test('state is a fresh plain object each time: changing it does not change the reader', () => {
  const { reader } = setup();
  reader.reset('turn:1', { action: ACTION });
  const state = reader.state;
  state.text = '改過了';
  state.index = 9;
  assert.equal(reader.state.text, ACTION);
  assert.equal(reader.state.index, 0);
});

// ---- 整段讀完 ----------------------------------------------------------------------------------------------------------------

test('reading a streamed turn page by page shows exactly the pages of the final text, in order, each unchanged since it was first shown', () => {
  const text = '她推開門。屋裡很暗。有人在咳嗽。燈亮了。老人抬起頭，看著你。他笑了。\n你好。\n「你來了。我等你很久了。」他說。\n再見。';
  const expected = paginate(text, { done: true }).pages;
  assert.ok(expected.length >= 4);
  const ctx = setup();
  const { reader, clock } = ctx;
  reader.reset('turn:1', { action: ACTION });
  const firstSeen = new Map();
  const remember = () => { const state = reader.state; if (state.speaker === SPEAKER_NARRATOR && state.text) { if (!firstSeen.has(state.index)) { firstSeen.set(state.index, state.text); } assert.equal(firstSeen.get(state.index), state.text); } };
  for (let at = 0; at < text.length; at += 3) {                                      // 三個字一塊到達
    reader.feed(text.slice(at, at + 3), false);
    clock.advance(40);
    remember();
  }
  reader.feed('', true);
  const read = [];
  clock.advance(SETTLE);
  remember();
  read.push(reader.state.text);
  while (reader.next() !== 'end') { clock.advance(SETTLE); remember(); if (reader.state.text !== read[read.length - 1]) { read.push(reader.state.text); } }
  assert.deepEqual(read, expected);
  assert.equal(reader.state.shown, expected[expected.length - 1]);
});

// ---- 規範：不留痕跡 ----------------------------------------------------------------------------------------------------------

test('the module runs in a bare sandbox (no browser APIs), exposes Game.createStoryReader, and writes nothing to the console', () => {
  const logged = [];
  const spy = (...args) => logged.push(args);
  const sandbox = { console: { log: spy, warn: spy, error: spy, info: spy, debug: spy }, Intl, Array, String, RegExp, Object, Symbol, Error };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'game', 'story-pages.js'), 'utf8'), sandbox);
  vm.runInContext(fs.readFileSync(READER_FILE, 'utf8'), sandbox);
  assert.equal(typeof sandbox.Game.createStoryReader, 'function');
  assert.equal(sandbox.Game.SPEAKER_YOU, 'you');
  const reader = sandbox.Game.createStoryReader({});
  reader.reset('turn:1', { action: '玩家的祕密行動' });
  reader.feed('祕密的劇情。祕密的劇情。祕密的劇情。祕', false);
  reader.discard('被擋了。');
  assert.deepEqual(logged, []);
});

test('the files hold no browser storage, network, DOM writes, HTML rendering, console output or randomness', () => {
  for (const name of ['story-reader.js', 'story-pages.js']) {
    const source = fs.readFileSync(path.join(JS_DIR, 'game', name), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
    for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'XMLHttpRequest', 'innerHTML', 'v-html', 'console.', 'Math.random', 'Date.now', 'setTimeout', 'setInterval']) {
      assert.ok(!source.includes(forbidden), `${name}: ${forbidden}`);
    }
  }
});
