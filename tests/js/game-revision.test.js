'use strict';
/* 遊戲頁的重新生成與編輯最後一輪（game-revision.js）：rpg-revision-ui 規格的每個情境——
 * 按鈕只在最後一輪、串流中與離線與已完結與後端說不可修訂時的停用與說明、費用與剩餘免費次數的呈現、理由 Modal（分類與 5 至 50 字）與編輯確認 Modal（餘額不足、空白、沒有改動）、
 * 串流中的氣泡（新文字以打字機顯示、舊內容留在記憶體）、成功／被擋／錯誤／逾時／取消／中斷的還原與通知、`finally` 一定恢復、開始前被拒的各種狀態碼、
 * 重新整理後的在途紀錄判定、其他裝置修訂後的快取覆寫，以及「理由與分類只在請求本文：不進儲存、主控台、通知與時間軸」「行動與劇情以純文字處理」。
 * 環境見 tests/js/support/game-harness.js：真的 Vue、store、sync、turn-stream 與狀態模組，只有伺服器、打字機與計時器是替身。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  createHarness, trapGlobals, failureInfo, turnView, event, normalTurn, plain, SESSION_ID, Labels,
} = require('./support/game-harness.js');

const ROOT = path.join(__dirname, '..', '..');
const REASON = '這段劇情重複又無聊的標記字樣';
const CATEGORY = 'repetitive_or_dull';
const NEW_STORY = '新的劇情：你推開了後門。';

// 後端契約（2026-11-16 規則修訂）：還有免費次數時，重新生成與編輯的幣價都是 0；用完才是一輪的幣價。
const lastTurnOf = (extra) => Object.assign({
  client_msg_id: 't3', seq: 3, revision: 1, revisable: true, reason: null, free_left: 2, regenerate_cost: 0, edit_cost: 0,
}, extra);

const threeTurns = () => [
  turnView(1, 't1', '行動1', '劇情1', { revision: 1 }),
  turnView(2, 't2', '行動2', '劇情2', { revision: 1 }),
  turnView(3, 't3', '行動3', '劇情3', { revision: 1 }),
];

async function ready(options = {}) {
  const h = createHarness(Object.assign({ turns: threeTurns(), session: Object.assign({ completed_turns: 3, last_turn: lastTurnOf() }, options.session) }, options, { session: undefined }));
  Object.assign(h.server.session, { completed_turns: 3, last_turn: lastTurnOf() }, options.session);
  await h.build();
  await h.session.load();
  return h;
}

/* 修訂成功的串流計畫：meta、token、state、done；`apply` 在「伺服器端」替換最後一輪並更新 `last_turn`。 */
function revisionPlan({ kind = 'regenerate', story = NEW_STORY, revision = 1, freeLeft = 1, coins = 230, action, tokens } = {}) {
  const pieces = tokens || [story.slice(0, 2), story.slice(2)];
  return {
    events: [
      event('meta', { kind, turn_id: 3, client_msg_id: 't3', attempt: 2, revision, replayed: false }),
      ...pieces.map((text) => event('token', { text })),
      event('state', { updated: false }),
      event('done', { status: 'complete', turn_number: 3, coins, revision: revision + 1, free_left: freeLeft }),
    ],
    apply: (server) => {
      const stored = server.turns.find((item) => item.seq === 3);
      stored.story = story;
      stored.revision = revision + 1;
      if (action !== undefined) { stored.action = action; }
      const cost = freeLeft > 0 ? 0 : 30;
      server.session.last_turn = Object.assign({}, server.session.last_turn, { revision: revision + 1, free_left: freeLeft, regenerate_cost: cost, edit_cost: cost });
    },
  };
}

const blockedPlan = () => ({
  events: [
    event('meta', { kind: 'regenerate', turn_id: 3, client_msg_id: 't3', attempt: 2, revision: 1, replayed: false }),
    event('token', { text: '被擋的片段' }),
    event('transition', { text: '劇情在這裡轉了個方向。', will_retry: true }),
    event('token', { text: '又被擋的片段' }),
    event('transition', { text: '劇情再次轉了方向。', will_retry: false }),
    event('done', { status: 'blocked', turn_number: 3, coins: 260, revision: 1, free_left: 2 }),
  ],
});

const rejection = (status, code, extra) => ({
  http: { status, code, body: Object.assign({ ok: false, error: { code, message: '伺服器的訊息' } }, extra) },
});

const bubble = (h) => h.timeline().find((item) => item.key === 'turn:t3');

async function regenerate(h, plan, fields = {}) {
  if (plan) { h.server.streamPlans.push(plan); }
  assert.equal(h.revision.openRegenerate(), true);
  h.revision.selectRegenerateCategory(fields.category || CATEGORY);
  h.revision.regenerateReason.value = fields.reason || REASON;
  return h.revision.submitRegenerate();
}

async function edit(h, plan, action = '改成走後門') {
  if (plan) { h.server.streamPlans.push(plan); }
  assert.equal(h.revision.openEdit(), true);
  h.revision.editDraft.value = action;
  return h.revision.submitEdit();
}

// ---- 按鈕只在最後一輪 -------------------------------------------------------------------------------------

test('only the last of three complete turns has the buttons, and they are usable with the shared free count shown beside both of them', async () => {
  const h = await ready();
  assert.equal(h.revision.reviseKey.value, 'turn:t3');
  assert.deepEqual(h.timeline().filter((item) => item.key === h.revision.reviseKey.value).map((item) => item.clientMsgId), ['t3']);   // 其他回合、第 0 幕不會是這個 key
  assert.equal(h.revision.reviseDisabled.value, false);
  assert.equal(h.revision.reviseHint.value, '');
  assert.equal(h.revision.regenerateNote.value, '免費還剩 2 次');
  assert.equal(h.revision.editNote.value, '免費還剩 2 次');                          // 編輯與重新生成共用同一組免費次數
});

test('the edit note follows the free count exactly like the regenerate note: it shows the remaining free times, then the price', async () => {
  const one = await ready({ session: { last_turn: lastTurnOf({ free_left: 1 }) } });
  assert.equal(one.revision.regenerateNote.value, '免費還剩 1 次');
  assert.equal(one.revision.editNote.value, '免費還剩 1 次');
  const none = await ready({ session: { last_turn: lastTurnOf({ free_left: 0, regenerate_cost: 30, edit_cost: 30 }) } });
  assert.equal(none.revision.regenerateNote.value, '30 幣');
  assert.equal(none.revision.editNote.value, '30 幣');
});

test('once the free count is used up the regenerate note shows the price instead, and the price comes from the wallet rather than a constant', async () => {
  const h = await ready({ session: { last_turn: lastTurnOf({ free_left: 0, regenerate_cost: 45, edit_cost: 45 }) }, wallet: { total: 230, cost: 45 } });
  assert.equal(h.revision.regenerateNote.value, '45 幣');
  assert.equal(h.revision.editNote.value, '45 幣');
});

