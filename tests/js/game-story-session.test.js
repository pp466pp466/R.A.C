'use strict';
/* rpg-story-view 6.3：遊戲主畫面的劇情卡、直接推進劇情、行動建議與鍵盤翻頁（game-session.js 接 story-reader.js）——
 * 規格 rpg-story-pages、rpg-suggestions、rpg-advance-action 與 rpg-stream-client 的情境：
 * 載入既有存檔停在最後一段且不打字、新的一輪先顯示「你」的行動頁再逐段釋出、不自動翻頁、被擋與中斷只留一行說明、開始前被拒絕還原成送出前的畫面、
 * 第 0 幕要翻到最後一段才能行動、建議預設不呼叫且重開不重複請求、貼進輸入框不送出、推進不動草稿，以及整個流程沒有玩家文字進主控台或瀏覽器儲存。
 * 環境見 tests/js/support/game-harness.js（劇情卡的打字機是獨立的替身 `readerTypewriters`，與每個氣泡一個的 `typewriters` 分開）。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  createHarness, trapGlobals, failureInfo, turnView, normalTurn, event, plain, SESSION_ID, Labels, State,
} = require('./support/game-harness.js');

// 七個短句：每三句一段 -> 三段
const S1 = '甲說話了。乙回答了。丙笑了。';
const S2 = '丁走了。戊來了。己坐下。';
const S3 = '庚離開。';
const STORY = S1 + S2 + S3;
const OPENING_TEXT = '第零幕：山門前的石階。';

const doneEvent = (extra) => event('done', Object.assign({ status: 'complete', turn_number: 1, coins: 200 }, extra));
const metaEvent = (seq, extra) => event('meta', Object.assign({ turn_id: seq, attempt: 1, replayed: false }, extra));
const completeView = (seq, id, story) => turnView(seq, id, undefined, story);

async function ready(options) {
  const h = createHarness(options);
  await h.build();
  await h.session.load();
  return h;
}

async function sendAction(h, action, plan) {
  if (plan) { h.server.streamPlans.push(plan); }
  h.session.draft.value = action;
  return h.session.send();
}

/* 已有一輪（最後一輪的故事是三段的 STORY）的存檔。 */
const withTurns = (extra) => Object.assign({ turns: [turnView(1, 't1', '第一輪的行動', STORY)], session: { completed_turns: 1 } }, extra);

const key = (event_) => Object.assign({ key: 'x', shiftKey: false }, event_);
const keyEvent = (name, extra) => {
  const record = { prevented: 0 };
  return Object.assign({ key: name, target: { tagName: 'BODY' }, preventDefault() { record.prevented += 1; }, record }, extra);
};

// ---- 載入既有存檔 ----------------------------------------------------------------------------------------

test('loading an existing save shows the last page of the current turn without typing, and "back" stays inside that turn', async () => {
  const h = await ready(withTurns());
  const card = h.card();
  assert.equal(card.visible, true);
  assert.equal(card.key, 'turn:t1');
  assert.equal(card.speakerLabel, '旁白');
  assert.equal(card.text, S3);                                       // 停在最後一段
  assert.equal(card.fullText, S3);
  assert.equal(card.index, 3);                                       // 第 0 頁是行動頁，第 1 至 3 頁是劇情
  assert.equal(card.total, 4);
  assert.equal(card.canNext, false);
  assert.equal(card.canPrev, true);
  assert.equal(card.atEnd, true);
  assert.equal(card.position, '3 / 3');
  assert.equal(h.readerTypewriters.length, 0);                       // 載入不打字
  assert.equal(h.session.reading.value, false);
  assert.equal(h.session.sendDisabled.value, false);                 // 行動區維持可用

  assert.equal(h.session.prevPage(), 'back');
  assert.equal(h.card().text, S2);                                   // 回看直接完整顯示
  assert.equal(h.card().typing, false);
  assert.equal(h.session.prevPage(), 'back');
  assert.equal(h.card().text, S1);
  assert.equal(h.session.prevPage(), 'back');
  assert.equal(h.card().speakerLabel, '你');                         // 當前輪的第 0 段是玩家的行動
  assert.equal(h.card().text, '第一輪的行動');
  assert.equal(h.session.prevPage(), 'none');                        // 不會回到上一輪或第 0 幕之前
  assert.equal(h.card().key, 'turn:t1');
  assert.equal(h.card().canPrev, false);
  assert.equal(h.readerTypewriters.length, 0);
});

test('with only the opening the card shows the last page of the opening, and a page of an older turn is never the current one', async () => {
  const h = await ready({ session: { opening: { state: 'ready', text: '第一句。第二句。第三句。第四句。' } } });
  const card = h.card();
  assert.equal(card.key, 'opening');
  assert.equal(card.text, '第四句。');
  assert.equal(card.hasAction, false);                               // 第 0 幕沒有玩家行動
  assert.equal(card.canPrev, true);
  h.session.prevPage();
  assert.equal(h.card().text, '第一句。第二句。第三句。');
  assert.equal(h.session.prevPage(), 'none');                        // 第一段停用
  assert.equal(h.session.reading.value, false);                      // 載入的第 0 幕直接停在最後一段，不會卡住行動區
});

test('a save with several turns shows only the last complete turn on the card, while the history keeps every turn', async () => {
  const h = await ready({
    turns: [turnView(1, 'a', '行動一', '劇情一。'), turnView(2, 'b', '行動二', '劇情二。'), turnView(3, 'c', '行動三', '劇情三。')],
    session: { completed_turns: 3 },
  });
  assert.equal(h.card().key, 'turn:c');
  assert.equal(h.card().text, '劇情三。');
  assert.deepEqual(h.kinds(), ['opening', 'turn', 'turn', 'turn']);   // 完整的時間軸（歷史用）照舊
});

test('a blocked turn after the last complete one is not the current turn: the card still shows the last complete turn', async () => {
  const h = await ready({
    turns: [turnView(1, 'a', '行動一', '劇情一。'), { seq: 2, client_msg_id: 'b', turn_number: null, status: 'blocked', action: null, story: null }],
    session: { completed_turns: 1 },
  });
  assert.equal(h.card().key, 'turn:a');
  assert.equal(h.card().note, '');
});

test('offline, the card is built from the local cache; coming back online does not disturb a page the player is reading', async () => {
  const seeded = createHarness(withTurns());
  await seeded.build();
  await seeded.session.load();
  const h = createHarness(Object.assign({ online: false }, {
    seed: async (store) => {
      await store.putSession(SESSION_ID, seeded.server.session);
      await store.putTurns(SESSION_ID, await seeded.store.turnsOf(SESSION_ID));
    },
  }));
  await h.build();
  await h.session.load();
  assert.equal(h.session.offline.value, true);
  assert.equal(h.card().key, 'turn:t1');
  assert.equal(h.card().text, S3);
  h.session.prevPage();                                              // 玩家回看第二段
  assert.equal(h.card().text, S2);
  h.setOnline(true);
  await h.session.setOnline(true);                                   // 恢復連線：重新載入，後端內容相同
  assert.equal(h.session.offline.value, false);
  assert.equal(h.card().text, S2);                                   // 閱讀位置沒有被重載打斷
  assert.equal(h.card().key, 'turn:t1');
});

