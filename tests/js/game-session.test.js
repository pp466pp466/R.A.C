'use strict';
/* 遊戲主畫面的狀態與流程（game-session.js）：rpg-game-screen、rpg-stream-client、rpg-game-sync 規格的情境——
 * 載入順序與第 0 幕、時間軸與歷史補齊、行動輸入與送出、串流事件到時間軸（轉場、被擋、中斷、取消、逾時之外的結果）、在途回合、離線、404、閘門、
 * 餘額不足、捲動、渲染視窗，以及「整個流程沒有玩家文字進主控台、沒有瀏覽器儲存的寫入」。
 * 環境見 tests/js/support/game-harness.js：真的 Vue、store、sync、turn-stream 與狀態模組，只有伺服器、打字機與計時器是替身。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  createHarness, trapGlobals, failureInfo, turnView, normalTurn, event, plain, SESSION_ID, Labels,
} = require('./support/game-harness.js');

const ROOT = path.join(__dirname, '..', '..');
const OPENING_TEXT = '第零幕：山門前的石階。';

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

const doneEvent = (extra) => event('done', Object.assign({ status: 'complete', turn_number: 1, coins: 200 }, extra));
const metaEvent = (seq, extra) => event('meta', Object.assign({ turn_id: seq, attempt: 1, replayed: false }, extra));
const completeView = (seq, id, story) => turnView(seq, id, undefined, story);

// ---- 載入順序與第 0 幕 ----------------------------------------------------------------------------------

test('loading reads the snapshot first, then fills history and the wallet, and shows an opening that is ready without calling the opening stream', async () => {
  const h = await ready();
  assert.equal(h.session.phase.value, 'ready');
  assert.deepEqual(h.kinds(), ['opening']);
  assert.equal(h.timeline()[0].text, OPENING_TEXT);
  assert.equal(h.calls.openings.length, 0);                          // 第 0 幕已 ready：不重抽
  assert.equal(h.calls.snapshot.length, 1);
  assert.deepEqual(h.calls.turns.map((call) => call.since), [0]);    // 歷史以 since_seq 補齊
  assert.equal(h.wallet.balanceText.value, '230');                   // 錢包並行載入
  assert.equal(h.session.inputDisabled.value, false);
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal(h.session.world.value.location, '青雲山門');
  assert.deepEqual(plain(h.session.world.value.goals), ['通過入門測試']);
});

test('sending stays disabled until the history and the in-flight record have been checked', async () => {
  const h = createHarness({ seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'lost-1', action: '重新整理前的行動', created_at: 'x' }) });
  let release = null;
  h.server.turnsGate = new Promise((resolve) => { release = resolve; });
  await h.build();
  const loading = h.session.load();
  await h.until(() => h.session.phase.value === 'ready' && h.calls.turns.length === 1, '快照已讀、歷史卡住');
  assert.equal(h.session.sendDisabled.value, true);                  // 歷史還沒補齊：不知道有沒有一筆還沒完成的行動
  h.session.draft.value = '太早送出的行動';
  assert.equal(await h.session.send(), false);
  assert.equal(h.calls.streams.length, 0);
  release();
  await loading;
  assert.equal(h.session.inflight.value.client_msg_id, 'lost-1');
  assert.equal(h.session.sendDisabled.value, true);                  // 確認之後：有一筆在途回合，仍然要先重送或放棄
  assert.match(h.session.blockerText.value, /重送.*放棄/);
});

test('the snapshot is remembered in the local cache without the notebook or the protagonist card', async () => {
  const h = await ready();
  const record = await h.store.getSession(SESSION_ID);
  assert.equal(record.title, '青雲問道');
  assert.equal(record.opening.text, OPENING_TEXT);
  assert.deepEqual(Object.keys(record).sort(), ['completed_turns', 'content_tier', 'ended', 'id', 'opening', 'title', 'world']);   // 只有白名單欄位
  const dump = JSON.stringify(record);
  assert.ok(!dump.includes('notebook') && !dump.includes('林初') && !('protagonist' in record));
});

test('the notebook summary of the snapshot is kept in memory for the page prompts, follows every later snapshot, and is never cached locally', async () => {
  const h = await ready();
  assert.deepEqual(plain(h.session.notebookSummary.value), { version: 0, length: 0, capacity: 2000, full: false });
  h.server.session.notebook = { version: 3, length: 2240, capacity: 2000, full: true };
  await sendAction(h, '翻閱典籍', normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '你翻開了書。' }));
  await h.until(() => h.calls.snapshot.length >= 2 && h.session.notebookSummary.value.version === 3, '完成後的快照帶來新的摘要');
  assert.deepEqual(plain(h.session.notebookSummary.value), { version: 3, length: 2240, capacity: 2000, full: true });
  h.server.session.notebook = { version: 'broken' };                                   // 壞掉的摘要：什麼都不改（不確定就不亂顯示）
  await sendAction(h, '再翻一頁', normalTurn({ seq: 2, clientMsgId: 'msg-2', story: '第二頁。' }));
  await h.until(() => h.calls.snapshot.length >= 3, '第二輪之後再讀一次快照');
  await h.idle();
  assert.equal(h.session.notebookSummary.value.version, 3);
  const record = await h.store.getSession(SESSION_ID);
  assert.ok(!JSON.stringify(record).includes('notebook') && !JSON.stringify(record).includes('2240'));      // 摘要只在記憶體
});

test('a fallback opening (the script initial dialogue) is shown as it is and not generated again', async () => {
  const h = await ready({ session: { opening: { state: 'fallback', text: '劇本的初始對話。' } } });
  assert.equal(h.timeline()[0].text, '劇本的初始對話。');
  assert.equal(h.calls.openings.length, 0);
  assert.equal(h.session.inputDisabled.value, false);
});

test('an opening that is pending is streamed with the typewriter, the input stays locked until done, and done unlocks it', async () => {
  const observed = [];
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({
    events: [
      event('meta', { kind: 'opening', replayed: false }),
      event('token', { text: '山風' }),
      () => observed.push({ disabled: h.session.inputDisabled.value, send: h.session.sendDisabled.value, text: h.timeline().map((item) => item.text) }),
      event('token', { text: '拂過石階。' }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'ready' }),
    ],
    apply: (server) => { server.session.opening = { state: 'ready', text: '山風拂過石階。' }; },
  });
  await h.build();
  await h.session.load();
  assert.equal(h.session.inputDisabled.value, true);                  // 開場一開始就禁用輸入（串流還沒結束）
  await h.until(() => h.session.inputDisabled.value === false, '開場完成後解鎖');
  assert.equal(observed[0].disabled, true);                           // 串流中途：輸入禁用
  assert.equal(observed[0].send, true);
  assert.deepEqual(observed[0].text, ['山風']);                        // 逐字顯示：中途只有已推進的片段
  assert.equal(h.calls.openings.length, 1);
  assert.equal(h.calls.openings[0].body, undefined);                  // 開場沒有本文
  assert.equal(h.calls.openings[0].headers['X-CSRF-Token'], 'CSRF-TOKEN');
  assert.equal(h.timeline()[0].text, '山風拂過石階。');
  assert.equal(h.wallet.balanceText.value, '230');                    // 第 0 幕不扣幣：餘額沒變
  assert.equal(h.session.openingNotice.value, null);
});

test('after an opening stream the text comes from the server version once the typewriter has finished (no cut-off, no flicker)', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } }, autoFinish: false });
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '串流的' }), event('token', { text: '文字' }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'ready' })],
    apply: (server) => { server.session.opening = { state: 'ready', text: '後端的完整文字' }; },
  });
  await h.build();
  await h.session.load();
  await h.until(() => h.calls.snapshot.length >= 2, '開場完成後重讀快照');
  await h.idle();
  assert.equal(h.timeline()[0].text, '串流的文字');                    // 打字機還沒打完：先不換
  h.typewriters[0].complete();
  assert.equal(h.timeline()[0].text, '後端的完整文字');                // 打完才換成後端的版本
});

test('an opening that ends in the fallback takes its text from the server after done', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '被擋的文字' }),
      event('transition', { text: '（場景一轉）', will_retry: true }), event('token', { text: '又被擋' }),
      event('transition', { text: '（場景再轉）', will_retry: false }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'fallback' })],
    apply: (server) => { server.session.opening = { state: 'fallback', text: '劇本的初始對話。' }; },
  });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.inputDisabled.value === false);
  await h.idle();
  assert.equal(h.timeline()[0].text, '劇本的初始對話。');
  assert.ok(!JSON.stringify(h.timeline()).includes('被擋的文字'));
});

test('another device generating the opening shows a waiting note, re-reads the snapshot every 5 seconds, and stops once it is ready', async () => {
  const h = createHarness({ session: { opening: { state: 'generating', text: null } } });
  h.server.openingPlans.push({ http: { status: 409, code: 'opening_in_progress', message: '另一個裝置正在準備故事的開場' } });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.openingNotice.value !== null, '等待說明');
  assert.match(h.session.openingNotice.value.text, /另一個裝置正在準備/);
  assert.equal(h.session.openingNotice.value.tone, 'info');           // 不是錯誤
  assert.equal(h.session.openingNotice.value.retry, false);
  assert.equal(h.session.notice.value, null);
  assert.deepEqual(h.timers.pending(), [5000]);
  assert.equal(h.session.inputDisabled.value, true);
  h.timers.fire();
  await h.until(() => h.calls.snapshot.length === 2);
  await h.idle();
  assert.deepEqual(h.timers.pending(), [5000]);                       // 還在生成：繼續等
  h.server.session.opening = { state: 'ready', text: '另一個裝置準備好的開場。' };
  h.timers.fire();
  await h.until(() => h.session.inputDisabled.value === false, '開場準備好');
  assert.equal(h.timeline()[0].text, '另一個裝置準備好的開場。');
  assert.equal(h.session.openingNotice.value, null);
  assert.deepEqual(h.timers.pending(), []);
});

test('the opening wait gives up after 24 re-reads and offers a retry button', async () => {
  const h = createHarness({ session: { opening: { state: 'generating', text: null } } });
  h.server.openingPlans.push({ http: { status: 409, code: 'opening_in_progress' } });
  await h.build();
  await h.session.load();
  await h.until(() => h.timers.pending().length === 1);
  for (let round = 1; round <= 24; round += 1) {
    h.timers.fire();
    await h.until(() => h.calls.snapshot.length === 1 + round, `第 ${round} 次重讀`);
    await h.idle(3);
  }
  assert.deepEqual(h.timers.pending(), []);                           // 約 2 分鐘後不再輪詢
  assert.equal(h.session.openingNotice.value.retry, true);
  assert.equal(h.session.inputDisabled.value, true);
  // 重試：這回開場串流成功
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '終於開場。' }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'ready' })],
    apply: (server) => { server.session.opening = { state: 'ready', text: '終於開場。' }; },
  });
  await h.session.retryOpening();
  await h.until(() => h.session.inputDisabled.value === false);
  assert.equal(h.calls.openings.length, 2);
});

test('an opening whose waiter finds it pending again takes over and generates it itself', async () => {
  const h = createHarness({ session: { opening: { state: 'generating', text: null } } });
  h.server.openingPlans.push({ http: { status: 409, code: 'opening_in_progress' } });
  await h.build();
  await h.session.load();
  await h.until(() => h.timers.pending().length === 1);
  h.server.session.opening = { state: 'pending', text: null };        // 另一個裝置放棄了
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '換我來。' }),
      event('done', { status: 'complete', turn_number: null, coins: 230, opening_state: 'ready' })],
    apply: (server) => { server.session.opening = { state: 'ready', text: '換我來。' }; },
  });
  h.timers.fire();
  await h.until(() => h.session.inputDisabled.value === false, '接手後完成');
  assert.equal(h.calls.openings.length, 2);
});

test('an opening that fails shows a Traditional Chinese error with a retry button and keeps the input locked', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({ events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '半' }), event('error', { code: 'generation_failed', message: 'x' })] });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.openingNotice.value !== null, '失敗說明');
  assert.equal(h.session.openingNotice.value.tone, 'error');
  assert.equal(h.session.openingNotice.value.retry, true);
  assert.match(h.session.openingNotice.value.text, /故事暫時無法生成/);
  assert.equal(h.session.inputDisabled.value, true);
  assert.equal(h.timeline().length, 0);                               // 半截的文字被丟棄
});

test('an opening that the server finished anyway after the connection broke is picked up from the snapshot, and the failure note goes away', async () => {
  const h = createHarness({ session: { opening: { state: 'pending', text: null } } });
  h.server.openingPlans.push({
    events: [event('meta', { kind: 'opening', replayed: false }), event('token', { text: '半' })], cut: true,
    apply: (server) => { server.session.opening = { state: 'ready', text: '伺服器已經完成的開場。' }; },
  });
  await h.build();
  await h.session.load();
  await h.until(() => h.session.inputDisabled.value === false, '重讀快照後取得開場');
  assert.equal(h.timeline()[0].text, '伺服器已經完成的開場。');
  assert.equal(h.session.openingNotice.value, null);
});

// ---- 時間軸與歷史 ---------------------------------------------------------------------------------------

test('25 complete turns load in order with increasing sequence numbers, across pages', async () => {
  const turns = Array.from({ length: 25 }, (_, index) => turnView(index + 1, `m${index + 1}`, `行動${index + 1}`, `劇情${index + 1}`));
  const h = await ready({ turns, pageSize: 10 });
  assert.deepEqual(h.calls.turns.map((call) => call.since), [0, 10, 20]);   // has_more 為真就繼續讀
  const items = h.timeline();
  assert.equal(items[0].kind, 'opening');
  assert.equal(items.length, 26);
  assert.deepEqual(items.slice(1).map((item) => item.seq), Array.from({ length: 25 }, (_, index) => index + 1));
  assert.equal(items[25].action, '行動25');
  assert.equal(items[25].story, '劇情25');
});

test('a blocked or interrupted turn in the history is one line and never any text', async () => {
  const turns = [
    turnView(1, 'a', '行動A', '劇情A'),
    { seq: 2, client_msg_id: 'b', turn_number: null, status: 'blocked', action: '不該出現的行動', story: '不該出現的被擋文字' },
    { seq: 3, client_msg_id: 'c', turn_number: null, status: 'interrupted', action: '不該出現的行動2', story: '不該出現的半截文字' },
    { seq: 4, client_msg_id: 'd', turn_number: null, status: 'streaming', action: null, story: null },
  ];
  const h = await ready({ turns });
  const notes = h.timeline().filter((item) => item.kind === 'note');
  assert.deepEqual(notes.map((item) => item.text), [Labels.historyLine('blocked'), Labels.historyLine('interrupted'), Labels.historyLine('streaming')]);
  assert.ok(!JSON.stringify(h.timeline()).includes('不該出現'));
});

test('story and action text with markup characters is carried as the original characters, never converted', async () => {
  const story = '<b>粗體</b><script>alert(1)</script><img onerror=x>';
  const h = await ready({ turns: [turnView(1, 'a', '<i>斜體行動</i>', story)] });
  const turn = h.timeline()[1];
  assert.equal(turn.story, story);
  assert.equal(turn.action, '<i>斜體行動</i>');
});

test('only the latest 60 items are rendered; the earlier ones are one click away', async () => {
  const turns = Array.from({ length: 100 }, (_, index) => turnView(index + 1, `m${index + 1}`, `行動${index + 1}`, `劇情${index + 1}`));
  const h = await ready({ turns });
  assert.equal(h.session.items.value.length, 60);
  assert.equal(h.session.hasEarlier.value, true);
  assert.equal(h.timeline()[59].seq, 100);                            // 最後一項是最新的回合
  assert.equal(h.timeline()[0].seq, 41);
  h.session.showEarlier();
  assert.equal(h.session.items.value.length, 101);                    // 第 0 幕加 100 輪，全部展開
  assert.equal(h.session.hasEarlier.value, false);
});

// ---- 送出一輪 -------------------------------------------------------------------------------------------

test('a normal turn: the action appears, the story streams in, done refreshes the balance without a wallet request, the input is cleared and restored', async () => {
  const observed = [];
  const h = await ready();
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '你推開沉重的木門。', tokens: ['你推開', '沉重的木門。'], coins: 200 });
  plan.events.splice(2, 0, () => observed.push({ items: h.timeline(), sending: h.session.sending.value, sendDisabled: h.session.sendDisabled.value }));
  assert.equal(await sendAction(h, '  我走進藏經閣  ', plan), true);
  assert.equal(h.calls.streams.length, 1);
  assert.deepEqual(h.calls.streams[0].body, { client_msg_id: 'msg-1', action: '我走進藏經閣' });      // 頭尾空白去掉，新的 client_msg_id
  assert.equal(h.calls.streams[0].headers['X-CSRF-Token'], 'CSRF-TOKEN');
  assert.equal(h.calls.streams[0].credentials, 'same-origin');
  assert.match(h.calls.streams[0].url, /\/api\/game\/sessions\/[^/]+\/turns\/stream$/);
  // 串流中途：玩家行動已在時間軸，AI 回應正在逐字出現，送出被禁用
  assert.equal(observed[0].items[1].action, '我走進藏經閣');
  assert.equal(observed[0].items[1].story, '你推開');
  assert.equal(observed[0].items[1].streaming, true);
  assert.equal(observed[0].sending, true);
  assert.equal(observed[0].sendDisabled, true);
  // 結束後
  assert.deepEqual(h.kinds(), ['opening', 'turn']);
  assert.equal(h.timeline()[1].story, '你推開沉重的木門。');
  assert.equal(h.timeline()[1].streaming, false);
  assert.equal(h.session.draft.value, '');
  assert.equal(h.session.sending.value, false);
  assert.equal(h.session.inflight.value, null);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.wallet.balanceText.value, '200');
  assert.equal(h.calls.wallet.length, 1);                              // 只有載入時的一次，done.coins 直接覆寫
  assert.equal(h.session.world.value.location, '藏經閣');
  assert.equal((await h.store.turnsOf(SESSION_ID)).length, 1);        // 補齊後寫入本機快取
  assert.equal(h.session.sendDisabled.value, false);
});

test('the in-flight turn is stored before the stream starts and removed when done arrives', async () => {
  const h = await ready();
  let during = null;
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '劇情' });
  plan.events.splice(1, 0, async () => { during = await h.store.getInflight(SESSION_ID); });
  await sendAction(h, '行動內容', plan);
  assert.equal(during.client_msg_id, 'msg-1');
  assert.equal(during.action, '行動內容');
  assert.equal(during.created_at, '2026-11-11T12:00:00.000Z');
  assert.equal(await h.store.getInflight(SESSION_ID), null);
});

test('an empty action and an over-long action are stopped in the browser: no request, and a plain explanation', async () => {
  const h = await ready();
  h.session.draft.value = '   \n  ';
  assert.equal(await h.session.send(), false);
  assert.equal(h.session.actionError.value, '請輸入你的行動。');
  assert.equal(h.calls.streams.length, 0);
  h.session.draft.value = '字'.repeat(1001);
  assert.equal(h.session.draftOver.value, true);
  assert.equal(await h.session.send(), false);
  assert.match(h.session.actionError.value, /最多 1000 字（目前 1001 字）/);
  assert.equal(h.calls.streams.length, 0);
  h.session.draft.value = '字'.repeat(1000);                           // 剛好上限可以送出
  assert.equal(h.session.draftOver.value, false);
  assert.equal(h.session.draftCount.value, 1000);
});

test('pressing send twice sends exactly one request', async () => {
  const h = await ready();
  h.server.streamPlans.push(normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '劇情' }));
  h.session.draft.value = '行動';
  const first = h.session.send();
  const second = h.session.send();
  assert.equal(h.session.sending.value, true);
  assert.equal(await second, false);
  await first;
  assert.equal(h.calls.streams.length, 1);
});

test('a failed turn keeps the action text, restores the button, shows a Traditional Chinese error, and removes the in-flight record', async () => {
  const h = await ready();
  const plan = { events: [metaEvent(1), event('token', { text: '半截' }), event('error', { code: 'generation_failed', message: 'raw' })] };
  assert.equal(await sendAction(h, '我要保留的行動', plan), true);
  assert.equal(h.session.draft.value, '我要保留的行動');
  assert.equal(h.session.sending.value, false);
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal(h.session.notice.value.tone, 'error');
  assert.match(h.session.notice.value.text, /故事暫時無法生成/);
  assert.ok(!h.session.notice.value.text.includes('raw'));            // 不回顯伺服器的文字
  assert.deepEqual(h.kinds(), ['opening']);                           // 半截的文字被丟棄，沒有新項目
  assert.equal(h.session.inflight.value, null);
  assert.equal(h.calls.streams.length, 1);                            // 不自動重送
});

test('a connection that ends before done is an interruption: the text is kept, the explanation mentions the server may have finished, and the in-flight card offers resend or abandon', async () => {
  const h = await ready();
  const plan = { events: [metaEvent(1), event('token', { text: '還沒說完的劇情' })], cut: true };
  await sendAction(h, '我的行動', plan);
  assert.equal(h.session.draft.value, '我的行動');
  assert.match(h.session.notice.value.text, /可能已在伺服器完成/);
  assert.ok(!JSON.stringify(h.timeline()).includes('還沒說完'));      // 不把片段當成完整回應
  assert.deepEqual(h.kinds(), ['opening', 'inflight']);
  assert.equal(h.timeline()[1].action, '我的行動');
  assert.equal(h.session.inflight.value.client_msg_id, 'msg-1');
  assert.ok(await h.store.getInflight(SESSION_ID));                    // 仍存著，重新整理後還在
  assert.equal(h.calls.streams.length, 1);                             // 沒有自動重送
  assert.equal(h.session.sendDisabled.value, true);                    // 只有一筆在途回合：新的行動被禁用
  assert.match(h.session.blockerText.value, /重送.*放棄/);
  assert.equal(h.calls.turns.length, 2);                               // 載入一次、中斷後補齊一次
});

test('an interruption after the server had already finished is healed by the catch-up: the turn appears and nothing is offered for resend', async () => {
  const h = await ready();
  const plan = Object.assign({ events: [metaEvent(1), event('token', { text: '半' })], cut: true }, { commit: { view: completeView(1, 'msg-1', '伺服器已完成的完整劇情') } });
  await sendAction(h, '我的行動', plan);
  assert.equal(h.session.inflight.value, null);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.deepEqual(h.kinds(), ['opening', 'turn']);
  assert.equal(h.timeline()[1].story, '伺服器已完成的完整劇情');
});

test('resending an in-flight turn uses the same message id and replays without charging again', async () => {
  const h = await ready();
  await sendAction(h, '行動A', { events: [metaEvent(1)], cut: true });
  assert.equal(h.session.inflight.value.client_msg_id, 'msg-1');
  // 後端其實已完成；重送得到 replayed 的重播
  h.server.streamPlans.push({
    events: [metaEvent(1, { replayed: true }), event('token', { text: '完整的重播劇情' }), doneEvent({ coins: 230 })],
    commit: { view: completeView(1, 'msg-1', '完整的重播劇情') },
  });
  assert.equal(await h.session.resendInflight(), true);
  assert.equal(h.calls.streams.length, 2);
  assert.equal(h.calls.streams[1].body.client_msg_id, 'msg-1');
  assert.equal(h.calls.streams[1].body.action, '行動A');
  assert.equal(h.wallet.balanceText.value, '230');                     // 餘額不再減少
  assert.equal(h.session.inflight.value, null);
  assert.equal(h.timeline()[1].story, '完整的重播劇情');
  assert.equal(h.session.draft.value, '');                             // 草稿與送出的行動相同：成功後清掉
});

test('abandoning an in-flight turn removes the prompt and the stored record and sends nothing', async () => {
  const h = await ready();
  await sendAction(h, '行動A', { events: [metaEvent(1)], cut: true });
  assert.equal(await h.session.abandonInflight(), true);
  assert.equal(h.session.inflight.value, null);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.calls.streams.length, 1);
  assert.equal(h.session.sendDisabled.value, false);
  assert.deepEqual(h.kinds(), ['opening']);
});

test('after a page reload the catch-up removes an in-flight turn the server already finished, and no resend is offered', async () => {
  const h = await ready({
    turns: [turnView(1, 'before-reload', '重新整理前的行動', '伺服器已完成的劇情')],
    seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'before-reload', action: '重新整理前的行動', created_at: 'x' }),
  });
  assert.equal(h.session.inflight.value, null);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.deepEqual(h.kinds(), ['opening', 'turn']);
  assert.equal(h.calls.streams.length, 0);
});

test('after a page reload an in-flight turn the server does not have is shown with resend and abandon, and nothing is sent by itself', async () => {
  const h = await ready({ seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'lost-1', action: '重新整理前的行動', created_at: 'x' }) });
  assert.equal(h.session.inflight.value.client_msg_id, 'lost-1');
  assert.deepEqual(h.kinds(), ['opening', 'inflight']);
  assert.equal(h.timeline()[1].action, '重新整理前的行動');
  assert.equal(h.calls.streams.length, 0);
  assert.equal(h.session.sendDisabled.value, true);
});

test('a turn that auto-regenerates after a block: the first text is dropped, a transition line is shown, and the new complete text stays', async () => {
  const h = await ready();
  const plan = {
    events: [metaEvent(1), event('token', { text: '被擋的第一段文字' }), event('transition', { text: '（場景忽然一轉。）', will_retry: true }),
      event('token', { text: '重新生成的新劇情' }), event('state', { updated: false }), doneEvent({ coins: 200 })],
    commit: { view: completeView(1, 'msg-1', '重新生成的新劇情') },
  };
  await sendAction(h, '行動', plan);
  const turn = h.timeline()[1];
  assert.equal(turn.story, '重新生成的新劇情');
  assert.deepEqual(turn.notes, ['（場景忽然一轉。）']);                  // 轉場文字在行動與劇情之間
  assert.ok(!JSON.stringify(h.timeline()).includes('被擋的第一段'));
  assert.equal(h.typewriters.filter((typewriter) => typewriter.cancelled).length, 1);       // 每個氣泡一個打字機：第一個被丟棄
  assert.equal(h.typewriters.length, 2);
  assert.equal(h.session.notice.value, null);                         // 轉場不是錯誤：沒有錯誤色彩的提示
  assert.deepEqual(h.toasts, []);
});

test('a turn blocked twice shows the transition line, unlocks the input with a hint to try another action, and leaves no story', async () => {
  const h = await ready();
  const plan = {
    events: [metaEvent(1), event('token', { text: '被擋A' }), event('transition', { text: '（轉場一）', will_retry: true }), event('token', { text: '被擋B' }),
      event('transition', { text: '（轉場二）', will_retry: false }), doneEvent({ status: 'blocked', turn_number: null, coins: 230 })],
    commit: { view: { seq: 1, client_msg_id: 'msg-1', turn_number: null, status: 'blocked', action: null, story: null } },
  };
  await sendAction(h, '會被擋的行動', plan);
  assert.deepEqual(h.kinds(), ['opening', 'note']);
  assert.equal(h.timeline()[1].text, '（轉場二）');
  assert.equal(h.timeline()[1].tone, 'transition');
  assert.ok(!JSON.stringify(h.timeline()).includes('被擋A') && !JSON.stringify(h.timeline()).includes('被擋B'));
  assert.equal(h.session.notice.value.tone, 'info');
  assert.equal(h.session.notice.value.text, Labels.blockedHint());
  assert.equal(h.session.draft.value, '會被擋的行動');                   // 保留，可以修改後再試
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal(h.session.inflight.value, null);
  assert.equal(h.wallet.balanceText.value, '230');                     // 沒有成功生成：沒有扣款
});

test('an ending turn marks the save as ended and locks the input', async () => {
  const h = await ready();
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '故事終於落幕。', doneExtra: { status: 'ended' } });
  const apply = plan.apply;
  plan.apply = (server) => { apply(server); server.session.ended = true; server.session.status = 'ended'; };
  await sendAction(h, '最後的行動', plan);
  assert.equal(h.session.ended.value, true);
  assert.equal(h.session.inputDisabled.value, true);
  assert.equal(h.session.sendDisabled.value, true);
  assert.match(h.session.blockerText.value, /已完結/);
  assert.equal(h.timeline()[1].story, '故事終於落幕。');
});

test('a state event updates the world panel from the whitelist and an age correction is announced with the previous age', async () => {
  const seen = [];
  const h = await ready();
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '劇情' });
  plan.events[2] = event('state', {
    updated: true, elapsed_months: 3, location: '後山', summary: '修煉', goals: ['突破'],
    notices: [{ kind: 'age_corrected', name: '師姐', before: 17, after: 19, text: '伺服器寫的文字不採用' }], hidden_arc: 'cracking', traits: ['冷淡'],
  });
  plan.events.splice(3, 0, () => seen.push(plain(h.session.world.value)));
  plan.apply = () => {};                                              // 伺服器快照沒有變：中途觀察到的是 state 事件的內容
  await sendAction(h, '行動', plan);
  assert.equal(seen[0].location, '後山');
  assert.equal(seen[0].elapsed_months, 3);
  assert.deepEqual(seen[0].goals, ['突破']);
  assert.ok(!('hidden_arc' in seen[0]) && !('traits' in seen[0]));
  assert.equal(h.toasts.length, 1);
  assert.match(h.toasts[0], /師姐的年齡由 17 歲更正為 19 歲/);
  assert.ok(!h.toasts[0].includes('伺服器寫的文字'));
  assert.deepEqual(Object.keys(plain(h.session.world.value)).sort(), ['elapsed_months', 'goals', 'location', 'places', 'protagonist_age', 'summary']);
});

test('the input unlocks at done, before the catch-up finishes, and the next turn waits for that catch-up', async () => {
  const h = await ready();
  let release = null;
  h.server.turnsGate = new Promise((resolve) => { release = resolve; });
  h.server.streamPlans.push(normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '第一輪' }));
  h.session.draft.value = '第一個行動';
  const first = h.session.send();
  await h.until(() => h.session.sending.value === false && h.calls.turns.length >= 2, '補齊卡住時輸入已解鎖');
  assert.equal(h.session.sendDisabled.value, false);                   // done 時就解鎖，不等補齊
  assert.equal(h.session.inflight.value, null);                        // 在途回合在 done 時就移除（補齊卡住時重新整理，也不會誤以為這一輪沒完成）
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  h.server.streamPlans.push(normalTurn({ seq: 2, clientMsgId: 'msg-2', story: '第二輪' }));
  h.session.draft.value = '第二個行動';
  const second = h.session.send();
  assert.equal(h.session.sending.value, true);
  await h.idle();
  assert.equal(h.calls.streams.length, 1);                             // 第二輪等第一輪的補齊做完才開始
  release();
  await first;
  await second;
  assert.equal(h.calls.streams.length, 2);
  assert.equal(h.calls.streams[1].body.client_msg_id, 'msg-2');
  assert.deepEqual(h.timeline().filter((item) => item.kind === 'turn').map((item) => item.story), ['第一輪', '第二輪']);
});

test('when the catch-up after done fails (the network drops right then) the finished turn stays on screen from what was streamed', async () => {
  const h = await ready();
  h.server.turnsFailure = failureInfo(0, '', { kind: 'network' });
  h.server.snapshotFailure = failureInfo(0, '', { kind: 'network' });
  await sendAction(h, '行動', normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '串流完成的劇情' }));
  assert.deepEqual(h.kinds(), ['opening', 'turn']);
  assert.equal(h.timeline()[1].story, '串流完成的劇情');             // 後端還沒補齊，但這一輪已經 done：不能讓它從畫面上消失
  assert.equal(h.session.offline.value, true);                        // 補齊失敗：標示離線，恢復連線後會重新載入
  assert.equal(h.session.sending.value, false);
  assert.equal(h.session.inflight.value, null);
  h.server.turnsFailure = null;
  h.server.snapshotFailure = null;
  h.setOnline(true);
  await h.session.setOnline(true);
  assert.equal(h.session.offline.value, false);
  assert.equal(h.timeline()[1].story, '串流完成的劇情');
  assert.equal((await h.store.turnsOf(SESSION_ID)).length, 1);        // 重新載入後以後端補齊並寫入快取
});

test('the server version replaces the streamed text after the catch-up, and a stale local copy does not survive', async () => {
  const h = await ready();
  await sendAction(h, '行動', normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '串流收到的文字' }));
  assert.equal(h.timeline()[1].story, '串流收到的文字');
  h.server.turns[0].story = '後端的最終文字';                           // 後端為準
  await h.session.load({ soft: true });
  assert.equal(h.timeline()[1].story, '串流收到的文字');               // 已定稿的本機快取不會被同序號以外的更新覆蓋（since_seq 只往後讀）
  const cached = await h.store.turnsOf(SESSION_ID);
  assert.equal(cached[0].story, '串流收到的文字');
});

// ---- 開始前的失敗 ---------------------------------------------------------------------------------------

test('insufficient coins opens the coins modal, keeps the action, adds nothing to the timeline, and leaves no in-flight record', async () => {
  const h = await ready();
  const plan = { http: { status: 409, body: { ok: false, error: { code: 'insufficient_coins', message: '遊戲幣不足' }, balance: 10, needed: 30 } } };
  await sendAction(h, '我想送出的行動', plan);
  assert.equal(h.wallet.coinModalOpen.value, true);
  assert.match(h.wallet.coinModalText.value, /目前餘額 10 幣/);
  assert.match(h.wallet.coinModalText.value, /沒有扣除遊戲幣/);
  assert.equal(h.session.draft.value, '我想送出的行動');
  assert.deepEqual(h.kinds(), ['opening']);
  assert.equal(h.session.inflight.value, null);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.session.sending.value, false);
  assert.equal(h.calls.turns.length, 1);                               // 還沒開始：不需要補齊
  // 簽到成功不會自動重送
  await h.wallet.claimCheckin();
  assert.equal(h.calls.streams.length, 1);
  assert.equal(h.session.draft.value, '我想送出的行動');
});

test('another turn in progress is explained and the catch-up runs; the action is kept and nothing is charged', async () => {
  const h = await ready();
  await sendAction(h, '行動', { http: { status: 409, code: 'turn_in_progress', message: '另一個回合進行中' } });
  assert.match(h.session.notice.value.text, /另一個回合還在進行/);
  assert.match(h.session.notice.value.text, /沒有扣除遊戲幣/);
  assert.equal(h.session.notice.value.tone, 'info');
  assert.equal(h.session.draft.value, '行動');
  assert.equal(h.calls.turns.length, 2);
  assert.equal(h.session.inflight.value, null);
});

test('a 401 sends the player to the login page', async () => {
  const h = await ready();
  await sendAction(h, '行動', { http: { status: 401, code: 'unauthorized' } });
  assert.ok(h.logins.includes('session'));
  assert.equal(h.session.draft.value, '行動');
});

test('a locked account is announced through the restriction notice and the input is locked', async () => {
  const h = await ready();
  await sendAction(h, '行動', { http: { status: 403, code: 'account_locked', message: 'x' } });
  assert.equal(h.session.restriction.value, 'locked');
  assert.equal(h.session.inputDisabled.value, true);
  assert.ok(h.session.gateNotice.value);
});

test('a save that turned out to be ended on the server is marked ended', async () => {
  const h = await ready();
  await sendAction(h, '行動', { http: { status: 409, code: 'session_ended', message: 'x' } });
  assert.equal(h.session.ended.value, true);
  assert.equal(h.session.inputDisabled.value, true);
});

test('field errors from the server are shown next to the action and the text is kept', async () => {
  const h = await ready();
  await sendAction(h, '行動', { http: { status: 400, body: { ok: false, error: { code: 'validation_error', message: 'x', fields: { action: '行動最多 1000 字。' } } } } });
  assert.equal(h.session.actionError.value, '行動最多 1000 字。');
  assert.equal(h.session.draft.value, '行動');
});

// ---- 取消與離開頁面 -------------------------------------------------------------------------------------

test('cancelling aborts the request, is not shown as an error, catches up, and keeps the in-flight prompt when the server has no result', async () => {
  const h = await ready();
  h.server.streamPlans.push({ events: [metaEvent(1), event('token', { text: 'abc' })], hold: true });
  h.session.draft.value = '行動';
  const running = h.session.send();
  await h.until(() => h.typewriters.some((typewriter) => typewriter.raw === 'abc'), '收到第一段');
  assert.equal(h.session.cancel(), true);
  await running;
  assert.equal(h.calls.streams[0].signal.aborted, true);
  assert.equal(h.session.sending.value, false);
  assert.equal(h.session.notice.value.tone, 'info');                   // 取消不是錯誤
  assert.equal(h.session.notice.value.text, Labels.streamOutcomeNotice('aborted'));
  assert.equal(h.calls.turns.length, 2);                               // 前端中止不代表後端中止：補齊
  assert.ok(h.session.inflight.value);
  assert.equal(h.session.draft.value, '行動');
  assert.ok(!JSON.stringify(h.timeline()).includes('abc'));
});

test('leaving the page (pagehide) aborts a running turn and a running opening', async () => {
  const turn = await ready();
  turn.server.streamPlans.push({ events: [metaEvent(1)], hold: true });
  turn.session.draft.value = '行動';
  const running = turn.session.send();
  await turn.until(() => turn.calls.streams.length === 1 && turn.typewriters.length >= 1);
  turn.session.dispose();
  await running;
  assert.equal(turn.calls.streams[0].signal.aborted, true);
  assert.equal(turn.session.sending.value, false);

  const opening = createHarness({ session: { opening: { state: 'pending', text: null } } });
  opening.server.openingPlans.push({ events: [event('meta', { kind: 'opening', replayed: false })], hold: true });
  await opening.build();
  await opening.session.load();
  await opening.until(() => opening.calls.openings.length === 1);
  opening.session.dispose();
  await opening.until(() => opening.session.openingNotice.value === null && opening.calls.openings[0].signal.aborted);
  assert.equal(opening.calls.openings[0].signal.aborted, true);
});

// ---- 離線、404、閘門 ------------------------------------------------------------------------------------

test('offline: the cached history is readable, sending is disabled with an explanation, and nothing goes to the network', async () => {
  const seeded = createHarness({ turns: [turnView(1, 'a', '行動', '劇情')] });
  await seeded.build();
  await seeded.session.load();                                         // 先在線上載入一次，填滿快取
  const cache = seeded.store;

  const h = createHarness({ online: false, seed: async (store) => {
    await store.putSession(SESSION_ID, seeded.server.session);
    await store.putTurns(SESSION_ID, await cache.turnsOf(SESSION_ID));
  } });
  await h.build();
  await h.session.load();
  assert.equal(h.session.offline.value, true);
  assert.equal(h.session.phase.value, 'ready');
  assert.deepEqual(h.kinds(), ['opening', 'turn']);
  assert.equal(h.timeline()[1].story, '劇情');
  assert.equal(h.calls.snapshot.length + h.calls.turns.length + h.calls.fetch.length + h.calls.wallet.length, 0);
  assert.equal(h.session.sendDisabled.value, true);
  assert.match(h.session.blockerText.value, /離線/);
  h.session.draft.value = '行動';
  assert.equal(await h.session.send(), false);
  assert.equal(h.calls.fetch.length, 0);
  // 恢復連線：重新走載入順序
  h.setOnline(true);
  await h.session.setOnline(true);
  assert.equal(h.session.offline.value, false);
  assert.equal(h.calls.snapshot.length, 1);
  assert.equal(h.session.sendDisabled.value, false);
});

test('a network failure on the snapshot falls back to the cache instead of an error page', async () => {
  const h = createHarness({ seed: (store) => store.putTurns(SESSION_ID, [turnView(1, 'a', '行動', '快取的劇情')]) });
  h.server.snapshotFailure = failureInfo(0, '', { kind: 'network' });
  await h.build();
  await h.session.load();
  assert.equal(h.session.phase.value, 'ready');
  assert.equal(h.session.offline.value, true);
  assert.equal(h.timeline().find((item) => item.kind === 'turn').story, '快取的劇情');
});

test('offline with nothing cached shows a retryable explanation instead of an empty page', async () => {
  const h = createHarness({ online: false });
  await h.build();
  await h.session.load();
  assert.equal(h.session.phase.value, 'failed');
  assert.equal(h.session.loadFailure.value.retry, true);
  assert.match(h.session.loadFailure.value.message, /離線/);
});

test('a missing save (404) clears its cache and its in-flight record and says so', async () => {
  const h = createHarness({ seed: async (store) => {
    await store.putTurns(SESSION_ID, [turnView(1, 'a', '行動', '劇情')]);
    await store.setInflight(SESSION_ID, { client_msg_id: 'x', action: 'a', created_at: 'x' });
  } });
  h.server.snapshotFailure = failureInfo(404, 'not_found');
  await h.build();
  await h.session.load();
  assert.equal(h.session.phase.value, 'gone');
  assert.deepEqual(await h.store.turnsOf(SESSION_ID), []);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(await h.store.getSession(SESSION_ID), null);
});

test('a non-empty account gate disables the input, shows the history from the cache, and never calls the server for content', async () => {
  const h = createHarness({ state: { gate: 'age_confirmation_required' }, seed: async (store) => {
    await store.putSession(SESSION_ID, { title: '青雲問道', opening: { state: 'ready', text: OPENING_TEXT }, world: {} });
    await store.putTurns(SESSION_ID, [turnView(1, 'a', '行動', '歷史仍可閱讀')]);
  } });
  await h.build();
  await h.session.load();
  assert.equal(h.session.phase.value, 'ready');
  assert.equal(h.calls.snapshot.length + h.calls.turns.length, 0);
  assert.equal(h.timeline()[1].story, '歷史仍可閱讀');
  assert.equal(h.session.inputDisabled.value, true);
  assert.equal(h.session.sendDisabled.value, true);
  assert.equal(h.session.gateNotice.value.link, '/account');
  h.session.draft.value = '行動';
  assert.equal(await h.session.send(), false);
  assert.equal(h.calls.fetch.length, 0);
});

test('an ended save loads readable, with the input locked', async () => {
  const h = await ready({ session: { ended: true, status: 'ended' } });
  assert.equal(h.session.ended.value, true);
  assert.equal(h.session.inputDisabled.value, true);
  assert.equal(h.session.sendDisabled.value, true);
});

test('a load failure that is not network related shows a retryable error, and retrying loads again', async () => {
  const h = createHarness();
  h.server.snapshotFailure = failureInfo(500, 'internal_error');
  await h.build();
  await h.session.load();
  assert.equal(h.session.phase.value, 'failed');
  assert.equal(h.session.loadFailure.value.retry, true);
  assert.equal(h.session.sendDisabled.value, true);
  h.server.snapshotFailure = null;
  await h.session.retryLoad();
  assert.equal(h.session.phase.value, 'ready');
});

test('loading twice at once reads the snapshot once', async () => {
  const h = createHarness();
  await h.build();
  await Promise.all([h.session.load(), h.session.load()]);
  assert.equal(h.calls.snapshot.length, 1);
});

test('the content-mode note appears only for a player below the adult threshold', async () => {
  assert.match((await ready({ state: { content_mode: { effective: 'safe', is_adult: false } } })).session.modeNote.value, /未滿 18 歲固定使用安全模式/);
  assert.equal((await ready()).session.modeNote.value, '');
  const tier = await ready({ session: { content_tier: 'standard' } });
  assert.equal(tier.session.tierLabel.value, '一般模式');
});

// ---- 捲動 ---------------------------------------------------------------------------------------------

test('new content scrolls to the bottom only while the reader is near it; otherwise it offers a jump back to the latest', async () => {
  const away = { target: { scrollTop: 0, clientHeight: 300, scrollHeight: 1000 } };
  const near = { target: { scrollTop: 640, clientHeight: 300, scrollHeight: 1000 } };
  const h = await ready();
  const plan = normalTurn({ seq: 1, clientMsgId: 'msg-1', story: '第一段第二段', tokens: ['第一段', '第二段'] });
  let ticksWhileAway = null;
  plan.events.splice(2, 0, () => { h.session.onScroll(away); ticksWhileAway = h.session.scrollTick.value; });
  await sendAction(h, '行動', plan);
  assert.equal(h.session.jumpVisible.value, true);                     // 讀者在較早的內容：不強制捲動，提供「回到最新」
  assert.equal(h.session.scrollTick.value, ticksWhileAway);            // 離開底部之後沒有再要求捲動
  const before = h.session.scrollTick.value;
  h.session.jumpToLatest();
  assert.equal(h.session.jumpVisible.value, false);
  assert.equal(h.session.scrollTick.value, before + 1);
  h.session.onScroll(near);                                            // 距離底部 60px：算在底部附近
  assert.equal(h.session.jumpVisible.value, false);
  h.session.onScroll(away);
  h.session.onScroll(near);
  h.server.streamPlans.push(normalTurn({ seq: 2, clientMsgId: 'msg-2', story: '追加的文字' }));
  const ticks = h.session.scrollTick.value;
  h.session.draft.value = '再一個行動';
  await h.session.send();
  assert.ok(h.session.scrollTick.value > ticks);                       // 在底部附近：跟著內容捲動
  assert.equal(h.session.jumpVisible.value, false);
  h.session.onScroll({ target: { scrollTop: 'x' } });                  // 看不懂的捲動資料被忽略
  assert.equal(h.session.jumpVisible.value, false);
});

// ---- 隱私 ---------------------------------------------------------------------------------------------

test('the whole flow writes nothing to the console or to browser storage, and the cache holds no notebook, email, birth date or age range', async () => {
  const trap = trapGlobals();
  try {
    const marker = '獨特標記玩家文字';
    const h = createHarness({ turns: [turnView(1, 'old', `${marker}行動`, `${marker}劇情`)], seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 'lost', action: `${marker}在途`, created_at: 'x' }) });
    await h.build();
    await h.session.load();
    await sendAction(h, `${marker}新行動`, normalTurn({ seq: 2, clientMsgId: 'msg-1', story: `${marker}新劇情` }));
    await sendAction(h, `${marker}被擋`, { events: [metaEvent(3), event('token', { text: `${marker}被擋文字` }), event('error', { code: 'generation_failed', message: marker })] });
    await sendAction(h, `${marker}中斷`, { events: [metaEvent(3), event('token', { text: `${marker}半截` })], cut: true });
    await h.session.abandonInflight();
    h.server.snapshotFailure = failureInfo(0, '', { kind: 'network' });
    await h.session.setOnline(false);
    await h.session.setOnline(true);
    await h.idle();
    assert.deepEqual(trap.logs, []);                                   // 沒有任何主控台輸出（當然也沒有玩家文字）
    assert.deepEqual(trap.writes, []);                                 // 沒有 localStorage、sessionStorage、indexedDB 的寫入
    const dump = JSON.stringify([await h.store.getSession(SESSION_ID), await h.store.turnsOf(SESSION_ID)]);
    for (const forbidden of ['notebook', 'email', 'birth', 'age_range', 'user1@example.com']) { assert.ok(!dump.includes(forbidden), forbidden); }
  } finally {
    trap.restore();
  }
});

test('the module source never touches the DOM, browser storage, the network or the console', () => {
  const source = fs.readFileSync(path.join(ROOT, 'app/static/js/game/game-session.js'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'XMLHttpRequest', 'console.', 'innerHTML', 'v-html', 'location.']) {
    assert.ok(!source.includes(word), word);
  }
});