test('a missing price shows no number: neither the wallet nor the snapshot knows it', async () => {
  const h = await ready({ session: { last_turn: lastTurnOf({ free_left: 0, regenerate_cost: null, edit_cost: null }) }, wallet: { total: 230, cost: null } });
  assert.equal(h.revision.regenerateNote.value, '');
  assert.equal(h.revision.editNote.value, '');
  assert.ok(!/undefined|null|NaN/.test(h.revision.regenerateCostLine.value + h.revision.editCostLine.value));
});

test('a price the wallet does not give is taken from the snapshot of the last turn', async () => {
  const h = await ready({ session: { last_turn: lastTurnOf({ free_left: 0, regenerate_cost: 30, edit_cost: 30 }) }, wallet: { total: 230, cost: null } });
  assert.equal(h.revision.regenerateNote.value, '30 幣');
  assert.equal(h.revision.editNote.value, '30 幣');
});

test('the buttons are hidden for the opening-only save, an ended save, a gate, and when the backend says it is not the last turn or the save has ended', async () => {
  const empty = await ready({ turns: [], session: { completed_turns: 0, last_turn: null } });
  assert.equal(empty.revision.reviseKey.value, '');
  const ended = await ready();
  ended.session.ended.value = true;
  assert.equal(ended.revision.reviseKey.value, '');
  const gated = await ready();
  gated.session.revisionHost.gate.value = 'account_locked';
  assert.equal(gated.revision.reviseKey.value, '');
  for (const reason of ['ended', 'not_last']) {
    const h = await ready({ session: { last_turn: lastTurnOf({ revisable: false, reason }) } });
    assert.equal(h.revision.reviseKey.value, '', reason);
  }
});

test('the buttons are hidden when the snapshot and the history disagree about which turn is last (one of them is out of date), or when the snapshot data is broken', async () => {
  const stale = await ready({ session: { last_turn: lastTurnOf({ client_msg_id: 't2', seq: 2 }) } });
  assert.equal(stale.revision.reviseKey.value, '');
  for (const broken of [{ client_msg_id: 't3', seq: 3 }, 'x', { client_msg_id: '', seq: 3, revision: 1, revisable: true }]) {
    const h = await ready({ session: { last_turn: broken } });
    assert.equal(h.revision.reviseKey.value, '', JSON.stringify(broken));
  }
});

test('while a new turn streams the buttons are disabled with a reason; offline, a pending action, and the backend reasons each disable them with their own text', async () => {
  const h = await ready();
  h.session.sending.value = true;
  assert.equal(h.revision.reviseDisabled.value, true);
  assert.equal(h.revision.reviseHint.value, Labels.revisionText('hint_sending'));
  assert.equal(h.revision.openRegenerate(), false);
  assert.equal(h.revision.openEdit(), false);
  h.session.sending.value = false;
  assert.equal(h.revision.reviseDisabled.value, false);

  h.session.setOnline(false);
  assert.equal(h.revision.reviseHint.value, Labels.revisionText('hint_offline'));
  assert.equal(h.revision.reviseDisabled.value, true);
  h.session.offline.value = false;

  h.session.inflight.value = { client_msg_id: 'x', action: '還沒確認' };
  assert.equal(h.revision.reviseHint.value, Labels.revisionText('hint_inflight'));
  h.session.inflight.value = null;

  const busy = await ready({ session: { last_turn: lastTurnOf({ revisable: false, reason: 'in_progress' }) } });
  assert.equal(busy.revision.reviseKey.value, 'turn:t3');                          // 看得到，但停用並說明
  assert.equal(busy.revision.reviseDisabled.value, true);
  assert.equal(busy.revision.reviseHint.value, Labels.revisionText('hint_in_progress'));
  const old = await ready({ session: { last_turn: lastTurnOf({ revisable: false, reason: 'no_snapshot' }) } });
  assert.equal(old.revision.reviseHint.value, Labels.revisionText('hint_no_snapshot'));
  assert.equal(old.revision.openRegenerate(), false);
});

test('after a new turn completes the buttons move to it once the snapshot says so, and stay hidden while the snapshot cannot be read (fail closed)', async () => {
  const h = await ready();
  h.server.streamPlans.push(Object.assign(normalTurn({ seq: 4, clientMsgId: 'msg-1', story: '第四輪。' }), {
    apply: (server) => {
      server.session.completed_turns += 1;
      server.session.last_turn = lastTurnOf({ client_msg_id: 'msg-1', seq: 4 });
    },
  }));
  h.session.draft.value = '再往前走';
  await h.session.send();
  await h.until(() => h.revision.reviseKey.value === 'turn:msg-1', '快照讀到新的最後一輪');
  assert.equal(h.revision.reviseKey.value, 'turn:msg-1');

  const offline = await ready();
  offline.server.snapshotFailure = failureInfo(0, 'network', { kind: 'network' });
  offline.server.streamPlans.push(normalTurn({ seq: 4, clientMsgId: 'msg-1', story: '第四輪。' }));
  offline.session.draft.value = '再往前走';
  await offline.session.send();
  await offline.idle();
  assert.equal(offline.revision.reviseKey.value, '');                             // 還不知道新的最後一輪能不能修訂：不顯示
});

// ---- 重新生成的理由 Modal ----------------------------------------------------------------------------------

test('the regenerate modal needs a category and a reason of 5 to 50 characters; the counter and the cost line follow the input', async () => {
  const h = await ready();
  assert.equal(h.revision.openRegenerate(), true);
  assert.equal(h.revision.regenerateOpen.value, true);
  assert.deepEqual(plain(h.revision.regenerateOptions.value).map((option) => option.value), ['setting_mismatch', 'repetitive_or_dull', 'typo_or_quality', 'blocked_transition', 'other']);
  for (const option of plain(h.revision.regenerateOptions.value)) { assert.equal(option.label, Labels.revisionCategoryLabel(option.value)); }
  assert.equal(h.revision.regenerateCanSubmit.value, false);
  assert.equal(h.revision.regenerateCount.value, '0 / 50');
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成不用遊戲幣（免費還剩 2 次）。');

  h.revision.regenerateReason.value = '字'.repeat(5);
  assert.equal(h.revision.regenerateCanSubmit.value, false);                       // 沒選分類
  h.revision.selectRegenerateCategory(CATEGORY);
  assert.equal(h.revision.regenerateCanSubmit.value, true);
  h.revision.regenerateReason.value = '字'.repeat(4);
  assert.equal(h.revision.regenerateCanSubmit.value, false);                       // 不足 5 字
  assert.equal(h.revision.regenerateError.value, '');                              // 太短不主動罵人
  h.revision.regenerateReason.value = '字'.repeat(50);
  assert.equal(h.revision.regenerateCanSubmit.value, true);
  assert.equal(h.revision.regenerateCount.value, '50 / 50');
  h.revision.regenerateReason.value = '字'.repeat(51);
  assert.equal(h.revision.regenerateCanSubmit.value, false);
  assert.match(h.revision.regenerateError.value, /最多 50 字/);
});

