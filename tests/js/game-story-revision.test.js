'use strict';
/* rpg-story-view 6.4：重新生成與編輯最後一輪掛在劇情卡上（game-revision.js 接 game-session.js 的劇情卡）——
 * 規格 rpg-revision-ui 的情境：按鈕只在劇情卡當下顯示的是最後一個完成的回合時出現（新的一輪串流中、第 0 幕、被擋與中斷的說明都沒有）、
 * 成功後劇情卡從第一段重新閱讀新內容、失敗／被擋／錯誤／中斷／被拒絕時還原成舊內容與原本所在的段、修訂時收回建議，以及理由不進劇情卡與儲存。
 * 其餘（Modal、費用、通知、失敗訊息）仍由 tests/js/game-revision.test.js 涵蓋。環境見 tests/js/support/game-harness.js。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  createHarness, trapGlobals, turnView, event, normalTurn, plain, Labels,
} = require('./support/game-harness.js');

const REASON = '這段劇情重複又無聊的標記字樣';
const CATEGORY = 'repetitive_or_dull';
const OLD_STORY = '舊一。舊二。舊三。舊四。舊五。';                 // 兩段：['舊一。舊二。舊三。', '舊四。舊五。']
const OLD_P1 = '舊一。舊二。舊三。';
const OLD_P2 = '舊四。舊五。';
const NEW_STORY = '新一。新二。新三。新四。新五。';
const NEW_P1 = '新一。新二。新三。';
const NEW_P2 = '新四。新五。';

const lastTurnOf = (extra) => Object.assign({
  client_msg_id: 't3', seq: 3, revision: 1, revisable: true, reason: null, free_left: 2, regenerate_cost: 0, edit_cost: 0,
}, extra);

const threeTurns = () => [
  turnView(1, 't1', '行動1', '劇情1。', { revision: 1 }),
  turnView(2, 't2', '行動2', '劇情2。', { revision: 1 }),
  turnView(3, 't3', '行動3', OLD_STORY, { revision: 1 }),
];

async function ready(options = {}) {
  const h = createHarness(Object.assign({ turns: threeTurns(), session: { completed_turns: 3, last_turn: lastTurnOf() } }, options));
  await h.build();
  await h.session.load();
  return h;
}

/* 修訂成功的串流計畫；`apply` 在「伺服器端」替換最後一輪並更新 `last_turn`。 */
function revisionPlan({ kind = 'regenerate', story = NEW_STORY, tokens, action, extra } = {}) {
  const pieces = tokens || [story.slice(0, 7), story.slice(7)];
  return Object.assign({
    events: [
      event('meta', { kind, turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
      ...pieces.map((text) => event('token', { text })),
      event('state', { updated: false }),
      event('done', { status: 'complete', turn_number: 3, coins: 230, revision: 2, free_left: 1 }),
    ],
    apply: (server) => {
      const stored = server.turns.find((item) => item.seq === 3);
      stored.story = story;
      stored.revision = 2;
      if (action !== undefined) { stored.action = action; }
      server.session.last_turn = Object.assign({}, server.session.last_turn, { revision: 2, free_left: 1, regenerate_cost: 0, edit_cost: 0 });
    },
  }, extra);
}

const blockedPlan = () => ({
  events: [
    event('meta', { kind: 'regenerate', turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
    event('token', { text: '被擋一。被擋二。被擋三。被擋四。' }),
    event('transition', { text: '劇情在這裡轉了個方向。', will_retry: false }),
    event('done', { status: 'blocked', turn_number: 3, coins: 260, revision: 1, free_left: 2 }),
  ],
});

async function regenerate(h, plan) {
  if (plan) { h.server.streamPlans.push(plan); }
  assert.equal(h.revision.openRegenerate(), true);
  h.revision.selectRegenerateCategory(CATEGORY);
  h.revision.regenerateReason.value = REASON;
  return h.revision.submitRegenerate();
}

async function edit(h, plan, action = '改成走後門') {
  if (plan) { h.server.streamPlans.push(plan); }
  assert.equal(h.revision.openEdit(), true);
  h.revision.editDraft.value = action;
  return h.revision.submitEdit();
}

// ---- 按鈕只在劇情卡顯示最後一輪時出現 ------------------------------------------------------------------------

test('the buttons belong to the card: they appear while the card shows the last complete turn, also after paging back inside that turn', async () => {
  const h = await ready();
  assert.equal(h.card().key, 'turn:t3');
  assert.equal(h.revision.reviseKey.value, 'turn:t3');
  assert.equal(h.revision.reviseDisabled.value, false);
  h.session.prevPage();
  assert.equal(h.card().text, OLD_P1);
  assert.equal(h.revision.reviseKey.value, 'turn:t3');               // 在當前輪內回看：仍是最後一輪
  h.session.prevPage();
  assert.equal(h.card().speakerLabel, '你');
  assert.equal(h.revision.reviseKey.value, 'turn:t3');               // 行動頁也是這一輪
  assert.equal(h.revision.openRegenerate(), true);
});

test('there are no buttons on the opening card, and none while no turn is complete', async () => {
  const h = await ready({ turns: [], session: { completed_turns: 0, last_turn: null } });
  assert.equal(h.card().key, 'opening');
  assert.equal(h.revision.reviseKey.value, '');
  assert.equal(h.revision.openRegenerate(), false);
  assert.equal(h.revision.openEdit(), false);
});

test('while a new turn streams the card shows that turn and the old last turn has no buttons; afterwards the new last turn has them', async () => {
  const h = await ready();
  const seen = {};
  const plan = normalTurn({ seq: 4, clientMsgId: 'msg-1', story: '甲一。甲二。甲三。甲四。' });
  plan.events.splice(2, 0, () => { seen.key = h.revision.reviseKey.value; seen.card = h.card().key; });
  const afterApply = plan.apply;
  plan.apply = (server) => {
    afterApply(server);
    server.turns.find((item) => item.seq === 4).revision = 1;
    server.session.last_turn = lastTurnOf({ client_msg_id: 'msg-1', seq: 4 });
  };
  h.server.streamPlans.push(plan);
  h.session.draft.value = '新的行動';
  await h.session.send();
  assert.equal(seen.card, 'turn:msg-1');
  assert.equal(seen.key, '');                                        // 串流中：卡片顯示新的一輪，上一輪的按鈕不在
  await h.until(() => h.revision.reviseKey.value === 'turn:msg-1', '快照讀到新的最後一輪');
  assert.equal(h.revision.reviseKey.value, 'turn:msg-1');
});

test('a blocked or interrupted turn leaves a one-line note on the card, and then nothing on the card can be regenerated or edited', async () => {
  const h = await ready();
  h.server.streamPlans.push({
    events: [event('meta', { turn_id: 4, attempt: 1, replayed: false }), event('transition', { text: '（轉場）', will_retry: false }),
      event('done', { status: 'blocked', turn_number: null, coins: 230 })],
    commit: { view: { seq: 4, client_msg_id: 'msg-1', turn_number: null, status: 'blocked', action: null, story: null } },
  });
  h.session.draft.value = '會被擋的行動';
  await h.session.send();
  assert.equal(h.card().note, '（轉場）');
  assert.equal(h.revision.reviseKey.value, '');
  assert.equal(h.revision.openRegenerate(), false);
  assert.equal(h.revision.openEdit(), false);
});

// ---- 成功：從第一段重新閱讀 -----------------------------------------------------------------------------------

test('a successful regeneration shows the same action first and then the new pages from the first one, and the history keeps the old text meanwhile', async () => {
  const h = await ready();
  h.session.prevPage();                                              // 玩家原本在讀第一段
  const seen = {};
  const plan = revisionPlan();
  plan.events.splice(3, 0, () => {
    seen.card = h.card();
    seen.history = h.session.revisionHost.history.value.find((turn) => turn.seq === 3).story;
    seen.reviseKey = h.revision.reviseKey.value;
  });
  await regenerate(h, plan);
  assert.equal(seen.card.key, 'turn:t3');
  assert.equal(seen.card.text, NEW_P1);                              // 新內容從第一段開始
  assert.equal(seen.card.index, 1);
  assert.ok(!JSON.stringify(seen.card).includes('舊'));
  assert.equal(seen.history, OLD_STORY);                             // 舊內容留在記憶體（歷史），沒有被改動
  assert.equal(seen.reviseKey, 'turn:t3');                           // 按鈕仍屬於這一輪（停用由 reviseHint 說明）
  const card = h.card();
  assert.equal(card.text, NEW_P1);                                   // 結束、重讀之後仍停在第一段，沒有被補齊拉到最後一段
  assert.equal(card.index, 1);
  assert.equal(card.total, 3);                                       // 行動頁加兩段新內容
  assert.equal(card.finished, true);
  assert.equal(h.session.nextPage(), 'advanced');
  assert.equal(h.card().text, NEW_P2);
  assert.equal(h.session.prevPage(), 'back');
  assert.equal(h.session.prevPage(), 'back');
  assert.equal(h.card().text, '行動3');                               // 重新生成：行動不變
  assert.equal(h.session.revisionHost.history.value.find((turn) => turn.seq === 3).story, NEW_STORY);   // 重讀之後歷史是新內容
});

test('a successful edit shows the new action as the first page and then the new pages', async () => {
  const h = await ready();
  await edit(h, revisionPlan({ kind: 'edit', action: '改成走後門' }), '改成走後門');
  const card = h.card();
  assert.equal(card.key, 'turn:t3');
  assert.equal(card.text, NEW_P1);
  assert.equal(card.index, 1);
  h.session.prevPage();
  assert.equal(h.card().speakerLabel, '你');
  assert.equal(h.card().text, '改成走後門');                            // 編輯：行動頁換成新行動
});

test('while the revision streams, the card is on the first page of the new text (not the old page) and nothing of the old text shows', async () => {
  const h = await ready({ readerAutoFinish: false });
  h.session.prevPage();
  const seen = {};
  const plan = revisionPlan({ tokens: ['新一。新二。新三。新', '四。新五。'] });
  plan.events.splice(1, 0, () => { seen.start = h.card(); });         // meta 之後、第一個片段之前
  plan.events.splice(3, 0, () => { seen.first = h.card(); });         // 第一個片段（湊滿第一段）之後
  await regenerate(h, plan);
  assert.equal(seen.start.key, 'turn:t3');
  assert.equal(seen.start.speakerLabel, '你');                       // 開始時先顯示這一輪的行動，第一段釋出後換成新劇情
  assert.equal(seen.start.text, '行動3');
  assert.equal(seen.start.waiting, true);
  assert.ok(!JSON.stringify(seen.start).includes('舊'));
  assert.equal(seen.first.speakerLabel, '旁白');
  assert.equal(seen.first.fullText, NEW_P1);                          // 新內容的第一段（不是玩家原本看的那一段）；打字機才剛開始，已顯示的字還是空的
  assert.equal(seen.first.text, '');
  assert.equal(seen.first.index, 1);
  assert.equal(seen.first.typing, true);                              // 逐字顯示
  assert.ok(!JSON.stringify(seen.first).includes('舊'));
});

// ---- 失敗：還原舊內容與原本所在的段 -----------------------------------------------------------------------------

test('a regeneration that ends blocked puts the old content back on the page the player was reading, and says so', async () => {
  const h = await ready();
  h.session.prevPage();                                              // 玩家在第一段
  const before = h.card();
  assert.equal(before.text, OLD_P1);
  await regenerate(h, blockedPlan());
  assert.deepEqual(h.card(), before);                                // 舊內容與原本所在的段
  assert.deepEqual(h.toasts, [Labels.revisionText('kept_blocked')]);
  assert.equal(h.revision.reviseKey.value, 'turn:t3');               // 按鈕恢復
  assert.equal(h.session.revising.value, false);
  assert.ok(!JSON.stringify(h.card()).includes('被擋'));
  assert.equal(h.session.nextPage(), 'advanced');                    // 還原後可以正常翻頁
  assert.equal(h.card().text, OLD_P2);
});

test('a regeneration that fails with a stream error keeps the old card', async () => {
  const h = await ready();
  const before = h.card();
  await regenerate(h, { events: [event('meta', { kind: 'regenerate', turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
    event('token', { text: '一半。的內容。很長。很長。' }), event('error', { code: 'generation_failed' })] });
  assert.deepEqual(h.card(), before);
  assert.equal(h.toasts.length, 1);
  assert.ok(!JSON.stringify(h.card()).includes('一半'));
});

test('a refused start (conflict, busy, limit) leaves the card exactly as it was', async () => {
  for (const [status, code] of [[409, 'revision_conflict'], [409, 'turn_in_progress'], [429, 'rate_limited'], [503, 'system_busy']]) {
    const h = await ready();
    h.session.prevPage();
    const before = h.card();
    await regenerate(h, { http: { status, code, body: { ok: false, error: { code, message: '伺服器的訊息' } } } });
    assert.equal(h.session.revising.value, false, code);
    assert.deepEqual(plain(h.card()), plain(before), code);
  }
});

test('cancelling a revision puts the old content back', async () => {
  const h = await ready();
  const before = h.card();
  h.server.streamPlans.push({ events: [event('meta', { kind: 'regenerate', turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
    event('token', { text: '新一。新二。新三。新四。' })], hold: true });
  assert.equal(h.revision.openRegenerate(), true);
  h.revision.selectRegenerateCategory(CATEGORY);
  h.revision.regenerateReason.value = REASON;
  const running = h.revision.submitRegenerate();
  await h.until(() => h.calls.streams.length === 1 && h.card().total > 1, '串流開始，新內容的第一段出現');
  assert.equal(h.revision.cancelRevision(), true);
  await running;
  assert.deepEqual(h.card(), before);
  assert.equal(h.session.revising.value, false);
});

test('a connection cut after the server had already finished shows the server version once the re-read finds it, at its last page', async () => {
  const h = await ready();
  const plan = revisionPlan({ extra: { cut: true } });
  plan.events = plan.events.slice(0, 3);                              // meta 與一個 token 之後斷線，沒有 done；伺服器端其實已完成（apply 照做）
  await regenerate(h, plan);
  assert.equal(h.card().key, 'turn:t3');
  assert.equal(h.card().text, NEW_P2);                                // 後端已修訂完成：劇情卡換成後端的內容（載入，停在最後一段）
  assert.equal(h.card().canNext, false);
  assert.ok(!JSON.stringify(h.card()).includes('舊'));
  assert.deepEqual(h.toasts, [Labels.revisionText('synced')]);
});

test('a connection cut when the server did not finish puts the old content back and keeps the old text', async () => {
  const h = await ready();
  const before = h.card();
  await regenerate(h, { events: [event('meta', { kind: 'regenerate', turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
    event('token', { text: '新一。新二。新三。新四。' })], cut: true });
  assert.deepEqual(h.card(), before);
  assert.deepEqual(h.toasts, [Labels.revisionText('kept')]);
});

// ---- 建議與劇情卡 -----------------------------------------------------------------------------------------

test('starting a revision folds the open suggestions back and forgets them', async () => {
  const h = await ready();
  h.server.suggestPlans.push({ suggestions: ['修訂前的建議'] });
  await h.session.toggleSuggestions();
  assert.equal(h.session.suggestOpen.value, true);
  await regenerate(h, revisionPlan());
  assert.equal(h.session.suggestOpen.value, false);                  // 最後一輪被修訂：已開啟的建議收回
  h.server.suggestPlans.push({ suggestions: ['修訂後的新建議'] });
  await h.session.toggleSuggestions();
  assert.equal(h.calls.suggest.length, 2);                           // 舊建議已過期：重新產生
  assert.deepEqual(plain(h.session.suggestItems.value), ['修訂後的新建議']);
});

test('a refused revision still folds the suggestions back, but asking again costs no model call because the server reuses the saved ones', async () => {
  const h = await ready({ session: { completed_turns: 3, last_turn: lastTurnOf(), suggestions: ['已保存的建議'] } });
  await h.session.toggleSuggestions();
  assert.equal(h.calls.suggest.length, 0);
  await regenerate(h, { http: { status: 409, code: 'revision_conflict', body: { ok: false, error: { code: 'revision_conflict', message: '伺服器的訊息' } } } });
  assert.equal(h.session.suggestOpen.value, false);
});

// ---- 不留痕跡 -----------------------------------------------------------------------------------------------

test('the reason and the category never reach the card, and a whole revision writes nothing to the console or browser storage', async () => {
  const trap = trapGlobals();
  try {
    const h = await ready();
    await regenerate(h, revisionPlan());
    await regenerate(h, blockedPlan());
    assert.ok(!JSON.stringify(h.card()).includes('標記字樣'));
    assert.ok(!JSON.stringify(h.session.items.value).includes('標記字樣'));
    assert.deepEqual(trap.logs, []);
    assert.deepEqual(trap.writes, []);
  } finally {
    trap.restore();
  }
});
