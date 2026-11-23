'use strict';
/* rpg-story-view 6.1：遊戲畫面的純函式新增的部分（game-state.js、game-labels.js）——
 * 「直接推進劇情」的固定行動文字（與後端相等）、劇情卡的顯示欄位、行動建議的整理與啟用條件、建議端點的失敗對照。
 * 純函式，不需要瀏覽器；訊息一律是固定的繁體中文，不含任何伺服器文字或玩家文字。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'app', 'static', 'js', 'game');
const State = require(path.join(JS_DIR, 'game-state.js'));
const Labels = require(path.join(JS_DIR, 'game-labels.js'));

const HAN = /[一-鿿]/;
const MARKER = 'ZZPLAYER_SECRET_ZZ';
const NO_CHARGE = '沒有扣除遊戲幣';

/* Auth.describeError 整理過的失敗（與 game-state.test.js 相同的形狀）。 */
const info = (status, code, extra) => Object.assign({ kind: 'server', status, code, message: '伺服器的訊息', fields: {}, categories: [] }, extra);

test('the advance action is the fixed four-character platform text and equals the backend constant', () => {
  assert.equal(State.ADVANCE_ACTION, '推進劇情');
  assert.equal(State.ADVANCE_ACTION.length, 4);
  const backend = fs.readFileSync(path.join(ROOT, 'app', 'services', 'rpg_prompt.py'), 'utf8');
  const match = /^ADVANCE_ACTION = "([^"]+)"$/m.exec(backend);
  assert.ok(match, '後端的 rpg_prompt.py 找不到 ADVANCE_ACTION');
  assert.equal(match[1], State.ADVANCE_ACTION);                                  // 前後端必須相等：不相等時後端的推進規則不會被觸發
  assert.equal(State.actionProblem(State.ADVANCE_ACTION, 1000), '');             // 它本身是合格的行動文字（走一般的送出流程）
});

test('the card view of nothing is an empty, invisible card and unknown values fall back safely', () => {
  for (const nothing of [null, undefined, 'x', 7, [], {}, { key: '' }, { key: null }, { key: 3 }]) {
    const card = State.cardView(nothing);
    assert.equal(card.visible, false, JSON.stringify(nothing));
    assert.equal(card.text, '');
    assert.equal(card.fullText, '');
    assert.equal(card.canNext, false);
    assert.equal(card.canPrev, false);
    assert.equal(card.speakerLabel, '旁白');
  }
  const odd = State.cardView({ key: 'turn:a', speaker: '<b>', shown: 5, text: null, index: -1, total: 'x', canPrev: 1, canNext: 'yes', typing: 1, waiting: 0, finished: 'true' });
  assert.equal(odd.visible, true);
  assert.equal(odd.speaker, 'narrator');
  assert.equal(odd.text, '');
  assert.equal(odd.index, 0);
  assert.equal(odd.total, 0);
  assert.equal(odd.canPrev, false);                                              // 只有嚴格的 true 才算（失敗時關閉）
  assert.equal(odd.canNext, false);
  assert.equal(odd.typing, false);
  assert.equal(odd.finished, false);
});

test('the card view labels the speaker, keeps the typed progress apart from the full text and shows a note alone', () => {
  const you = State.cardView({ key: 'turn:a', speaker: 'you', shown: '推開門', text: '推開門', index: 0, total: 1, hasAction: true, waiting: true, finished: false });
  assert.equal(you.speakerLabel, '你');
  assert.equal(you.text, '推開門');
  assert.equal(you.waiting, true);
  assert.equal(you.waitingText, Labels.storyText('waiting'));                    // 等待首字：行動頁加上等待說明
  assert.equal(you.position, '');                                                // 行動頁不算劇情段

  const typing = State.cardView({ key: 'turn:a', speaker: 'narrator', shown: '門後', text: '門後是一條長廊。', index: 1, total: 3, hasAction: true, typing: true, canPrev: true, canNext: true, waiting: false, finished: false });
  assert.equal(typing.speakerLabel, '旁白');
  assert.equal(typing.text, '門後');                                              // 打字機目前顯示到哪裡
  assert.equal(typing.fullText, '門後是一條長廊。');                              // 完整文字給輔助科技，與打字進度分開
  assert.equal(typing.waitingText, '');                                          // 打字中不重複顯示等待說明

  const waitingAtEnd = State.cardView({ key: 'turn:a', speaker: 'narrator', shown: '一句。', text: '一句。', index: 1, total: 2, hasAction: true, typing: false, waiting: true, finished: false });
  assert.equal(waitingAtEnd.waitingText, Labels.storyText('waiting'));           // 翻到目前最後一個已釋出的段、串流還沒結束：等待狀態（不是錯誤）

  const note = State.cardView({ key: 'turn:a', speaker: 'narrator', shown: '劇情在這裡轉了個方向。', text: '劇情在這裡轉了個方向。', note: '劇情在這裡轉了個方向。', index: 0, total: 1, finished: true });
  assert.equal(note.note, '劇情在這裡轉了個方向。');
  assert.equal(note.position, '');
  assert.equal(note.atEnd, true);
});