test('the reason length counts Unicode characters: combining marks merge and an emoji is one character', async () => {
  const h = await ready();
  h.revision.openRegenerate();
  h.revision.selectRegenerateCategory('other');
  h.revision.regenerateReason.value = 'é'.repeat(5);
  assert.equal(h.revision.regenerateCanSubmit.value, true);
  assert.equal(h.revision.regenerateCount.value, '5 / 50');
  h.revision.regenerateReason.value = '😀'.repeat(5);
  assert.equal(h.revision.regenerateCanSubmit.value, true);
  h.revision.regenerateReason.value = '😀'.repeat(4);
  assert.equal(h.revision.regenerateCanSubmit.value, false);
});

test('only a category from the page limits can be chosen, and cancelling the modal sends no request and forgets what was typed', async () => {
  const h = await ready();
  h.revision.openRegenerate();
  assert.equal(h.revision.selectRegenerateCategory('made_up'), false);
  assert.equal(h.revision.selectRegenerateCategory('__proto__'), false);
  assert.equal(h.revision.regenerateCategory.value, '');
  h.revision.selectRegenerateCategory(CATEGORY);
  h.revision.regenerateReason.value = REASON;
  assert.equal(h.revision.closeRegenerate(), true);
  assert.equal(h.revision.regenerateOpen.value, false);
  assert.equal(h.revision.regenerateReason.value, '');
  assert.equal(h.revision.regenerateCategory.value, '');
  assert.equal(h.calls.streams.length, 0);
  assert.equal(await h.revision.submitRegenerate(), false);                        // 沒有條件就不送出
  assert.equal(h.calls.streams.length, 0);
});

test('without usable limits on the page the regenerate modal cannot be submitted (fail closed) while the edit still works', async () => {
  for (const limits of [undefined, {}, { reason_min: 5, reason_max: 50, categories: [] }, { reason_min: 0, reason_max: 50, categories: ['other'] }, { reason_min: 60, reason_max: 50, categories: ['other'] }]) {
    const h = await ready({ state: { revision_limits: limits } });
    assert.equal(h.revision.openRegenerate(), false, JSON.stringify(limits));
    assert.equal(h.revision.openEdit(), true, JSON.stringify(limits));
  }
});

// ---- 重新生成的流程 ---------------------------------------------------------------------------------------

test('a regenerate closes the modal, sends the right request, shows the new text in the last bubble while the old content stays in memory, then replaces it with the backend content', async () => {
  const h = await ready();
  const observed = [];
  const plan = revisionPlan();
  plan.events.splice(2, 0, async () => {
    observed.push({
      bubble: plain(bubble(h)), busy: h.revision.revisionBusy.value, sendDisabled: h.session.sendDisabled.value,
      record: await h.store.getInflight(SESSION_ID), cached: plain(h.session.revisionHost.history.value.find((item) => item.seq === 3)),
    });
  });
  const started = regenerate(h, plan);
  assert.equal(h.revision.regenerateOpen.value, false);                            // 送出：Modal 關閉
  assert.equal(h.revision.revisionBusy.value, true);
  await started;

  assert.equal(h.calls.streams.length, 1);
  const call = h.calls.streams[0];
  assert.equal(call.url, `/api/game/sessions/${SESSION_ID}/turns/t3/regenerate/stream`);
  assert.deepEqual(call.body, { expected_revision: 1, category: CATEGORY, reason: REASON });
  assert.equal(call.headers['X-CSRF-Token'], 'CSRF-TOKEN');
  assert.equal(call.credentials, 'same-origin');

  assert.equal(observed.length, 1);
  assert.equal(observed[0].bubble.story, '新的');                                    // 打字機逐字推進
  assert.equal(observed[0].bubble.streaming, true);
  assert.equal(observed[0].bubble.action, '行動3');                                  // 重新生成不改行動
  assert.equal(observed[0].busy, true);
  assert.equal(observed[0].sendDisabled, true);                                    // 修訂中不得送新行動
  assert.equal(observed[0].cached.story, '劇情3');                                   // 舊內容還在記憶體
  assert.deepEqual(plain(observed[0].record), { client_msg_id: 't3', action: '', created_at: '2026-11-12T12:00:00.000Z', kind: 'regenerate', expected_revision: 1 });

  assert.equal(bubble(h).story, NEW_STORY);
  assert.equal(bubble(h).streaming, false);
  assert.equal(h.revision.revisionBusy.value, false);
  assert.equal(h.session.sending.value, false);
  assert.equal(h.revision.regenerateNote.value, '免費還剩 1 次');                     // 依 done 與快照更新
  assert.equal(h.wallet.balanceText.value, '230');
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.deepEqual(h.toasts, []);                                                  // 成功不另外跳通知
  assert.equal(h.calls.turns[h.calls.turns.length - 1].since, 2);                  // 從最後一輪的前一個序號起重讀
  assert.ok(h.calls.snapshot.length >= 2);                                         // 並重讀快照
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal((await h.store.turnsOf(SESSION_ID)).find((item) => item.seq === 3).revision, 2);
});

test('using the last free regenerate switches the note to the price, taken from the done event first and confirmed by the snapshot', async () => {
  const h = await ready({ session: { last_turn: lastTurnOf({ free_left: 1 }) } });
  await regenerate(h, revisionPlan({ freeLeft: 0 }));
  assert.equal(h.revision.regenerateNote.value, '30 幣');
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成會消耗 30 遊戲幣（目前餘額 230 幣）。');
});

test('the new revision of the second regenerate is expected from the content now on screen, not the first one', async () => {
  const h = await ready();
  await regenerate(h, revisionPlan({ revision: 1, freeLeft: 1 }));
  await regenerate(h, revisionPlan({ revision: 2, freeLeft: 0, story: '第三版的劇情。' }));
  assert.deepEqual(h.calls.streams.map((call) => call.body.expected_revision), [1, 2]);
  assert.equal(bubble(h).story, '第三版的劇情。');
});

test('a second click while the request runs makes no second request', async () => {
  const h = await ready();
  h.server.streamPlans.push(revisionPlan());
  h.revision.openRegenerate();
  h.revision.selectRegenerateCategory(CATEGORY);
  h.revision.regenerateReason.value = REASON;
  const first = h.revision.submitRegenerate();
  const second = h.revision.submitRegenerate();
  assert.equal(await second, false);
  assert.equal(await h.revision.submitEdit(), false);
  await first;
  assert.equal(h.calls.streams.length, 1);
});

test('the reason and the category are only in the request body: not in the saved records, the timeline, the toasts, the notice or the console', async () => {
  const trap = trapGlobals();
  try {
    const h = await ready();
    const observed = [];
    const plan = revisionPlan();
    plan.events.splice(2, 0, async () => { observed.push(JSON.stringify([plain(h.timeline()), await h.store.getInflight(SESSION_ID), await h.store.turnsOf(SESSION_ID), await h.store.getSession(SESSION_ID)])); });
    await regenerate(h, plan);
    await regenerate(h, blockedPlan());
    await regenerate(h, revisionPlan({ revision: 2, freeLeft: 0, story: '又一版。' }));
    const everything = JSON.stringify([
      observed, plain(h.timeline()), await h.store.getInflight(SESSION_ID), await h.store.turnsOf(SESSION_ID), await h.store.getSession(SESSION_ID),
      h.toasts, plain(h.session.notice.value), h.revision.regenerateError.value, h.revision.regenerateCount.value, h.revision.regenerateCostLine.value,
    ]);
    for (const marker of [REASON, CATEGORY, '"reason"', '"category"']) { assert.ok(!everything.includes(marker), marker); }
    assert.ok(!JSON.stringify(plain(h.session.lastTurn.value)).includes('category'));      // （快照的 `reason` 是「不能修訂的原因」，與玩家的理由無關，所以上面不掃它）
    assert.ok(JSON.stringify(h.calls.streams[0].body).includes(REASON));         // 它只存在請求本文
    assert.equal(h.revision.regenerateReason.value, '');
    assert.equal(h.revision.regenerateCategory.value, '');
    assert.deepEqual(trap.logs, []);
    assert.deepEqual(trap.writes, []);
  } finally { trap.restore(); }
});