test('a changed turn on the server (another device) replaces the card on the next load, at the last page', async () => {
  const h = await ready(withTurns());
  h.session.prevPage();
  h.server.turns.push(turnView(2, 't2', '另一個裝置的行動', '新的一句。又一句。'));
  h.server.session.completed_turns = 2;
  await h.session.load({ soft: true });
  assert.equal(h.card().key, 'turn:t2');
  assert.equal(h.card().text, '新的一句。又一句。');
  assert.equal(h.card().atEnd, true);
});

// ---- 串流一輪 --------------------------------------------------------------------------------------------

test('a streamed turn shows the action as "you" first, releases a page only when it is full, never turns pages by itself and ends with every page', async () => {
  const h = await ready();
  const seen = {};
  const snap = (label) => () => { seen[label] = h.card(); };
  const plan = {
    events: [
      metaEvent(1),
      snap('start'),
      event('token', { text: '甲說話了。乙回答了。' }),
      snap('two-sentences'),
      event('token', { text: '丙笑了。丁走了。' }),
      snap('first-page'),
      event('token', { text: '戊來了。己坐下。庚離開。' }),
      event('state', { updated: false }),
      doneEvent({ coins: 200 }),
    ],
    commit: { view: completeView(1, 'msg-1', STORY) },
  };
  assert.equal(await sendAction(h, '我的行動', plan), true);

  assert.equal(seen.start.key, 'turn:msg-1');
  assert.equal(seen.start.speakerLabel, '你');                       // 送出後立刻顯示玩家的行動
  assert.equal(seen.start.text, '我的行動');
  assert.equal(seen.start.waiting, true);
  assert.equal(seen['two-sentences'].text, '我的行動');              // 還沒湊滿：沒有可翻的段，仍是行動頁
  assert.equal(seen['two-sentences'].total, 1);
  assert.equal(seen['first-page'].speakerLabel, '旁白');             // 第一段湊滿後自動接手畫面
  assert.equal(seen['first-page'].text, S1);
  assert.equal(seen['first-page'].index, 1);
  assert.equal(seen['first-page'].total, 2);
  assert.equal(seen['first-page'].canNext, false);                   // 翻到目前最後一個已釋出的段
  assert.equal(seen['first-page'].waiting, true);                    // 串流尚未結束：等待狀態，不是錯誤
  assert.equal(seen['first-page'].waitingText, Labels.storyText('waiting'));

  const end = h.card();                                              // 串流結束、補齊與快照重讀都完成之後
  assert.equal(end.text, S1);                                        // 玩家停在第一段：不自動翻頁，閱讀位置不被補齊打斷
  assert.equal(end.index, 1);
  assert.equal(end.total, 4);
  assert.equal(end.finished, true);
  assert.equal(end.canNext, true);
  assert.equal(h.readerTypewriters.length, 1);                       // 只有第一段打過字

  assert.equal(h.session.nextPage(), 'advanced');
  assert.equal(h.card().text, S2);
  assert.equal(h.session.nextPage(), 'advanced');
  assert.equal(h.card().text, S3);
  assert.equal(h.session.nextPage(), 'end');                         // 最後一段再點擊：不動作
  assert.equal(h.card().text, S3);
  assert.equal(h.session.sendDisabled.value, false);                 // 行動區維持可用
  assert.equal(h.readerTypewriters.length, 3);
});

test('the last released page waits for the rest of the story, and a tail without a closing mark is released by done', async () => {
  const h = await ready();
  const seen = {};
  const plan = {
    events: [
      metaEvent(1),
      event('token', { text: '一句。二句。三句。四' }),
      () => { seen.first = h.card(); h.session.nextPage(); seen.afterNext = h.card(); },
      event('token', { text: '句沒有句號' }),
      () => { seen.before = h.card(); },
      event('state', { updated: false }),
      doneEvent({ coins: 200 }),
    ],
    commit: { view: completeView(1, 'msg-1', '一句。二句。三句。四句沒有句號') },
  };
  await sendAction(h, '行動', plan);
  assert.equal(seen.first.text, '一句。二句。三句。');
  assert.equal(seen.afterNext.text, '一句。二句。三句。');             // 後面的段還沒湊滿：翻頁等待，不是錯誤
  assert.equal(seen.afterNext.waitingText, Labels.storyText('waiting'));
  assert.equal(seen.before.total, 2);                                // 沒有句號的尾端暫不釋出
  assert.equal(h.card().total, 3);                                   // done 之後釋出為最後一段
  h.session.nextPage();
  assert.equal(h.card().text, '四句沒有句號');                       // 內容完整，沒有丟字
});

test('a turn that is typed one page at a time: the first click completes the page being typed and the second turns it', async () => {
  const h = await ready({ readerAutoFinish: false });
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: STORY });
  await sendAction(h, '行動', plan);
  const typewriter = h.readerTypewriters[0];
  assert.equal(h.card().typing, true);                               // 第一段正在逐字顯示
  typewriter.progress('甲說話');
  assert.equal(h.card().text, '甲說話');
  assert.equal(h.card().fullText, S1);                               // 完整文字另外提供（打字中不重複朗讀）
  assert.equal(h.session.nextPage(), 'completed');                   // 第一下：補完這一段，仍停在第一段
  assert.equal(h.card().text, S1);
  assert.equal(h.card().index, 1);
  assert.equal(h.card().typing, false);
  assert.equal(h.session.nextPage(), 'advanced');                    // 第二下才翻頁
  assert.equal(h.card().index, 2);
  assert.equal(h.card().typing, true);
});

test('reduced motion shows every page in full without any typing', async () => {
  const h = createHarness();
  await h.build();
  h.setReducedMotion(true);
  await h.session.load();
  await sendAction(h, '行動', normalTurn({ seq: 1, clientMsgId: 'msg-1', story: STORY }));
  assert.equal(h.readerTypewriters.length, 0);                       // 減少動態：不建立打字機
  assert.equal(h.card().text, S1);
  assert.equal(h.card().typing, false);
});

test('a turn that auto-regenerates after a block drops the first text, shows a one-line transition, then the new pages', async () => {
  const h = await ready();
  const seen = {};
  const plan = {
    events: [metaEvent(1), event('token', { text: '被擋的第一段文字。第二句。第三句。第四句。' }), event('transition', { text: '（場景忽然一轉。）', will_retry: true }),
      () => { seen.transition = h.card(); },
      event('token', { text: '新的第一句。新的第二句。新的第三句。新的第四句。' }), event('state', { updated: false }), doneEvent({ coins: 200 })],
    commit: { view: completeView(1, 'msg-1', '新的第一句。新的第二句。新的第三句。新的第四句。') },
  };
  await sendAction(h, '行動', plan);
  assert.equal(seen.transition.note, '（場景忽然一轉。）');           // 轉場只顯示一行
  assert.equal(seen.transition.waiting, true);                       // 仍在等待重新生成的新內容
  assert.ok(!JSON.stringify(seen.transition).includes('被擋的第一段'));
  assert.equal(h.card().note, '');                                   // 新的第一段釋出後說明消失
  assert.equal(h.card().text, '新的第一句。新的第二句。新的第三句。');
  assert.ok(!JSON.stringify(h.card()).includes('被擋的第一段'));
  assert.equal(h.session.notice.value, null);                        // 轉場不是錯誤
  assert.deepEqual(h.toasts, []);
});