test('the card position counts only story pages, only once the story is finished, and the end is the last page of a finished story', () => {
  const base = { key: 'turn:a', speaker: 'narrator', shown: 'x', text: 'x', hasAction: true };
  assert.equal(State.cardView(Object.assign({}, base, { index: 2, total: 4, finished: true, canNext: true })).position, '2 / 3');      // 含行動頁共 4 頁 = 3 個劇情段，目前在第 2 個
  assert.equal(State.cardView(Object.assign({}, base, { index: 3, total: 4, finished: true, canNext: false })).position, '3 / 3');
  assert.equal(State.cardView(Object.assign({}, base, { index: 2, total: 4, finished: false, canNext: true })).position, '');          // 串流中總段數還會增加：不顯示
  assert.equal(State.cardView(Object.assign({}, base, { index: 1, total: 2, finished: true })).position, '');                          // 只有一個劇情段：不顯示
  assert.equal(State.cardView(Object.assign({}, base, { hasAction: false, index: 0, total: 3, finished: true, canNext: true })).position, '1 / 3');   // 第 0 幕沒有行動頁
  assert.equal(State.cardView(Object.assign({}, base, { index: 3, total: 4, finished: true, canNext: false })).atEnd, true);
  assert.equal(State.cardView(Object.assign({}, base, { index: 3, total: 4, finished: false, canNext: false })).atEnd, false);         // 串流還沒結束：還沒到底
  assert.equal(State.cardView(Object.assign({}, base, { index: 1, total: 4, finished: true, canNext: true })).atEnd, false);
});

test('suggestion items keep at most three distinct trimmed strings and ignore everything else', () => {
  assert.deepEqual(State.suggestionItems(['  走進旅店 ', '問路', '問路', '', '   ', 42, null, { text: 'x' }, '回頭', '第四個']), ['走進旅店', '問路', '回頭']);
  assert.deepEqual(State.suggestionItems(undefined), []);
  assert.deepEqual(State.suggestionItems('走進旅店'), []);
  assert.deepEqual(State.suggestionItems({ 0: 'a' }), []);
  assert.deepEqual(State.suggestionItems(['x'.repeat(61), '正常的行動']), ['正常的行動']);                      // 明顯過長的項目被丟棄（前端只是寬鬆的上界，驗證在後端）
  const input = ['<b>粗體</b>'];
  assert.deepEqual(State.suggestionItems(input), ['<b>粗體</b>']);                                              // 原樣保留，由樣板以 {{ }} 顯示成文字，不在這裡「清洗」
  assert.deepEqual(input, ['<b>粗體</b>']);                                                                      // 不修改傳入的陣列
});

test('the suggestion icon and the advance button share the conditions of sending an action, with the unread opening included', () => {
  const ready = { gate: null, ended: false, offline: false, openingReady: true, reading: false, inflight: null, busy: false };
  assert.equal(State.canSuggest(ready), true);
  assert.equal(State.canAdvance(ready), true);
  const cases = [
    [{ gate: 'account_locked' }, 'gate'], [{ ended: true }, 'ended'], [{ offline: true }, 'offline'], [{ openingReady: false }, 'opening'],
    [{ reading: true }, 'reading'], [{ inflight: { client_msg_id: 'a' } }, 'inflight'], [{ busy: true }, 'busy']
  ];
  for (const [change, reason] of cases) {
    const state = Object.assign({}, ready, change);
    assert.equal(State.suggestBlocker(state), reason, reason);
    assert.equal(State.advanceBlocker(state), reason, reason);
    assert.equal(State.sendBlocker(state), reason, reason);                      // 三者的條件相同
    assert.equal(State.canSuggest(state), false);
    assert.equal(State.canAdvance(state), false);
    assert.ok(Labels.actionHint(reason), `「${reason}」要有說明`);
  }
  // 順序固定：閘門 > 已完結 > 離線 > 第 0 幕 > 還沒翻到最後一段 > 在途回合 > 進行中
  assert.equal(State.sendBlocker({ gate: 'x', ended: true, offline: true, openingReady: false, reading: true, inflight: {}, busy: true }), 'gate');
  assert.equal(State.sendBlocker({ ended: true, offline: true, openingReady: false, reading: true, inflight: {}, busy: true }), 'ended');
  assert.equal(State.sendBlocker({ offline: true, openingReady: false, reading: true, inflight: {}, busy: true }), 'offline');
  assert.equal(State.sendBlocker({ openingReady: false, reading: true, inflight: {}, busy: true }), 'opening');
  assert.equal(State.sendBlocker({ openingReady: true, reading: true, inflight: {}, busy: true }), 'reading');
  assert.equal(State.sendBlocker({ openingReady: true, inflight: {}, busy: true }), 'inflight');
  // 失敗時關閉：狀態不明就不能使用
  for (const unknown of [null, undefined, 'x', 0]) { assert.equal(State.canSuggest(unknown), false); assert.equal(State.canAdvance(unknown), false); }
  assert.equal(State.canSuggest({ openingReady: 'yes' }), false);
});