test('the in-flight record handed to the store never carries the reason or the category (checked before the store whitelist gets a say)', async () => {
  const h = await ready();
  const handed = [];
  const original = h.store.setInflight;
  h.store.setInflight = (id, entry) => { handed.push(JSON.stringify(entry)); return original(id, entry); };
  await regenerate(h, revisionPlan());
  await edit(h, revisionPlan({ kind: 'edit', revision: 2, freeLeft: 0, action: '改成走後門' }));
  assert.equal(handed.length, 2);
  for (const entry of handed) { for (const marker of [REASON, CATEGORY, 'reason', 'category']) { assert.ok(!entry.includes(marker), marker); } }
  assert.ok(handed[1].includes('改成走後門'));                                       // 編輯的新行動在紀錄裡（重新整理後判定要用）
});

test('a success is kept on screen even when the re-read after it fails: the new text, the revision and the free count come from the done event', async () => {
  const h = await ready();
  const plan = revisionPlan({ freeLeft: 1 });
  plan.events.splice(plan.events.length - 1, 0, () => {
    h.server.turnsFailure = failureInfo(0, 'network', { kind: 'network' });
    h.server.snapshotFailure = failureInfo(0, 'network', { kind: 'network' });
  });
  await regenerate(h, plan);
  assert.equal(bubble(h).story, NEW_STORY);
  assert.equal(h.revision.regenerateNote.value, '免費還剩 1 次');
  assert.equal(h.session.revisionHost.history.value.find((item) => item.seq === 3).revision, 2);
  assert.deepEqual(h.toasts, []);
  assert.equal(h.revision.revisionBusy.value, false);
});

// ---- 編輯 -------------------------------------------------------------------------------------------------

test('the edit modal is prefilled with the original action and says the edit is free while there are free times left; the original text is not a change', async () => {
  const h = await ready();
  assert.equal(h.revision.openEdit(), true);
  assert.equal(h.revision.editOpen.value, true);
  assert.equal(h.revision.editDraft.value, '行動3');
  assert.equal(h.revision.editCanSubmit.value, false);
  assert.equal(h.revision.editHint.value, Labels.revisionText('edit_unchanged'));
  assert.equal(h.revision.editCostLine.value, '這次編輯免費（免費還剩 2 次）。');
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCount.value, '3 / 1000');
  h.revision.editDraft.value = '  行動3  ';                                          // 頭尾空白不算改動
  assert.equal(h.revision.editCanSubmit.value, false);
  h.revision.editDraft.value = '行動3，換個說法';
  assert.equal(h.revision.editCanSubmit.value, true);
  assert.equal(h.revision.editHint.value, '');
});

test('an empty or too long edit cannot be sent, with the same checks as sending a new action', async () => {
  const h = await ready();
  h.revision.openEdit();
  for (const draft of ['', '   \n  ']) {
    h.revision.editDraft.value = draft;
    assert.equal(h.revision.editCanSubmit.value, false);
    assert.equal(h.revision.editHint.value, '請輸入你的行動。');
  }
  h.revision.editDraft.value = '字'.repeat(1001);
  assert.equal(h.revision.editCanSubmit.value, false);
  assert.match(h.revision.editHint.value, /最多 1000 字/);
  h.revision.editDraft.value = '字'.repeat(1000);
  assert.equal(h.revision.editCanSubmit.value, true);
  assert.equal(await h.revision.submitEdit().then(() => 0), 0);                    // 只確認可以走到送出（下面的測試檢查請求）
});

const USED_UP = () => ({ last_turn: lastTurnOf({ free_left: 0, regenerate_cost: 30, edit_cost: 30 }) });

test('with free times left the edit can be sent even when the balance is below the price or zero, and the modal does not mention the balance', async () => {
  for (const total of [10, 0]) {
    const h = await ready({ wallet: { total } });
    h.revision.openEdit();
    h.revision.editDraft.value = '走後門';
    assert.equal(h.revision.editInsufficient.value, false, `balance ${total}`);
    assert.equal(h.revision.editCanSubmit.value, true, `balance ${total}`);
    assert.equal(h.revision.editCostLine.value, '這次編輯免費（免費還剩 2 次）。');
    assert.ok(!h.revision.editCostLine.value.includes('不足') && !h.revision.editCostLine.value.includes('餘額'));
  }
});

test('with the free times used up the edit modal reminds that it consumes game coins, that the coins already spent are not refunded, and shows the price and the balance', async () => {
  const h = await ready({ session: USED_UP() });
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editCostLine.value, '這次編輯會消耗 30 幣，原本那輪已扣的幣不退（目前餘額 230 幣）。');
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCanSubmit.value, true);
  assert.ok(!h.revision.editCostLine.value.includes('歸零'));
});

test('with the free times used up and a balance below the price the edit cannot be sent and the modal says so and points to the daily check-in', async () => {
  const h = await ready({ wallet: { total: 10 }, session: USED_UP() });
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editInsufficient.value, true);
  assert.equal(h.revision.editCanSubmit.value, false);
  assert.ok(h.revision.editCostLine.value.includes('遊戲幣不足') && h.revision.editCostLine.value.includes('目前餘額 10 幣') && h.revision.editCostLine.value.includes('簽到'));
  h.wallet.setBalance(230);                                                        // 簽到之後餘額變多：可以送出
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCanSubmit.value, true);
});

test('the balance check follows the free count while the modal is open: using the last free time turns a zero balance into a blocked edit', async () => {
  const h = await ready({ wallet: { total: 0 }, session: { last_turn: lastTurnOf({ free_left: 1 }) } });
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editCanSubmit.value, true);
  h.session.lastTurn.value = Object.assign({}, h.session.lastTurn.value, { freeLeft: 0, regenerateCost: 30, editCost: 30 });   // 例如另一個分頁用掉最後一次，快照重讀之後
  assert.equal(h.revision.editInsufficient.value, true);
  assert.equal(h.revision.editCanSubmit.value, false);
});