test('a turn blocked twice leaves only the transition line on the card and the input unlocked, with no blocked text anywhere', async () => {
  const h = await ready(withTurns());
  const plan = {
    events: [metaEvent(2), event('token', { text: '被擋A。被擋B。被擋C。被擋D。' }), event('transition', { text: '（轉場一）', will_retry: true }),
      event('token', { text: '被擋E。被擋F。被擋G。被擋H。' }), event('transition', { text: '（轉場二）', will_retry: false }),
      doneEvent({ status: 'blocked', turn_number: null, coins: 230 })],
    commit: { view: { seq: 2, client_msg_id: 'msg-1', turn_number: null, status: 'blocked', action: null, story: null } },
  };
  await sendAction(h, '會被擋的行動', plan);
  const card = h.card();
  assert.equal(card.note, '（轉場二）');
  assert.equal(card.text, '（轉場二）');
  assert.equal(card.key, 'turn:msg-1');
  assert.equal(card.finished, true);
  assert.equal(card.canNext, false);
  assert.equal(card.canPrev, false);
  assert.ok(!/被擋[A-H]/.test(JSON.stringify(card)));
  assert.equal(h.session.sendDisabled.value, false);                 // 輸入解鎖
  assert.equal(h.session.nextPage(), 'none');
  assert.equal(h.session.prevPage(), 'none');
  await h.idle();
  assert.equal(h.card().note, '（轉場二）');                          // 補齊與重讀快照之後，仍是這一行說明（沒有被上一輪的內容換掉）
});

test('an interrupted connection leaves one line on the card and no partial story; the line stays until the next action', async () => {
  const h = await ready(withTurns());
  await sendAction(h, '我的行動', { events: [metaEvent(2), event('token', { text: '還沒說完。的劇情。很長。很長。很長。' })], cut: true });
  const card = h.card();
  assert.match(card.note, /可能已在伺服器完成/);
  assert.ok(!JSON.stringify(card).includes('還沒說完'));
  assert.equal(card.canNext, false);
  await h.idle();
  assert.match(h.card().note, /可能已在伺服器完成/);                  // 補齊之後伺服器沒有這一輪：維持那一行說明
  assert.equal(h.session.inflight.value.client_msg_id, 'msg-1');
});

test('an interruption after the server had already finished is healed: the card shows the server version at its last page', async () => {
  const h = await ready(withTurns());
  const plan = { events: [metaEvent(2), event('token', { text: '半' })], cut: true, commit: { view: completeView(2, 'msg-1', '伺服器已完成。完整的劇情。') } };
  await sendAction(h, '我的行動', plan);
  assert.equal(h.card().key, 'turn:msg-1');
  assert.equal(h.card().note, '');
  assert.equal(h.card().text, '伺服器已完成。完整的劇情。');
  assert.equal(h.card().canNext, false);
});

test('a stream error leaves one line on the card and discards what had arrived', async () => {
  const h = await ready(withTurns());
  const plan = { events: [metaEvent(2), event('token', { text: '一半。的劇情。很長。很長。' }), event('error', { code: 'generation_failed' })] };
  await sendAction(h, '行動', plan);
  assert.equal(h.card().note, Labels.streamErrorMessage('generation_failed'));
  assert.ok(!JSON.stringify(h.card()).includes('一半'));
});

test('cancelling a running turn leaves the explanation line on the card', async () => {
  const h = await ready(withTurns());
  h.server.streamPlans.push({ events: [metaEvent(2), event('token', { text: '半句。半句。半句。半句。' })], hold: true });
  h.session.draft.value = '行動';
  const sending = h.session.send();
  await h.until(() => h.calls.streams.length === 1 && h.card().key === 'turn:msg-1', '串流開始');
  await h.until(() => h.card().total > 1, '第一段釋出');
  assert.equal(h.session.cancel(), true);
  await sending;
  assert.match(h.card().note, /已取消這一輪/);
  assert.ok(!JSON.stringify(h.card()).includes('半句'));
});

test('a refused start (insufficient coins) restores the card to what it showed before sending, and opens the coin window', async () => {
  const h = await ready(withTurns());
  h.session.prevPage();                                              // 玩家正在看第二段
  const before = h.card();
  const plan = { http: { status: 409, body: { ok: false, error: { code: 'insufficient_coins', message: '遊戲幣不足' }, balance: 10, needed: 30 } } };
  await sendAction(h, '我想做的事', plan);
  assert.equal(h.calls.streams.length, 1);
  assert.deepEqual(h.card(), before);                                // 什麼都沒發生：原本的畫面與所在的段
  assert.equal(h.session.draft.value, '我想做的事');                  // 草稿保留
  assert.equal(h.wallet.coinModalOpen.value, true);
  assert.equal(h.session.inflight.value, null);
});

test('resending an in-flight turn replays the same pages as a normal turn', async () => {
  const h = await ready();
  await sendAction(h, '行動A', { events: [metaEvent(1)], cut: true });
  h.server.streamPlans.push({
    events: [metaEvent(1, { replayed: true }), event('token', { text: STORY }), doneEvent({ coins: 230 })],
    commit: { view: completeView(1, 'msg-1', STORY) },
  });
  assert.equal(await h.session.resendInflight(), true);
  assert.equal(h.card().key, 'turn:msg-1');
  assert.equal(h.card().note, '');
  assert.equal(h.card().text, S1);                                   // 重送的單一完整 token 同樣依句分段
  assert.equal(h.card().total, 4);
  assert.equal(h.calls.streams[1].body.client_msg_id, 'msg-1');
});

test('an ended save keeps the last page on the card while the action area is locked', async () => {
  const h = await ready(withTurns({ session: { completed_turns: 1, ended: true, status: 'ended' } }));
  assert.equal(h.card().text, S3);
  assert.equal(h.session.sendDisabled.value, true);
  assert.equal(h.session.suggestDisabled.value, true);
  assert.equal(h.session.advanceDisabled.value, true);
  assert.equal(h.session.advanceHint.value, Labels.actionHint('ended'));
});

// ---- 第 0 幕 ----------------------------------------------------------------------------------------------