test('suggestion failures are described in fixed Traditional Chinese for every status the endpoint can answer with', () => {
  const unavailable = State.describeSuggestionFailure(info(503, 'suggestions_unavailable'));
  assert.equal(unavailable.message, '建議暫時無法產生，可以自己輸入行動。');               // 規格指定的文字
  assert.equal(unavailable.kind, 'unavailable');
  assert.equal(unavailable.retry, true);                                         // 圖示仍可再按
  assert.equal(unavailable.reload, false);

  const stale = State.describeSuggestionFailure(info(409, 'suggestions_stale'));
  assert.equal(stale.kind, 'stale');
  assert.equal(stale.reload, true);                                              // 劇情已前進：重讀狀態
  assert.equal(stale.catchUp, true);
  assert.equal(stale.message, Labels.suggestText('stale'));

  const ended = State.describeSuggestionFailure(info(409, 'session_ended'));
  assert.equal(ended.kind, 'ended');
  assert.equal(ended.ended, true);

  const pending = State.describeSuggestionFailure(info(409, 'opening_pending'));
  assert.equal(pending.kind, 'opening_pending');
  assert.equal(pending.reload, true);

  const busy = State.describeSuggestionFailure(info(409, 'turn_in_progress'));
  assert.equal(busy.kind, 'busy');
  assert.equal(busy.catchUp, true);

  const system = State.describeSuggestionFailure(info(503, 'system_busy'));
  assert.equal(system.kind, 'unavailable');
  assert.equal(system.message, '目前使用人數較多，請稍後再試。');
  assert.equal(State.describeSuggestionFailure(info(503, 'service_unavailable')).message, '故事引擎目前無法使用，請稍後再試。');
  assert.equal(State.describeSuggestionFailure(info(404, 'not_found')).kind, 'not_found');
  assert.equal(State.describeSuggestionFailure(info(401, 'unauthorized')).kind, 'login');
  assert.equal(State.describeSuggestionFailure(info(403, 'age_confirmation_required')).kind, 'gate');
  assert.equal(State.describeSuggestionFailure({ kind: 'canceled', status: 0, code: '' }).silent, true);       // 玩家取消：不顯示成錯誤
  assert.equal(State.describeSuggestionFailure({ kind: 'network', status: 0, code: '' }).kind, 'network');
  assert.equal(State.describeSuggestionFailure({ kind: 'timeout', status: 0, code: '' }).kind, 'timeout');
  for (const failure of [unavailable, stale, ended, pending, busy, system]) {
    assert.ok(HAN.test(failure.message));
    assert.ok(!failure.message.includes(NO_CHARGE), '建議本來就不扣遊戲幣，不附「沒有扣除遊戲幣」');
  }
});

test('suggestion failure messages never contain the server text or a marker', () => {
  for (const [status, code] of [[503, 'suggestions_unavailable'], [409, 'suggestions_stale'], [409, 'turn_in_progress'], [503, 'system_busy'], [500, 'internal_error']]) {
    const failure = State.describeSuggestionFailure(info(status, code, { message: MARKER }));
    assert.ok(!failure.message.includes(MARKER), `${status} ${code}`);
    assert.equal(typeof failure.message, 'string');
  }
});

test('the story and suggestion texts are fixed Traditional Chinese with no placeholder, markup or template leftover, and unknown keys fall back', () => {
  const texts = [
    Labels.speakerLabel('narrator'), Labels.speakerLabel('you'), Labels.speakerLabel('anything'),
    Labels.storyText('waiting'), Labels.storyText('back_label'), Labels.storyText('next_label'), Labels.storyText('nope'),
    ...['unavailable', 'stale', 'loading', 'icon_label', 'close_label', 'cancel_label', 'nope'].map((key) => Labels.suggestText(key)),
    ...['gate', 'ended', 'offline', 'opening', 'reading', 'inflight', 'busy', 'suggesting', 'loading'].map((key) => Labels.actionHint(key))
  ];
  for (const text of texts) {
    assert.ok(HAN.test(text), text);
    assert.ok(!/[<>{}]/.test(text) && !text.includes('undefined') && !text.includes('null'), text);
  }
  assert.equal(Labels.speakerLabel('narrator'), '旁白');
  assert.equal(Labels.speakerLabel('you'), '你');
  assert.equal(Labels.speakerLabel('__proto__'), '旁白');                       // 不把原型鏈上的鍵當成對照
  assert.equal(Labels.suggestText('constructor'), Labels.suggestText('unavailable'));
  assert.equal(Labels.actionHint('constructor'), '');                            // 不認得的原因沒有說明
  assert.equal(Labels.actionHint(''), '');
  assert.equal(Labels.actionHint(undefined), '');
});