test('an unknown free count, balance or price never blocks the edit on the client (the server decides)', async () => {
  const h = await ready({ wallet: { total: 230, cost: null }, session: { last_turn: lastTurnOf({ free_left: 0, regenerate_cost: null, edit_cost: null }) } });
  h.server.walletFailure = failureInfo(0, 'network', { kind: 'network' });
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCanSubmit.value, true);
  const unknownFree = await ready({ wallet: { total: 0 } });
  unknownFree.revision.openEdit();
  unknownFree.revision.editDraft.value = '走後門';
  unknownFree.session.lastTurn.value = Object.assign({}, unknownFree.session.lastTurn.value, { freeLeft: null, editCost: 30 });   // 不知道還有沒有免費次數：不在前端擋
  assert.equal(unknownFree.revision.editInsufficient.value, false);
  assert.equal(unknownFree.revision.editCanSubmit.value, true);
  assert.ok(!/undefined|null|NaN/.test(unknownFree.revision.editCostLine.value + unknownFree.revision.editNote.value));
});

test('an edit sends the new action with the same client_msg_id, shows the new action in the bubble while it streams, and keeps it in the in-flight record', async () => {
  const h = await ready();
  const observed = [];
  const plan = revisionPlan({ kind: 'edit', story: '你從後門溜了進去。', freeLeft: 1, action: '改成走後門' });
  plan.events.splice(2, 0, async () => { observed.push({ bubble: plain(bubble(h)), record: await h.store.getInflight(SESSION_ID) }); });
  await edit(h, plan);
  const call = h.calls.streams[0];
  assert.equal(call.url, `/api/game/sessions/${SESSION_ID}/turns/t3/edit/stream`);
  assert.deepEqual(call.body, { expected_revision: 1, action: '改成走後門' });
  assert.equal(observed[0].bubble.action, '改成走後門');                              // 串流中就是新行動
  assert.equal(observed[0].bubble.story, '你從');
  assert.deepEqual(plain(observed[0].record), { client_msg_id: 't3', action: '改成走後門', created_at: '2026-11-12T12:00:00.000Z', kind: 'edit', expected_revision: 1 });
  assert.equal(bubble(h).action, '改成走後門');
  assert.equal(bubble(h).story, '你從後門溜了進去。');
  assert.equal(h.revision.regenerateNote.value, '免費還剩 1 次');                     // 編輯用掉一次免費次數（2 → 1），不歸零
  assert.equal(h.revision.editNote.value, '免費還剩 1 次');
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.revision.editOpen.value, false);
  assert.equal(h.revision.editDraft.value, '');
});

test('an edit and a regenerate use the same free count: two operations in either order use it up and the third one shows the price, on both buttons and in both modals', async () => {
  for (const order of [['edit', 'regenerate'], ['regenerate', 'edit'], ['edit', 'edit'], ['regenerate', 'regenerate']]) {
    const h = await ready();
    let revision = 1;
    let freeLeft = 2;
    for (const kind of order) {
      freeLeft -= 1;
      const plan = revisionPlan({ kind, revision, freeLeft, story: `第 ${revision + 1} 版的劇情。`, action: kind === 'edit' ? `改過的行動${revision}` : undefined });
      if (kind === 'edit') { await edit(h, plan, `改過的行動${revision}`); } else { await regenerate(h, plan); }
      revision += 1;
      assert.equal(h.revision.regenerateNote.value, freeLeft > 0 ? `免費還剩 ${freeLeft} 次` : '30 幣', order.join('>'));
      assert.equal(h.revision.editNote.value, h.revision.regenerateNote.value, order.join('>'));               // 兩個按鈕旁永遠一致
    }
    assert.deepEqual(h.calls.streams.map((call) => call.body.expected_revision), [1, 2], order.join('>'));
    h.revision.openEdit();                                                                                   // 用完之後：編輯 Modal 改為需付費的說明
    assert.equal(h.revision.editCostLine.value, '這次編輯會消耗 30 幣，原本那輪已扣的幣不退（目前餘額 230 幣）。', order.join('>'));
    h.revision.closeEdit();
    h.revision.openRegenerate();
    assert.equal(h.revision.regenerateCostLine.value, '這次重新生成會消耗 30 遊戲幣（目前餘額 230 幣）。', order.join('>'));
    h.revision.closeRegenerate();
  }
});

test('a free edit is sent and completes with a zero balance, and the free count goes down by one', async () => {
  const h = await ready({ wallet: { total: 0 } });
  await edit(h, revisionPlan({ kind: 'edit', freeLeft: 1, coins: 0, action: '改成走後門' }));
  assert.equal(h.calls.streams.length, 1);
  assert.deepEqual(h.calls.streams[0].body, { expected_revision: 1, action: '改成走後門' });
  assert.equal(bubble(h).action, '改成走後門');
  assert.equal(h.revision.editNote.value, '免費還剩 1 次');
  assert.deepEqual(h.toasts, []);
  assert.equal(h.wallet.balance.value, 0);
});

// ---- 管理員不消耗遊戲幣（admin-unlimited-coins）----------------------------------------------------------------
// 伺服器對管理員回 coin_exempt: true、每輪幣價 0，`last_turn` 的兩個費用也是 0（含免費次數用完之後）。畫面只讀這個欄位。

const EXEMPT_USED_UP = () => ({ last_turn: lastTurnOf({ free_left: 0, regenerate_cost: 0, edit_cost: 0 }) });
const exemptWallet = (total) => ({ total, exempt: true });

test('an exempt account with free times left sees the same texts as everyone else, with a zero balance and no balance warning', async () => {
  const h = await ready({ wallet: exemptWallet(0) });
  assert.equal(h.wallet.exempt.value, true);
  assert.equal(h.revision.regenerateNote.value, '免費還剩 2 次');
  assert.equal(h.revision.editNote.value, '免費還剩 2 次');
  h.revision.openRegenerate();
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成不用遊戲幣（免費還剩 2 次）。');
  h.revision.closeRegenerate();
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editCostLine.value, '這次編輯免費（免費還剩 2 次）。');
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCanSubmit.value, true);
  assert.equal(h.wallet.lowBalanceText.value, '');
});

test('an exempt account with the free times used up and a zero balance sees "does not consume game coins" on both buttons and in both modals, and can send', async () => {
  const h = await ready({ wallet: exemptWallet(0), session: EXEMPT_USED_UP() });
  assert.equal(h.revision.regenerateNote.value, '不消耗遊戲幣');
  assert.equal(h.revision.editNote.value, '不消耗遊戲幣');
  assert.equal(h.revision.reviseDisabled.value, false);
  h.revision.openRegenerate();
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成不消耗遊戲幣。');
  h.revision.selectRegenerateCategory(CATEGORY);
  h.revision.regenerateReason.value = REASON;
  assert.equal(h.revision.regenerateCanSubmit.value, true);
  h.revision.closeRegenerate();
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editCostLine.value, '這次編輯不消耗遊戲幣。');
  assert.equal(h.revision.editInsufficient.value, false);
  assert.equal(h.revision.editCanSubmit.value, true);                                // 送出鈕不因餘額停用
  for (const line of [h.revision.regenerateCostLine.value, h.revision.editCostLine.value, h.revision.regenerateNote.value, h.revision.editNote.value]) {
    assert.ok(!line.includes('會消耗') && !line.includes('餘額') && !line.includes('簽到') && !line.includes('不退') && !/undefined|null|NaN/.test(line), line);
  }
  assert.equal(h.wallet.lowBalanceText.value, '');
});