test('an opening generated by a stream is read from its first page and the action area opens only at its last page', async () => {
  const seen = {};
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({
    events: [
      event('meta', { kind: 'opening', replayed: false }),
      event('token', { text: '第一句。第二句。第三句。第四' }),
      () => { seen.streaming = { card: h.card(), send: h.session.sendDisabled.value }; },
      event('token', { text: '句。第五句。' }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'ready' }),
    ],
    apply: (server) => { server.session.opening = { state: 'ready', text: '第一句。第二句。第三句。第四句。第五句。' }; },
  });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.phase.value === 'ready' && h.calls.openings.length === 1 && h.card().finished, '開場完成');
  await h.idle();
  assert.equal(seen.streaming.card.key, 'opening');
  assert.equal(seen.streaming.card.text, '第一句。第二句。第三句。');       // 從第一段讀起，沒有「你」的行動頁
  assert.equal(seen.streaming.card.hasAction, false);
  assert.equal(seen.streaming.send, true);                            // 串流中：行動區不可用
  const card = h.card();
  assert.equal(card.index, 0);                                       // 玩家還在第一段：不自動翻頁
  assert.equal(card.text, '第一句。第二句。第三句。');
  assert.equal(h.session.openingNotice.value, null);
  assert.equal(h.session.reading.value, true);                       // 還沒翻到最後一段
  assert.equal(h.session.sendDisabled.value, true);
  assert.equal(h.session.inputDisabled.value, true);
  assert.equal(h.session.blockerText.value, Labels.actionHint('reading'));
  assert.match(h.session.blockerText.value, /最後一段/);
  assert.equal(h.session.suggestDisabled.value, true);
  assert.equal(h.session.advanceDisabled.value, true);
  assert.equal(h.session.advanceHint.value, Labels.actionHint('reading'));
  h.session.draft.value = '太早送出';
  assert.equal(await h.session.send(), false);
  assert.equal(h.calls.streams.length, 0);

  assert.equal(h.session.nextPage(), 'advanced');                    // 翻到最後一段之後才開放
  assert.equal(h.card().text, '第四句。第五句。');
  assert.equal(h.session.reading.value, false);
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal(h.session.inputDisabled.value, false);
  assert.equal(h.session.advanceDisabled.value, false);
  assert.equal(h.session.prevPage(), 'back');                        // 之後回看較早的段，行動區不會再鎖上
  assert.equal(h.card().text, '第一句。第二句。第三句。');
  assert.equal(h.session.reading.value, false);
  assert.equal(h.session.sendDisabled.value, false);
});

test('an opening that ended in the fallback shows the script initial dialogue from its last page, and none of the blocked text', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '被擋的文字。' }),
      event('transition', { text: '（場景一轉）', will_retry: true }), event('token', { text: '又被擋。' }),
      event('transition', { text: '（場景再轉）', will_retry: false }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'fallback' })],       // 後端的第 0 幕串流一律以 complete 結束，由 opening_state 說明是正常或退回
    apply: (server) => { server.session.opening = { state: 'fallback', text: '劇本的初始對話。' }; },
  });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.inputDisabled.value === false, '開場結束');
  await h.idle();
  assert.equal(h.card().key, 'opening');
  assert.equal(h.card().text, '劇本的初始對話。');
  assert.ok(!JSON.stringify(h.card()).includes('被擋的文字'));
  assert.equal(h.session.reading.value, false);
});

test('an opening stream that fails leaves one line on the card instead of a card that waits forever', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({ http: { status: 503, code: 'service_unavailable' } });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.openingNotice.value !== null, '開場失敗的說明');
  await h.idle();
  assert.ok(h.card().note);
  assert.equal(h.card().waiting && !h.card().note, false);
});

// ---- 鍵盤與點擊 -------------------------------------------------------------------------------------------

test('space, Enter and the right arrow turn to the next page, the left arrow goes back, and a handled key does not scroll the page', async () => {
  const h = await ready(withTurns());
  for (let step = 0; step < 3; step += 1) { h.session.prevPage(); }
  assert.equal(h.card().index, 0);
  for (const name of [' ', 'Enter', 'ArrowRight']) {
    const press = keyEvent(name);
    assert.equal(h.session.handleKey(press), true, name);
    assert.equal(press.record.prevented, 1, name);                    // 空白鍵不捲動頁面
  }
  assert.equal(h.card().index, 3);
  const left = keyEvent('ArrowLeft');
  assert.equal(h.session.handleKey(left), true);
  assert.equal(h.card().index, 2);
  assert.equal(left.record.prevented, 1);
  const spacebar = keyEvent('Spacebar');                              // 舊版瀏覽器的名稱
  assert.equal(h.session.handleKey(spacebar), true);
  assert.equal(h.card().index, 3);
});

test('a key does nothing when the focus is on an input, a button or an editable element, or when a window is open', async () => {
  const h = await ready(withTurns());
  h.session.prevPage();
  const before = h.card();
  for (const tag of ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A', 'SUMMARY', 'input', 'textarea']) {
    const press = keyEvent(' ', { target: { tagName: tag } });
    assert.equal(h.session.handleKey(press), false, tag);
    assert.equal(press.record.prevented, 0, tag);                     // 輸入框裡的空白字元照常輸入
  }
  assert.equal(h.session.handleKey(keyEvent('Enter', { target: { tagName: 'DIV', isContentEditable: true } })), false);
  h.setModalOpen(true);
  const blocked = keyEvent('ArrowRight');
  assert.equal(h.session.handleKey(blocked), false);                  // 有開啟的 Modal
  assert.equal(blocked.record.prevented, 0);
  h.setModalOpen(false);
  assert.deepEqual(h.card(), before);                                 // 以上都沒有翻頁
  assert.equal(h.session.handleKey(keyEvent('ArrowRight', { target: { tagName: 'DIV' } })), true);   // 焦點在一般元素：照常翻頁
  assert.equal(h.card().index, 3);
  const last = keyEvent('ArrowRight', { target: null });
  assert.equal(h.session.handleKey(last), true);                       // 已經在最後一段：按鍵仍算處理（不讓空白鍵捲動頁面），畫面不動
  assert.equal(last.record.prevented, 1);
  assert.equal(h.card().index, 3);
});

test('keys with a modifier, held-down repeats, composing text, other keys and not-yet-loaded pages are ignored', async () => {
  const h = createHarness(withTurns());
  await h.build();
  assert.equal(h.session.handleKey(keyEvent(' ')), false);            // 還沒載入：沒有劇情卡
  await h.session.load();
  h.session.prevPage();
  const before = h.card();
  for (const extra of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }, { repeat: true }, { isComposing: true }, { defaultPrevented: true }]) {
    assert.equal(h.session.handleKey(keyEvent('ArrowRight', extra)), false, JSON.stringify(extra));
  }
  for (const name of ['a', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'Home', '1', '']) { assert.equal(h.session.handleKey(keyEvent(name)), false, name); }
  for (const bad of [null, undefined, 'x', 7, {}]) { assert.equal(h.session.handleKey(bad), false); }
  assert.deepEqual(h.card(), before);
  assert.equal(h.session.handleKey(keyEvent('ArrowRight', { shiftKey: true })), true);              // Shift 不算修飾鍵
});