test('an exempt account with a positive balance and the free times used up gets the same texts, and the balance is not mentioned', async () => {
  const h = await ready({ wallet: exemptWallet(230), session: EXEMPT_USED_UP() });
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editCostLine.value, '這次編輯不消耗遊戲幣。');
  assert.equal(h.revision.editCanSubmit.value, true);
  h.revision.closeEdit();
  h.revision.openRegenerate();
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成不消耗遊戲幣。');
  assert.equal(h.revision.regenerateNote.value, '不消耗遊戲幣');
});

test('an ordinary account in the same situation (free times used up, zero balance) is still blocked from editing and still sees the price', async () => {
  const h = await ready({ wallet: { total: 0, exempt: false }, session: USED_UP() });
  assert.equal(h.wallet.exempt.value, false);
  assert.equal(h.revision.regenerateNote.value, '30 幣');
  assert.equal(h.revision.editNote.value, '30 幣');
  h.revision.openEdit();
  h.revision.editDraft.value = '走後門';
  assert.equal(h.revision.editInsufficient.value, true);
  assert.equal(h.revision.editCanSubmit.value, false);
  assert.ok(h.revision.editCostLine.value.includes('遊戲幣不足') && h.revision.editCostLine.value.includes('簽到'));
  h.revision.closeEdit();
  h.revision.openRegenerate();
  assert.equal(h.revision.regenerateCostLine.value, '這次重新生成會消耗 30 遊戲幣（目前餘額 0 幣）。');
  assert.ok(h.wallet.lowBalanceText.value.includes('每一輪需要 30 幣'));
});

test('a wallet without the flag is treated as an ordinary account, and a wallet that cannot be read never blocks the edit on the client', async () => {
  const oldWallet = await ready({ wallet: { total: 0 }, session: USED_UP() });              // 沒有 coin_exempt 欄位的舊版回應
  assert.equal(oldWallet.wallet.exempt.value, false);
  oldWallet.revision.openEdit();
  oldWallet.revision.editDraft.value = '走後門';
  assert.equal(oldWallet.revision.editInsufficient.value, true);                             // 一般玩家的既有行為不變
  // 管理員的錢包第一次讀取就失敗：不知道是否豁免（視為一般玩家的文字），但餘額與幣價都不知道，前端不擋，由伺服器決定
  const failed = createHarness({ turns: threeTurns(), wallet: exemptWallet(0) });
  Object.assign(failed.server.session, { completed_turns: 3 }, EXEMPT_USED_UP());
  failed.server.walletFailure = failureInfo(0, 'network', { kind: 'network' });
  await failed.build();
  await failed.session.load();
  assert.equal(failed.wallet.exempt.value, false);
  assert.equal(failed.wallet.balance.value, null);
  failed.revision.openEdit();
  failed.revision.editDraft.value = '走後門';
  assert.equal(failed.revision.editInsufficient.value, false);
  assert.equal(failed.revision.editCanSubmit.value, true);
  assert.ok(!/undefined|null|NaN/.test(failed.revision.editCostLine.value + failed.revision.editNote.value));
});

test('the exempt texts follow the free count live: the last free edit of an exempt account turns both buttons to "does not consume game coins", and the next one is sent', async () => {
  const h = await ready({ wallet: exemptWallet(0), session: { last_turn: lastTurnOf({ free_left: 1 }) } });
  const plan = revisionPlan({ kind: 'edit', freeLeft: 0, coins: 0, action: '改成走後門' });
  plan.apply = (server) => {
    const stored = server.turns.find((item) => item.seq === 3);
    stored.story = NEW_STORY;
    stored.action = '改成走後門';
    stored.revision = 2;
    server.session.last_turn = Object.assign({}, server.session.last_turn, { revision: 2, free_left: 0, regenerate_cost: 0, edit_cost: 0 });   // 管理員用完免費次數後，伺服器的費用仍是 0
  };
  await edit(h, plan);
  assert.equal(h.revision.regenerateNote.value, '不消耗遊戲幣');
  assert.equal(h.revision.editNote.value, '不消耗遊戲幣');
  assert.equal(h.wallet.balance.value, 0);
  assert.deepEqual(h.toasts, []);
  await edit(h, revisionPlan({ kind: 'edit', revision: 2, freeLeft: 0, coins: 0, action: '再改一次' }), '再改一次');   // 免費次數已用完、餘額 0：仍然送出
  assert.equal(h.calls.streams.length, 2);
  assert.deepEqual(h.calls.streams[1].body, { expected_revision: 2, action: '再改一次' });
});

test('the edit action is trimmed before it is sent and shown as plain text: markup in it is never interpreted', async () => {
  const h = await ready();
  await edit(h, revisionPlan({ kind: 'edit', action: '<b>改</b>' }), '  <b>改</b>  ');
  assert.equal(h.calls.streams[0].body.action, '<b>改</b>');
  assert.equal(bubble(h).action, '<b>改</b>');
  assert.equal(typeof bubble(h).story, 'string');
});

test('story text with markup stays plain text in the bubble and in the cache', async () => {
  const h = await ready();
  await regenerate(h, revisionPlan({ story: '<img src=x onerror=alert(1)> 劇情' }));
  assert.equal(bubble(h).story, '<img src=x onerror=alert(1)> 劇情');
  assert.equal((await h.store.turnsOf(SESSION_ID)).find((item) => item.seq === 3).story, '<img src=x onerror=alert(1)> 劇情');
});

// ---- 失敗時還原舊內容 -------------------------------------------------------------------------------------

test('a blocked regenerate shows the transition line while it retries, then restores the old content and says nothing was charged or used up', async () => {
  const h = await ready();
  const observed = [];
  const plan = blockedPlan();
  plan.events.splice(4, 0, () => observed.push(plain(bubble(h))));                 // 第一次被擋、重生之後
  await regenerate(h, plan);
  assert.deepEqual(observed[0].notes, ['劇情在這裡轉了個方向。']);
  assert.equal(observed[0].streaming, true);
  assert.equal(bubble(h).story, '劇情3');                                           // 舊內容
  assert.equal(bubble(h).action, '行動3');
  assert.deepEqual(plain(bubble(h).notes), []);                                    // 轉場文字顯示後消失
  assert.deepEqual(h.toasts, [Labels.revisionText('kept_blocked')]);
  assert.equal(h.revision.revisionBusy.value, false);
  assert.equal(h.revision.reviseDisabled.value, false);                            // 按鈕恢復可用
  assert.equal(h.revision.regenerateNote.value, '免費還剩 2 次');                     // 沒有用掉免費次數
  assert.equal(h.wallet.balanceText.value, '260');                                 // 餘額依 done（退回之後）更新
  assert.equal(await h.store.getInflight(SESSION_ID), null);
});

test('an error event restores the old content with its fixed message', async () => {
  const h = await ready();
  await regenerate(h, { events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 }), event('token', { text: '半句' }), event('error', { code: 'generation_failed' })] });
  assert.equal(bubble(h).story, '劇情3');
  assert.deepEqual(h.toasts, [Labels.revisionErrorMessage('generation_failed')]);
  assert.equal(h.revision.revisionBusy.value, false);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
});