test('keys do nothing while the page is not ready or the card holds only a one-line note', async () => {
  const gone = createHarness();
  gone.server.snapshotFailure = failureInfo(404, 'not_found');
  await gone.build();
  await gone.session.load();
  assert.equal(gone.session.phase.value, 'gone');
  assert.equal(gone.session.handleKey(keyEvent(' ')), false);

  const h = await ready(withTurns());
  await sendAction(h, '行動', { events: [metaEvent(2), event('transition', { text: '（轉場）', will_retry: false }), doneEvent({ status: 'blocked', turn_number: null, coins: 230 })] });
  assert.equal(h.card().note, '（轉場）');
  const press = keyEvent('ArrowLeft');
  assert.equal(h.session.handleKey(press), true);                     // 說明只有一行：按鍵被處理但沒有頁可翻
  assert.equal(h.card().note, '（轉場）');
});

// ---- 直接推進劇情 -----------------------------------------------------------------------------------------

test('the advance button sends the fixed text as a normal action and leaves the draft in the input untouched', async () => {
  const h = await ready(withTurns());
  h.session.draft.value = '我還沒寫完的草稿';
  h.server.streamPlans.push(normalTurn({ seq: 2, clientMsgId: 'msg-1', story: '劇情自然地往下走。又一句。', coins: 170 }));
  assert.equal(await h.session.advance(), true);
  assert.deepEqual(h.calls.streams[0].body, { client_msg_id: 'msg-1', action: State.ADVANCE_ACTION });
  assert.equal(h.calls.streams[0].body.action, '推進劇情');
  assert.equal(h.session.draft.value, '我還沒寫完的草稿');             // 草稿沒有被讀取、清除或送出
  assert.equal(h.card().key, 'turn:msg-1');
  assert.equal(h.wallet.balanceText.value, '170');
  assert.equal(h.timeline().find((item) => item.key === 'turn:msg-1').action, '推進劇情');   // 時間軸以「你：推進劇情」顯示
});

test('the advance button keeps a draft that happens to be the same four characters', async () => {
  const h = await ready(withTurns());
  h.session.draft.value = '推進劇情';
  h.server.streamPlans.push(normalTurn({ seq: 2, clientMsgId: 'msg-1', story: '劇情往前。' }));
  await h.session.advance();
  assert.equal(h.session.draft.value, '推進劇情');
});

test('typing the same four characters by hand is an ordinary action handled the same way', async () => {
  const h = await ready(withTurns());
  h.server.streamPlans.push(normalTurn({ seq: 2, clientMsgId: 'msg-1', story: '劇情往前。' }));
  h.session.draft.value = '推進劇情';
  await h.session.send();
  assert.deepEqual(h.calls.streams[0].body, { client_msg_id: 'msg-1', action: '推進劇情' });
  assert.equal(h.session.draft.value, '');                            // 一般送出：成功後清掉草稿
});

test('the advance button does nothing when an action cannot be sent, and sends no request', async () => {
  const cases = {
    ended: () => ready(withTurns({ session: { completed_turns: 1, ended: true, status: 'ended' } })),
    gate: () => ready(withTurns({ state: { gate: 'age_confirmation_required' } })),
    inflight: () => ready(withTurns({ seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'lost', action: '沒送完', created_at: 'x' }) })),
    offline: async () => { const h = await ready(withTurns()); await h.session.setOnline(false); return h; },
  };
  for (const [name, build] of Object.entries(cases)) {
    const h = await build();
    assert.equal(h.session.advanceDisabled.value, true, name);
    assert.ok(h.session.advanceHint.value, name);                      // 說明原因
    assert.equal(await h.session.advance(), false, name);
    assert.equal(h.calls.streams.length, 0, name);
    assert.equal(h.calls.fetch.length, 0, name);
  }
});

test('the advance button is single-flight: a second press while the turn runs sends no second request', async () => {
  const h = await ready(withTurns());
  h.server.streamPlans.push({ events: [metaEvent(2), event('token', { text: '一句。' })], hold: true });
  const first = h.session.advance();
  await h.until(() => h.calls.streams.length === 1, '串流開始');
  assert.equal(h.session.advanceDisabled.value, true);
  assert.equal(h.session.advanceHint.value, Labels.actionHint('busy'));
  assert.equal(await h.session.advance(), false);
  assert.equal(h.calls.streams.length, 1);
  h.session.cancel();
  await first;
});

test('advancing with too few coins opens the coin window, charges nothing and keeps the card as it was', async () => {
  const h = await ready(withTurns());
  const before = h.card();
  h.server.streamPlans.push({ http: { status: 409, body: { ok: false, error: { code: 'insufficient_coins', message: '遊戲幣不足' }, balance: 10, needed: 30 } } });
  await h.session.advance();
  assert.equal(h.calls.streams.length, 1);
  assert.equal(h.wallet.coinModalOpen.value, true);
  assert.deepEqual(h.card(), before);                                  // 什麼都沒發生：原本的畫面
  assert.equal(h.session.inflight.value, null);
  assert.equal(h.session.sending.value, false);
});

// ---- 行動建議 ---------------------------------------------------------------------------------------------

test('no suggestion is requested by loading, by a whole turn, or by anything but the icon', async () => {
  const h = await ready(withTurns());
  await sendAction(h, '行動', normalTurn({ seq: 2, clientMsgId: 'msg-1', story: '劇情。又一句。' }));
  await h.idle();
  assert.equal(h.calls.suggest.length, 0);
  assert.equal(h.session.suggestOpen.value, false);
  assert.deepEqual(plain(h.session.suggestItems.value), []);
  assert.equal(h.session.suggestBusy.value, false);
  assert.equal(h.session.suggestError.value, '');
});

test('the icon asks once, shows the suggestions, and reopening shows the same ones without asking again', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ suggestions: ['走進旅店', '向路人問路', '先回頭看看'] });
  assert.equal(await h.session.toggleSuggestions(), true);
  assert.equal(h.calls.suggest.length, 1);
  assert.equal(h.calls.suggest[0].id, SESSION_ID);
  assert.equal(h.calls.suggest[0].opts.timeout, 35000);              // 略大於後端單次請求總預算 30 秒
  assert.ok(h.calls.suggest[0].opts.signal);                         // 可取消、離開頁面時中止
  assert.equal(h.session.suggestOpen.value, true);
  assert.deepEqual(plain(h.session.suggestItems.value), ['走進旅店', '向路人問路', '先回頭看看']);
  assert.equal(h.session.suggestBusy.value, false);
  await h.session.toggleSuggestions();                                // 再按：收回
  assert.equal(h.session.suggestOpen.value, false);
  assert.equal(await h.session.toggleSuggestions(), true);            // 再開：重用，沒有新的請求
  assert.equal(h.calls.suggest.length, 1);
  assert.deepEqual(plain(h.session.suggestItems.value), ['走進旅店', '向路人問路', '先回頭看看']);
});

test('the front-end timeout of the suggestion request follows the page state when it provides one', async () => {
  const h = await ready(withTurns({ state: { suggest_timeout_ms: 41000 } }));
  h.server.suggestPlans.push({ suggestions: ['走進旅店'] });
  await h.session.toggleSuggestions();
  assert.equal(h.calls.suggest[0].opts.timeout, 41000);
});

test('suggestions already saved with the save (snapshot) open at once without a request', async () => {
  const h = await ready(withTurns({ session: { completed_turns: 1, suggestions: ['已保存的建議一', '已保存的建議二'] } }));
  assert.equal(h.session.suggestOpen.value, false);                   // 載入時只有圖示
  assert.equal(await h.session.toggleSuggestions(), true);
  assert.equal(h.calls.suggest.length, 0);
  assert.deepEqual(plain(h.session.suggestItems.value), ['已保存的建議一', '已保存的建議二']);
});

test('a suggestion is pasted into the input (replacing the text, no request sent) and the list folds back into the icon', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ suggestions: ['走進旅店', '向路人問路'] });
  await h.session.toggleSuggestions();
  h.session.draft.value = '原本的草稿';
  const tick = h.session.focusTick.value;
  assert.equal(h.session.pickSuggestion('向路人問路'), true);
  assert.equal(h.session.draft.value, '向路人問路');                  // 取代原有內容
  assert.equal(h.session.suggestOpen.value, false);                   // 收回成圖示
  assert.equal(h.session.focusTick.value, tick + 1);                  // 掛載層據此把游標放到輸入框尾端
  assert.equal(h.calls.streams.length, 0);                            // 不直接送出
  assert.equal(h.calls.fetch.length, 0);
  assert.equal(h.session.pickSuggestion('不在清單裡的文字'), false);   // 只認目前顯示的建議
  assert.equal(h.session.pickSuggestion(null), false);
  assert.equal(h.session.draft.value, '向路人問路');
  assert.equal(await h.session.toggleSuggestions(), true);            // 用過之後再開：同一組，沒有新的請求
  assert.equal(h.calls.suggest.length, 1);
});

test('a failed suggestion explains itself in fixed text, keeps the icon usable, saves nothing and can be retried', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ failure: failureInfo(503, 'suggestions_unavailable', { message: 'ZZ模型的原文ZZ' }) });
  assert.equal(await h.session.toggleSuggestions(), false);
  assert.equal(h.session.suggestError.value, '建議暫時無法產生，可以自己輸入行動。');
  assert.ok(!h.session.suggestError.value.includes('ZZ'));
  assert.equal(h.session.suggestOpen.value, false);
  assert.equal(h.session.suggestBusy.value, false);
  assert.equal(h.session.suggestDisabled.value, false);               // 圖示仍可再按
  h.server.suggestPlans.push({ suggestions: ['重試成功的建議'] });
  assert.equal(await h.session.toggleSuggestions(), true);
  assert.equal(h.calls.suggest.length, 2);                            // 失敗不佔次數：再按就是新的請求
  assert.equal(h.session.suggestError.value, '');
  assert.deepEqual(plain(h.session.suggestItems.value), ['重試成功的建議']);
});

test('an empty answer is treated as a failure, not as an empty list', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ suggestions: [] });
  assert.equal(await h.session.toggleSuggestions(), false);
  assert.equal(h.session.suggestOpen.value, false);
  assert.equal(h.session.suggestError.value, Labels.suggestText('unavailable'));
});

test('a stale answer (the story moved on elsewhere) re-reads the save and says the suggestions expired', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ failure: failureInfo(409, 'suggestions_stale') });
  h.server.turns.push(turnView(2, 't2', '另一個裝置的行動', '新的劇情。又一句。'));
  h.server.session.completed_turns = 2;
  const turnReads = h.calls.turns.length;
  await h.session.toggleSuggestions();
  assert.equal(h.session.suggestError.value, Labels.suggestText('stale'));
  assert.ok(h.calls.turns.length > turnReads);                        // 重讀歷史
  assert.equal(h.card().key, 'turn:t2');                              // 劇情卡跟上新的一輪
  assert.equal(h.session.suggestOpen.value, false);
});

test('an ended save, a locked account, an unfinished opening, a running turn and no connection all keep the icon from asking', async () => {
  const cases = {
    ended: () => ready(withTurns({ session: { completed_turns: 1, ended: true, status: 'ended' } })),
    gate: () => ready(withTurns({ state: { gate: 'account_locked' } })),
    inflight: () => ready(withTurns({ seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'lost', action: '沒送完', created_at: 'x' }) })),
    offline: async () => { const h = await ready(withTurns()); await h.session.setOnline(false); return h; },
  };
  for (const [name, build] of Object.entries(cases)) {
    const h = await build();
    assert.equal(h.session.suggestDisabled.value, true, name);
    assert.ok(h.session.suggestHint.value, `${name} 要說明原因`);
    assert.equal(await h.session.toggleSuggestions(), false, name);
    assert.equal(h.calls.suggest.length, 0, name);
  }
  const running = await ready(withTurns());
  running.server.streamPlans.push({ events: [metaEvent(2), event('token', { text: '一句。' })], hold: true });
  const sending = sendAction(running, '行動');
  await running.until(() => running.calls.streams.length === 1, '串流開始');
  assert.equal(running.session.suggestDisabled.value, true);
  assert.equal(await running.session.toggleSuggestions(), false);
  assert.equal(running.calls.suggest.length, 0);
  running.session.cancel();
  await sending;
});

test('a new turn folds the suggestions back and forgets them, so the next screen asks again', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ suggestions: ['第一個畫面的建議'] });
  await h.session.toggleSuggestions();
  assert.equal(h.session.suggestOpen.value, true);
  await sendAction(h, '行動', normalTurn({ seq: 2, clientMsgId: 'msg-1', story: '新的劇情。又一句。' }));
  assert.equal(h.session.suggestOpen.value, false);                   // 新的回合開始：收回
  h.server.suggestPlans.push({ suggestions: ['第二個畫面的建議'] });
  await h.session.toggleSuggestions();
  assert.equal(h.calls.suggest.length, 2);                            // 舊的建議已過期：重新產生
  assert.deepEqual(plain(h.session.suggestItems.value), ['第二個畫面的建議']);
});

test('pressing the icon again while it is loading cancels the request without an error, and the late answer is ignored', async () => {
  const h = await ready(withTurns());
  let release = null;
  h.server.suggestGate = new Promise((resolve) => { release = resolve; });
  h.server.suggestPlans.push({ suggestions: ['太晚才回來'] });
  const asking = h.session.toggleSuggestions();
  await h.until(() => h.calls.suggest.length === 1, '請求送出');
  assert.equal(h.session.suggestBusy.value, true);
  assert.equal(h.session.suggestDisabled.value, false);               // 取得中仍可按（取消）
  assert.equal(h.session.suggestHint.value, Labels.suggestText('loading'));
  assert.equal(await h.session.toggleSuggestions(), false);           // 再按：取消
  assert.equal(h.calls.suggest[0].opts.signal.aborted, true);
  assert.equal(h.session.suggestBusy.value, false);
  assert.equal(await asking, false);
  release();
  await h.idle();
  assert.equal(h.session.suggestOpen.value, false);
  assert.equal(h.session.suggestError.value, '');                     // 取消不是錯誤
  assert.deepEqual(plain(h.session.suggestItems.value), []);
});