test('cancelling while it streams restores the old content; when the backend did not change it says the original was kept', async () => {
  const h = await ready();
  const plan = { events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 }), event('token', { text: '半句' })], hold: true };
  const running = regenerate(h, plan);
  await h.until(() => bubble(h) && bubble(h).story === '半句', '氣泡已有半句');
  assert.equal(h.revision.cancelRevision(), true);
  await running;
  assert.equal(h.calls.streams[0].signal.aborted, true);
  assert.equal(bubble(h).story, '劇情3');
  assert.deepEqual(h.toasts, [Labels.revisionText('kept')]);
  assert.equal(h.revision.revisionBusy.value, false);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.revision.cancelRevision(), false);                                // 沒有進行中的修訂
});

test('a cancel after the backend had already replaced the content shows the backend content and says it was synced, never "kept"', async () => {
  const h = await ready();
  const plan = Object.assign(revisionPlan(), { hold: true });
  plan.events.splice(3);                                                           // 只送兩個 token，沒有 done：連線一直開著
  const running = regenerate(h, plan);
  await h.until(() => bubble(h) && bubble(h).story === NEW_STORY, '氣泡已有新文字');
  h.revision.cancelRevision();
  await running;
  assert.equal(bubble(h).story, NEW_STORY);                                        // 以伺服器上的內容為準
  assert.deepEqual(h.toasts, [Labels.revisionText('synced')]);
  assert.equal(h.revision.revisionBusy.value, false);
});

test('a dropped connection with the backend unchanged keeps the original; with the backend still processing it says so', async () => {
  const cut = { events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 }), event('token', { text: '半句' })] };
  const kept = await ready();
  await regenerate(kept, cut);
  assert.equal(bubble(kept).story, '劇情3');
  assert.deepEqual(kept.toasts, [Labels.revisionText('kept')]);

  const processing = await ready();
  processing.server.streamPlans.push(cut);
  processing.revision.openRegenerate();
  processing.revision.selectRegenerateCategory(CATEGORY);
  processing.revision.regenerateReason.value = REASON;
  const plan = processing.server.streamPlans[0];
  plan.events.push(() => { processing.server.session.last_turn = lastTurnOf({ revisable: false, reason: 'in_progress' }); });
  await processing.revision.submitRegenerate();
  assert.deepEqual(processing.toasts, [Labels.revisionText('processing')]);
  assert.equal(bubble(processing).story, '劇情3');
});

test('when the backend cannot be read after an interruption the message stays uncertain and the in-flight record is kept for the next load', async () => {
  const h = await ready();
  const plan = { events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 }), event('token', { text: '半句' }), () => { h.server.snapshotFailure = failureInfo(0, 'network', { kind: 'network' }); }] };
  await regenerate(h, plan);
  assert.equal(bubble(h).story, '劇情3');
  assert.deepEqual(h.toasts, [Labels.revisionText('uncertain')]);                  // 沒有承諾「沒有扣幣」
  assert.equal((await h.store.getInflight(SESSION_ID)).kind, 'regenerate');
  assert.equal(h.revision.revisionBusy.value, false);
});

test('leaving the page aborts the request', async () => {
  const h = await ready();
  const running = regenerate(h, { events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 })], hold: true });
  await h.until(() => h.calls.streams.length === 1, '請求已送出');
  h.revision.dispose();
  assert.equal(h.calls.streams[0].signal.aborted, true);
  await running;
  assert.equal(h.revision.revisionBusy.value, false);
});

test('whatever the outcome the busy flag, the overlay and the buttons are restored (the finally of every path)', async () => {
  const plans = [
    () => revisionPlan(), () => blockedPlan(), () => ({ events: [event('error', { code: 'internal_error' })] }),
    () => ({ events: [event('meta', { kind: 'regenerate', turn_id: 3, attempt: 2, revision: 1 })] }), () => rejection(409, 'turn_in_progress'),
    () => rejection(503, 'system_busy'), () => ({ networkError: true }),
  ];
  for (const make of plans) {
    const h = await ready();
    await regenerate(h, make());
    assert.equal(h.revision.revisionBusy.value, false);
    assert.equal(h.session.revisionHost.overlay.value, null);
    assert.equal(h.session.sending.value, false);
    assert.equal(h.session.sendDisabled.value, false);
    assert.equal(bubble(h) && bubble(h).streaming, false);
  }
});

// ---- 開始前被拒 -------------------------------------------------------------------------------------------

test('a revision conflict (another device got there first) shows the latest content and the explanation, charges nothing and keeps no record', async () => {
  const h = await ready();
  h.server.turns[2] = Object.assign({}, h.server.turns[2], { story: '另一個裝置的新劇情', revision: 2 });
  h.server.session.last_turn = lastTurnOf({ revision: 2 });
  await regenerate(h, rejection(409, 'revision_conflict'));
  assert.match(h.session.notice.value.text, /已經在另一個地方被修改過/);
  assert.equal(bubble(h).story, '另一個裝置的新劇情');
  assert.equal(h.session.revisionHost.lastTurn.value.revision, 2);
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.deepEqual(h.toasts, []);
  assert.equal(h.revision.revisionBusy.value, false);
});

test('a not-revisable answer for an ended save turns the page to ended and hides the buttons', async () => {
  const h = await ready();
  h.server.session.status = 'ended';
  h.server.session.ended = true;
  await regenerate(h, rejection(409, 'not_revisable', { reason: 'ended' }));
  assert.equal(h.session.ended.value, true);
  assert.equal(h.revision.reviseKey.value, '');
  assert.match(h.session.notice.value.text, /已經完結/);
});

test('not-revisable because something is in progress keeps the buttons but disabled with the reason, after the snapshot is re-read', async () => {
  const h = await ready();
  h.server.session.last_turn = lastTurnOf({ revisable: false, reason: 'in_progress' });
  await regenerate(h, rejection(409, 'not_revisable', { reason: 'in_progress' }));
  assert.match(h.session.notice.value.text, /正在處理中/);
  assert.equal(h.revision.reviseDisabled.value, true);
  assert.equal(h.revision.reviseHint.value, Labels.revisionText('hint_in_progress'));
});

test('insufficient coins on the server opens the wallet modal that says the original response is kept, and nothing else changes', async () => {
  const h = await ready({ wallet: { total: 230 }, session: USED_UP() });             // 免費次數用完的付費編輯（餘額在這個瞬間被別處花掉，所以伺服器仍回 409）
  await edit(h, rejection(409, 'insufficient_coins', { balance: 10, needed: 30 }));
  assert.equal(h.wallet.coinModalOpen.value, true);
  assert.match(h.wallet.coinModalText.value, /目前餘額 10 幣/);
  assert.match(h.wallet.coinModalText.value, /原本的回應已保留/);
  assert.ok(!h.wallet.coinModalText.value.includes('輸入框'));                       // 沒有行動文字留在輸入框
  assert.equal(bubble(h).story, '劇情3');
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.revision.revisionBusy.value, false);
});