test('leaving the page aborts a suggestion that is still loading', async () => {
  const h = await ready(withTurns());
  h.server.suggestGate = new Promise(() => {});
  h.server.suggestPlans.push({ suggestions: ['不會回來'] });
  const asking = h.session.toggleSuggestions();
  await h.until(() => h.calls.suggest.length === 1, '請求送出');
  h.session.dispose();
  assert.equal(h.calls.suggest[0].opts.signal.aborted, true);
  assert.equal(h.session.suggestBusy.value, false);
  assert.equal(await asking, false);
});

test('suggestion text is kept as plain text and never written to the local cache', async () => {
  const h = await ready(withTurns());
  h.server.suggestPlans.push({ suggestions: ['<b>粗體</b>與<script>x</script>', '一般的建議'] });
  await h.session.toggleSuggestions();
  assert.deepEqual(plain(h.session.suggestItems.value), ['<b>粗體</b>與<script>x</script>', '一般的建議']);   // 原樣保留，樣板以 {{ }} 顯示成文字
  const record = await h.store.getSession(SESSION_ID);
  assert.ok(!JSON.stringify(record).includes('一般的建議') && !JSON.stringify(record).includes('suggestion'));
  assert.deepEqual(Object.keys(record).sort(), ['completed_turns', 'content_tier', 'ended', 'id', 'opening', 'title', 'world']);   // 快取的欄位白名單沒有變
  assert.ok(!JSON.stringify(await h.store.turnsOf(SESSION_ID)).includes('一般的建議'));
  assert.equal(await h.store.getInflight(SESSION_ID), null);
});

// ---- 整個流程不留痕跡 ---------------------------------------------------------------------------------------

test('a whole session of reading, turning pages, suggestions, advancing and interruptions writes no player text to the console or browser storage', async () => {
  const trap = trapGlobals();
  const marker = 'ZZPLAYER_STORY_ZZ';
  try {
    const h = createHarness({ turns: [turnView(1, 'old', `${marker}行動`, `${marker}劇情。第二句。第三句。第四句。`)], session: { completed_turns: 1, suggestions: [`${marker}建議`] } });
    await h.build();
    await h.session.load();
    h.session.prevPage();
    h.session.handleKey(keyEvent('ArrowRight'));
    await h.session.toggleSuggestions();
    h.session.pickSuggestion(`${marker}建議`);
    await sendAction(h, `${marker}行動二`, normalTurn({ seq: 2, clientMsgId: 'msg-1', story: `${marker}回應。第二句。第三句。第四句。` }));
    await h.session.toggleSuggestions();
    await sendAction(h, `${marker}被擋`, { events: [metaEvent(3), event('token', { text: `${marker}被擋的文字。` }), event('transition', { text: '（轉場）', will_retry: false }),
      doneEvent({ status: 'blocked', turn_number: null, coins: 170 })], commit: { view: { seq: 3, client_msg_id: 'msg-2', turn_number: null, status: 'blocked', action: null, story: null } } });
    await sendAction(h, `${marker}中斷`, { events: [metaEvent(4), event('token', { text: `${marker}半截。` })], cut: true });
    h.server.suggestPlans.push({ failure: failureInfo(503, 'suggestions_unavailable') });
    await h.idle();
    await h.session.advance();                                       // 在途回合：被擋住，不送出
    h.session.dispose();
    assert.deepEqual(trap.logs, []);
    assert.deepEqual(trap.writes, []);
  } finally {
    trap.restore();
  }
});

// ---- 劇情朗讀（rpg-story-tts：game-session.js 接 story-speech.js）---------------------------------------------------------

const Speech = require('./support/fake-speech.js');

const lastSpeech = (h) => h.session.speech.value;

test('speaking reads the full text of the page on the card, a second press stops it, and the card is never turned by it', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  const before = plain(h.card());
  assert.equal(lastSpeech(h).availability, 'ready');
  assert.equal(h.session.speakCurrent(), 'started');
  assert.deepEqual(speech.synth.speaks, ['speak:' + S3]);                 // 朗讀的是當前段的完整文字
  assert.equal(lastSpeech(h).speaking, true);
  assert.equal(h.session.speakCurrent(), 'stopped');
  assert.equal(lastSpeech(h).speaking, false);
  assert.equal(speech.synth.speaking, false);
  assert.deepEqual(plain(h.card()), before);                              // 朗讀不翻頁、不改劇情卡
});

test('a natural end neither turns the page nor reads on by itself', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  const before = plain(h.card());
  h.session.speakCurrent();
  speech.synth.queue[0].onend();
  assert.equal(lastSpeech(h).speaking, false);
  assert.deepEqual(plain(h.card()), before);
  assert.equal(speech.synth.speaks.length, 1);                            // 沒有自動接讀
});

test('turning a page, going back, or reloading a changed save stops the voice at once', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  h.session.speakCurrent();
  assert.equal(h.session.prevPage(), 'back');
  assert.equal(lastSpeech(h).speaking, false);                            // 回看
  assert.equal(speech.synth.speaking, false);

  h.session.speakCurrent();
  assert.equal(h.session.nextPage(), 'advanced');
  assert.equal(lastSpeech(h).speaking, false);                            // 翻段
  assert.equal(speech.synth.speaking, false);

  h.session.speakCurrent();
  h.server.turns.push(turnView(2, 't2', '另一個裝置的行動', '新的一句。又一句。'));   // 另一個裝置多完成了一輪：載入後劇情卡換成新的一輪
  h.server.session.completed_turns = 2;
  await h.session.load({ soft: true });
  assert.equal(h.card().key, 'turn:t2');
  assert.equal(lastSpeech(h).speaking, false);
  assert.equal(speech.synth.speaking, false);
});

test('the voice keeps going while the page, the turn and the text stay the same', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  h.session.speakCurrent();
  await h.session.load();                                                 // 重讀快照：內容沒變
  assert.equal(lastSpeech(h).speaking, true);
});

test('a card holding only a one-line note, or no content, cannot be read aloud', async () => {
  const speech = Speech.speechEnvironment();
  const gone = createHarness({ speech });
  gone.server.snapshotFailure = failureInfo(404, 'not_found');
  await gone.build();
  await gone.session.load();
  assert.equal(gone.card().visible, false);
  assert.equal(gone.session.speakCurrent(), 'unavailable');               // 沒有任何內容

  const h = await ready(withTurns({ speech }));
  await sendAction(h, '行動', { events: [metaEvent(2), event('transition', { text: '（轉場）', will_retry: false }), doneEvent({ status: 'blocked', turn_number: null, coins: 230 })] });
  assert.equal(h.card().note, '（轉場）');
  assert.equal(h.session.speakCurrent(), 'unavailable');                  // 只有說明行
  assert.deepEqual(speech.synth.speaks, []);
});