test('fields rejected by the server reopen the regenerate modal with what the player typed and the server explanation, which disappears when the fields change', async () => {
  const h = await ready();
  await regenerate(h, rejection(422, 'validation_error', { error: { code: 'validation_error', message: 'x', fields: { reason: '請簡短說明原因，至少 5 字。' } } }));
  assert.equal(h.revision.regenerateOpen.value, true);
  assert.equal(h.revision.regenerateCategory.value, CATEGORY);
  assert.equal(h.revision.regenerateReason.value, REASON);
  assert.equal(h.revision.regenerateError.value, '請簡短說明原因，至少 5 字。');
  assert.equal(h.calls.streams.length, 1);
  assert.equal(h.revision.revisionBusy.value, false);
  h.revision.regenerateReason.value = REASON + '！';
  assert.equal(h.revision.regenerateError.value, '');
});

test('a content-rule rejection of an edit reopens the edit modal with the draft kept and names only the categories', async () => {
  const h = await ready();
  await edit(h, rejection(422, 'input_blocked', { categories: ['non_consensual'] }), '被拒絕的新行動');
  assert.equal(h.revision.editOpen.value, true);
  assert.equal(h.revision.editDraft.value, '被拒絕的新行動');
  assert.match(h.revision.editError.value, /非自願的性情節/);
  assert.ok(!h.revision.editError.value.includes('被拒絕的新行動'));
  assert.equal(h.revision.editCanSubmit.value, true);                              // 玩家可以修改後再送
});

test('an expired login goes to the login page, and a busy server shows a notice, neither reopens a modal or leaves a record', async () => {
  const login = await ready();
  await regenerate(login, rejection(401, 'unauthorized'));
  assert.deepEqual(login.logins, ['session']);
  const busy = await ready();
  await regenerate(busy, rejection(503, 'system_busy'));
  assert.match(busy.session.notice.value.text, /使用人數較多/);
  for (const h of [login, busy]) {
    assert.equal(h.revision.regenerateOpen.value, false);
    assert.equal(await h.store.getInflight(SESSION_ID), null);
    assert.equal(h.revision.revisionBusy.value, false);
  }
});

test('a network failure before the response keeps the old content and does not claim anything about the backend', async () => {
  const h = await ready();
  await regenerate(h, { networkError: true });
  assert.equal(bubble(h).story, '劇情3');
  assert.deepEqual(h.toasts, [Labels.revisionText('kept')]);
});

// ---- 重新整理與其他裝置 -----------------------------------------------------------------------------------

test('another device regenerated: a stale cache is overwritten on load because the re-read starts one turn before the last', async () => {
  const h = createHarness({
    turns: [turnView(1, 't1', '行動1', '劇情1'), turnView(2, 't2', '行動2', '劇情2'), turnView(3, 't3', '行動3', '甲裝置的新劇情', { revision: 2 })],
    session: { completed_turns: 3, last_turn: lastTurnOf({ revision: 2, free_left: 1 }) },
    seed: (store) => store.putTurns(SESSION_ID, [turnView(1, 't1', '行動1', '劇情1'), turnView(2, 't2', '行動2', '劇情2'), turnView(3, 't3', '行動3', '乙裝置的舊劇情', { revision: 1 })]),
  });
  await h.build();
  await h.session.load();
  assert.equal(bubble(h).story, '甲裝置的新劇情');
  assert.equal(h.calls.turns[0].since, 2);
  assert.equal(h.revision.regenerateNote.value, '免費還剩 1 次');
  assert.equal((await h.store.turnsOf(SESSION_ID))[2].revision, 2);
});

test('reloading in the middle of a regenerate that the backend finished: the record is cleared, the new content and free count show, nothing is announced or resent', async () => {
  const h = createHarness({
    turns: [turnView(1, 't1', '行動1', '劇情1'), turnView(2, 't2', '行動2', '劇情2'), turnView(3, 't3', '行動3', '新劇情', { revision: 2 })],
    session: { completed_turns: 3, last_turn: lastTurnOf({ revision: 2, free_left: 1 }) },
    seed: async (store) => {
      await store.putTurns(SESSION_ID, [turnView(1, 't1', '行動1', '劇情1'), turnView(2, 't2', '行動2', '劇情2'), turnView(3, 't3', '行動3', '舊劇情', { revision: 1 })]);
      await store.setInflight(SESSION_ID, { client_msg_id: 't3', kind: 'regenerate', expected_revision: 1, created_at: 'x' });
    },
  });
  await h.build();
  await h.session.load();
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.session.inflight.value, null);
  assert.equal(h.session.notice.value, null);
  assert.equal(bubble(h).story, '新劇情');
  assert.equal(h.revision.regenerateNote.value, '免費還剩 1 次');
  assert.equal(h.calls.streams.length, 0);
  assert.equal(h.revision.reviseDisabled.value, false);
});

test('reloading when the backend did not replace anything: the record is cleared, the player is told the original was kept, the old content and buttons stay, nothing is resent', async () => {
  const h = createHarness({
    turns: threeTurns(), session: { completed_turns: 3, last_turn: lastTurnOf() },
    seed: (store) => store.setInflight(SESSION_ID, { client_msg_id: 't3', kind: 'edit', expected_revision: 1, action: '沒送完的新行動', created_at: 'x' }),
  });
  await h.build();
  await h.session.load();
  assert.equal(await h.store.getInflight(SESSION_ID), null);
  assert.equal(h.session.inflight.value, null);                                    // 不提供「重送」
  assert.equal(h.session.notice.value.text, Labels.revisionText('incomplete'));
  assert.equal(bubble(h).story, '劇情3');
  assert.equal(bubble(h).action, '行動3');
  assert.equal(h.revision.reviseKey.value, 'turn:t3');
  assert.equal(h.revision.reviseDisabled.value, false);
  assert.equal(h.session.sendDisabled.value, false);
  assert.equal(h.calls.streams.length, 0);
  assert.ok(!JSON.stringify(plain(h.session.notice.value)).includes('沒送完的新行動'));
});

// ---- 檔案本身 ---------------------------------------------------------------------------------------------

test('the module touches no window, document, browser storage, console or network of its own, and has no resend or auto-queue', () => {
  const source = fs.readFileSync(path.join(ROOT, 'app', 'static', 'js', 'game', 'game-revision.js'), 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'console.', 'fetch(', 'XMLHttpRequest', 'axios', 'setTimeout', 'setInterval', 'innerHTML', 'eval(']) {
    assert.ok(!code.includes(forbidden), forbidden);
  }
  assert.ok(!/resend|queue|retry/i.test(code), '修訂不自動排隊重送，也沒有重送');
  assert.deepEqual([...code.matchAll(/store\.(\w+)\(/g)].map((match) => match[1]).sort(), ['clearInflight', 'putTurns', 'setInflight']);
});

test('in the browser the module joins the shared Game namespace', () => {
  const vm = require('node:vm');
  const sandbox = { console };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext('self.Game = { api: { marker: 1 } };', sandbox);
  for (const file of ['game-labels.js', 'game-state.js', 'game-revision.js']) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, 'app', 'static', 'js', 'game', file), 'utf8'), sandbox, { filename: file });
  }
  assert.equal(typeof sandbox.Game.createRevision, 'function');
  assert.equal(sandbox.Game.api.marker, 1);
});