test('the first press without a chosen voice opens the voice menu instead of speaking; choosing closes it and the next press speaks', async () => {
  const speech = Speech.speechEnvironment({ stored: null });
  const h = await ready(withTurns({ speech }));
  assert.equal(lastSpeech(h).choice, 'none');
  assert.equal(h.session.speakCurrent(), 'needs-choice');
  assert.equal(h.session.voiceMenuOpen.value, true);
  assert.deepEqual(speech.synth.speaks, []);                              // 還沒選語音：不朗讀
  assert.equal(h.session.chooseVoice({ mode: 'auto' }), true);
  assert.equal(h.session.voiceMenuOpen.value, false);
  assert.equal(lastSpeech(h).choice, 'auto');
  assert.deepEqual(speech.storage.store.writes, [[Speech.SPEECH_STORAGE_KEY, JSON.stringify({ mode: 'auto' })]]);   // 只存語音的選擇
  assert.equal(h.session.speakCurrent(), 'started');
  assert.deepEqual(speech.synth.speaks, ['speak:' + S3]);
});

test('an invalid choice is refused and leaves the menu open', async () => {
  const speech = Speech.speechEnvironment({ stored: null });
  const h = await ready(withTurns({ speech }));
  h.session.openVoiceMenu();
  assert.equal(h.session.chooseVoice({ mode: 'voice', key: '不存在的語音' }), false);
  assert.equal(h.session.voiceMenuOpen.value, true);
  assert.equal(h.session.chooseVoice(null), false);
  assert.equal(h.session.voiceMenuOpen.value, true);
  assert.deepEqual(speech.storage.store.writes, []);
});

test('opening the menu and previewing a voice stop the story voice first, and closing the menu keeps the choice untouched', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  h.session.speakCurrent();
  assert.equal(h.session.openVoiceMenu(), true);
  assert.equal(lastSpeech(h).speaking, false);                            // 開選單先停止劇情朗讀
  assert.equal(speech.synth.speaking, false);
  assert.equal(h.session.voiceMenuOpen.value, true);
  assert.equal(h.session.previewVoice(Speech.ZH_TW_ONLINE.voiceURI), true);
  assert.deepEqual(speech.synth.speaks.slice(-1), ['speak:你好，這是語音試聽。']);   // 試聽只讀固定句子，不讀劇情
  assert.equal(h.session.closeVoiceMenu(), true);
  assert.equal(h.session.voiceMenuOpen.value, false);
  assert.deepEqual(speech.storage.store.writes, []);                      // 試聽與關閉都不改變選擇
});

test('without speech support everything is a quiet no-op and the page works as before', async () => {
  const h = await ready(withTurns());
  assert.equal(lastSpeech(h).availability, 'unsupported');
  assert.equal(h.session.speakCurrent(), 'unavailable');
  assert.equal(h.session.openVoiceMenu(), false);
  assert.equal(h.session.voiceMenuOpen.value, false);
  assert.equal(h.session.chooseVoice({ mode: 'auto' }), false);
  assert.equal(h.session.previewVoice('x'), false);
  assert.doesNotThrow(() => h.session.stopSpeech());
  assert.equal(h.card().text, S3);
});

test('with no Chinese voice the availability says so, the menu refuses to open and nothing is spoken', async () => {
  const speech = Speech.speechEnvironment({ voices: [Speech.voice('Microsoft David', 'en-US')] });
  const h = await ready(withTurns({ speech }));
  assert.equal(lastSpeech(h).availability, 'no-voice');
  assert.equal(h.session.openVoiceMenu(), false);
  assert.notEqual(h.session.speakCurrent(), 'started');
  assert.deepEqual(speech.synth.speaks, []);
});

test('sending an action and advancing stop the voice at once, but a refused click does not', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  h.session.speakCurrent();
  h.session.draft.value = '';
  await h.session.send();                                                 // 空白被擋下：什麼都沒送出，聲音不受影響
  assert.equal(lastSpeech(h).speaking, true);

  h.server.streamPlans.push({ events: [metaEvent(2), event('token', { text: '新的一輪。' }), doneEvent({ turn_number: 2 })], commit: { view: completeView(2, 'msg-1', '新的一輪。') } });
  h.session.draft.value = '往前走';
  const running = h.session.send();
  assert.equal(lastSpeech(h).speaking, false);                            // 送出的當下立即停止
  await running;

  h.session.speakCurrent();
  assert.equal(lastSpeech(h).speaking, true);
  h.server.streamPlans.push({ events: [metaEvent(3), event('token', { text: '又一輪。' }), doneEvent({ turn_number: 3 })], commit: { view: completeView(3, 'msg-2', '又一輪。') } });
  const advancing = h.session.advance();
  assert.equal(lastSpeech(h).speaking, false);                            // 直接推進劇情也一樣
  await advancing;
});

test('resending and abandoning an in-flight turn stop whatever is speaking', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready({ speech });
  await sendAction(h, '行動A', { events: [metaEvent(1)], cut: true });
  assert.ok(h.session.inflight.value);
  h.session.previewVoice(Speech.ZH_TW_LOCAL.voiceURI);                    // 試聽也是朗讀：用它代表「正在出聲」
  assert.equal(speech.synth.speaking, true);
  h.server.streamPlans.push({ events: [metaEvent(1), event('token', { text: '重播。' }), doneEvent({ turn_number: 1 })], commit: { view: completeView(1, 'msg-1', '重播。') } });
  const resending = h.session.resendInflight();
  assert.equal(speech.synth.speaking, false);                             // 重送：停止
  await resending;

  const other = await ready({ speech: Speech.speechEnvironment() });
  await sendAction(other, '行動B', { events: [metaEvent(1)], cut: true });
  assert.ok(other.session.inflight.value);
  other.session.previewVoice(Speech.ZH_TW_LOCAL.voiceURI);
  assert.equal(other.session.speech.value.speaking, true);
  await other.session.abandonInflight();
  assert.equal(other.session.speech.value.speaking, false);               // 放棄：停止
});

test('leaving the page destroys the speaker: the voice stops and the voice-list listener is removed', async () => {
  const speech = Speech.speechEnvironment();
  const h = await ready(withTurns({ speech }));
  h.session.speakCurrent();
  assert.equal(speech.synth.listenerCount, 1);
  h.session.dispose();
  assert.equal(speech.synth.speaking, false);
  assert.equal(speech.synth.listenerCount, 0);
  assert.equal(h.session.speakCurrent(), 'unavailable');                  // 銷毀之後不再朗讀
});

test('speaking writes only the chosen voice, never any story text, to the browser and the console', async () => {
  const trap = trapGlobals();
  try {
    const speech = Speech.speechEnvironment({ stored: null });
    const h = await ready(withTurns({ speech }));
    h.session.speakCurrent();
    h.session.chooseVoice({ mode: 'auto' });
    h.session.speakCurrent();
    h.session.previewVoice(Speech.ZH_TW_LOCAL.voiceURI);
    h.session.dispose();
    assert.deepEqual(trap.logs, []);
    assert.deepEqual(trap.writes, []);                                    // 沒有碰全域的 localStorage、sessionStorage、indexedDB（儲存是注入的）
    for (const [, value] of speech.storage.store.writes) { assert.ok(!value.includes('甲說話了') && !value.includes('庚離開'), '朗讀的文字不得進儲存'); }
  } finally {
    trap.restore();
  }
});
