'use strict';
/* 記事簿面板（game-notebook.js）：rpg-notebook-panel 規格的每個情境——開啟才讀全文、用量與預備空間、空狀態、全文含標記字元、已滿（面板與入口）、
 * 剛好用完容量不顯示已滿、編輯與即時字數、超過容量禁用儲存、儲存成功、重複點擊只送一次、finally 恢復、版本衝突保留草稿、422／409／404／401／網路的對照、
 * 未儲存離開要確認、已完結唯讀、從不自動儲存、全文不進主控台與瀏覽器儲存。
 * 以真的 Vue（tests/js/support/vue-runtime.js）、真的 game-state／game-labels 與替身 API 執行；替身 API 記錄每個呼叫，用來證明「沒有多送的請求」。
 * 掛到 game.js 之後的整合（側欄分頁、載入頁面時不請求、入口、IndexedDB 內容）在 tests/js/game.test.js。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadVue, plain, settle, ROOT } = require('./support/vue-runtime.js');
const { createNotebook } = require('../../app/static/js/game/game-notebook.js');
const Labels = require('../../app/static/js/game/game-labels.js');
const { trapGlobals } = require('./support/game-harness.js');

const Vue = loadVue();
const SESSION = 'session-1';
const MARKER = '獨特的記事簿標記X7Q';
const lengthOf = (text) => Array.from(text.normalize('NFC')).length;
/* 伺服器的記事簿檢視（`GET`／`PUT …/notebook` 的 `notebook`）；`full` 與後端一樣是「長度大於容量」。 */
const NB = (text, extra) => {
  const base = { text, version: 4, length: lengthOf(text), capacity: 2000, reserve: 500 };
  const merged = Object.assign(base, extra);
  if (merged.full === undefined) { merged.full = merged.length > merged.capacity; }
  return merged;
};
const SUMMARY = (extra) => Object.assign({ version: 4, length: 0, capacity: 2000, full: false }, extra);
const serverFailure = (status, code, extra) => Object.assign({ kind: 'server', status, code, message: '', fields: {}, categories: [] }, extra);
const transportFailure = (kind) => ({ kind, status: 0, code: '', message: '', fields: {}, categories: [] });
const rejects = (info) => ({ __reject: info });

function deferred() {
  let resolve; let reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

/* 建立被測試的面板與替身 API。`get`／`put` 是依序回應的陣列：值（resolve）、rejects(info)、或 deferred().promise。 */
function setup({ get = [], put = [], summary = null, ended = false, withAbort = false } = {}) {
  const calls = { get: [], put: [] };
  const toasts = [];
  const logins = [];
  const noted = [];
  const endedCalls = [];
  const summaryRef = Vue.ref(summary);
  const endedRef = Vue.ref(ended);
  const respond = (queue) => {
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next instanceof Promise) { return next; }
    return next && next.__reject ? Promise.reject(next.__reject) : Promise.resolve(next);
  };
  const api = {
    notebook: (id, options) => { calls.get.push({ id, options }); return respond(get); },
    saveNotebook: (id, text, version, options) => { calls.put.push({ id, text, version, options }); return respond(put); },
  };
  const target = createNotebook({
    Vue: { ref: Vue.ref, computed: Vue.computed }, api, sessionId: SESSION, summary: summaryRef, ended: endedRef,
    onEnded: () => { endedCalls.push(true); endedRef.value = true; },
    toast: (message) => toasts.push(message), goLogin: () => logins.push(true), noteFailure: (info) => noted.push(info),
    AbortController: withAbort ? AbortController : undefined,
  });
  return { nb: target, calls, toasts, logins, noted, endedCalls, summary: summaryRef, ended: endedRef };
}

/* 讀進記事簿並進入編輯，回傳整套。 */
async function editing(text = '舊的設定', options = {}) {
  const env = setup(Object.assign({ get: [{ notebook: NB(text) }] }, options));
  await env.nb.open();
  assert.equal(env.nb.startEdit(), true);
  return env;
}

// ---- 檢視 --------------------------------------------------------------------------------------------------

test('nothing is requested until the panel is opened, and opening reads the text once for this save', async () => {
  const { nb, calls } = setup({ get: [{ notebook: NB('玩家與 AI 確立的設定') }] });
  await settle();
  assert.equal(calls.get.length, 0);                                              // 建立面板、載入頁面都不請求全文
  assert.equal(nb.notebook.value, null);
  assert.equal(await nb.open(), true);
  assert.equal(calls.get.length, 1);
  assert.equal(calls.get[0].id, SESSION);
  assert.equal(calls.put.length, 0);
  assert.equal(nb.notebook.value.text, '玩家與 AI 確立的設定');
  assert.equal(nb.notebookLoading.value, false);
});

test('opening shows the whole text, the usage "used / capacity" and the reserve note', async () => {
  const text = '第一行\n第二行\n' + 'x'.repeat(1788);
  const { nb } = setup({ get: [{ notebook: NB(text) }] });
  await nb.open();
  assert.equal(nb.notebook.value.text, text);                                     // 全文、換行原樣（顯示靠 CSS 的 pre-wrap）
  assert.equal(nb.notebookUsage.value, `${lengthOf(text)} / 2000`);
  assert.match(nb.notebookReserveNote.value, /預備空間 500 字/);
  assert.match(nb.notebookReserveNote.value, /只供 AI 寫入/);
  assert.equal(nb.notebookEmpty.value, false);
  assert.equal(nb.notebookFull.value, false);
});

test('an empty notebook shows the explanation and usage 0', async () => {
  const { nb } = setup({ get: [{ notebook: NB('') }] });
  await nb.open();
  assert.equal(nb.notebookEmpty.value, true);
  assert.equal(nb.notebookUsage.value, '0 / 2000');
  assert.match(nb.notebookEmptyText, /每完成 10 輪/);
  assert.match(nb.notebookEmptyText, /AI 會/);
});

test('text containing markup is exposed exactly as written, never transformed', async () => {
  const text = '<img src=x onerror="alert(1)"> <script>boom()</script> &amp; ‮';
  const { nb } = setup({ get: [{ notebook: NB(text) }] });
  await nb.open();
  assert.equal(nb.notebook.value.text, text);                                      // 原文字元；樣板用 {{ }} 插值，不解析（樣板守門在 game-template.test.js）
});

test('the usage is a dash until a notebook (or the snapshot summary) is known', () => {
  const { nb } = setup();
  assert.equal(nb.notebookUsage.value, '—');
  assert.equal(nb.notebookReserveNote.value, '');
});

// ---- 已滿 --------------------------------------------------------------------------------------------------

test('full means longer than the capacity: 2240 is full, exactly 2000 is not, 2001 is', async () => {
  for (const [count, expected] of [[0, false], [1999, false], [2000, false], [2001, true], [2240, true]]) {
    const { nb } = setup({ get: [{ notebook: NB('字'.repeat(count)) }] });
    await nb.open();
    assert.equal(nb.notebookFull.value, expected, `${count} 字`);
    assert.equal(nb.notebookUsage.value, `${count} / 2000`);
  }
  assert.match(setup().nb.notebookFullText, /記事簿已滿/);
  assert.match(setup().nb.notebookFullText, /之後的設定不再被記住/);
  assert.match(setup().nb.notebookFullText, /AI 可能會遺忘/);
  assert.match(setup().nb.notebookFullText, /升級會員/);
});

test('a bigger capacity of another tier changes what counts as full', async () => {
  const { nb } = setup({ get: [{ notebook: NB('字'.repeat(2240), { capacity: 5000, reserve: 1250 }) }] });
  await nb.open();
  assert.equal(nb.notebookFull.value, false);
  assert.equal(nb.notebookUsage.value, '2240 / 5000');
});

test('the entrance can tell it is full from the snapshot summary alone, without any request', async () => {
  const { nb, calls, summary } = setup({ summary: SUMMARY({ length: 2240, full: true }) });
  assert.equal(nb.notebookFull.value, true);                                       // 遊戲頁的入口：沒有開面板、沒有請求
  assert.equal(nb.notebookUsage.value, '2240 / 2000');
  assert.equal(calls.get.length, 0);
  summary.value = SUMMARY({ length: 2000, full: false });
  assert.equal(nb.notebookFull.value, false);                                      // 剛好用完容量：不顯示
  summary.value = null;
  assert.equal(nb.notebookFull.value, false);                                      // 不知道時不顯示
});

test('a malformed snapshot summary shows nothing', () => {
  const { nb, summary } = setup();
  for (const bad of [null, undefined, 'x', 3, [], {}, { version: 1 }, { version: 1, length: '2240', capacity: 2000 }]) {
    summary.value = bad;
    assert.equal(nb.notebookFull.value, false);
  }
});

test('the newer of the panel view and the snapshot summary is used, and an older view is marked stale', async () => {
  const { nb, summary } = setup({ get: [{ notebook: NB('舊內容', { version: 4 }) }] });
  await nb.open();
  assert.equal(nb.notebookStale.value, false);
  summary.value = SUMMARY({ version: 5, length: 2240, full: true });               // AI 剛寫入：快照比面板讀到的新
  assert.equal(nb.notebookStale.value, true);
  assert.equal(nb.notebookFull.value, true);
  assert.equal(nb.notebookUsage.value, '2240 / 2000');
  summary.value = SUMMARY({ version: 3, length: 10, full: false });                // 較舊的摘要不蓋過面板的內容
  assert.equal(nb.notebookStale.value, false);
  assert.equal(nb.notebookUsage.value, `${lengthOf('舊內容')} / 2000`);
});

// ---- 編輯 --------------------------------------------------------------------------------------------------

test('starting to edit copies the saved text; there is no editing before a notebook was read, and none for an ended save', async () => {
  const early = setup();
  assert.equal(early.nb.startEdit(), false);
  assert.equal(early.nb.notebookEditing.value, false);

  const { nb } = await editing('舊的設定');
  assert.equal(nb.notebookEditing.value, true);
  assert.equal(nb.notebookDraft.value, '舊的設定');
  assert.equal(nb.notebookCanEdit.value, false);                                    // 已在編輯
  assert.equal(nb.startEdit(), false);

  const done = setup({ get: [{ notebook: NB('x') }], ended: true });
  await done.nb.open();
  assert.equal(done.nb.notebookCanEdit.value, false);                                 // 已完結：沒有「編輯」
  assert.equal(done.nb.startEdit(), false);
  assert.equal(done.nb.notebookEditing.value, false);
});

test('the count is live; over the capacity it blocks saving and says how many characters are over (no reserve for the player)', async () => {
  const { nb } = await editing('舊的設定');
  nb.notebookDraft.value = '字'.repeat(2000);
  assert.equal(nb.notebookDraftCount.value, '2000 / 2000');
  assert.equal(nb.notebookOver.value, false);
  assert.equal(nb.notebookCanSave.value, true);                                       // 剛好用完容量可以存
  assert.equal(nb.notebookOverNote.value, '');
  nb.notebookDraft.value = '字'.repeat(2001);
  assert.equal(nb.notebookDraftCount.value, '2001 / 2000');
  assert.equal(nb.notebookOver.value, true);
  assert.equal(nb.notebookCanSave.value, false);
  assert.match(nb.notebookOverNote.value, /超出容量 1 字/);
  nb.notebookDraft.value = '字'.repeat(2300);                                         // 容量加預備空間以內也不行：預備空間只給 AI
  assert.match(nb.notebookOverNote.value, /超出容量 300 字/);
  assert.equal(nb.notebookCanSave.value, false);
});

test('characters are counted like the backend: NFC first, one per code point', async () => {
  const { nb } = await editing('x');
  nb.notebookDraft.value = 'é'.repeat(3);                                         // e + 組合重音 -> NFC 之後 é（1 字）
  assert.equal(nb.notebookDraftCount.value, '3 / 2000');
  nb.notebookDraft.value = '😀'.repeat(4);                                              // 代理對算 1 字
  assert.equal(nb.notebookDraftCount.value, '4 / 2000');
  nb.notebookDraft.value = '';
  assert.equal(nb.notebookDraftCount.value, '0 / 2000');
});

test('a notebook that is already over the capacity must be cut to the capacity in one go before it can be saved', async () => {
  const { nb } = await editing('字'.repeat(2240));                                       // 2240 / 2000
  assert.match(nb.notebookEditHint.value, /超過容量/);
  assert.match(nb.notebookEditHint.value, /一次刪到容量以內/);
  assert.match(nb.notebookEditHint.value, /AI 才會恢復更新/);
  nb.notebookDraft.value = '字'.repeat(2100);                                           // 刪了一些但還沒到容量
  assert.equal(nb.notebookCanSave.value, false);
  assert.match(nb.notebookOverNote.value, /超出容量 100 字/);
  nb.notebookDraft.value = '字'.repeat(2000);
  assert.equal(nb.notebookCanSave.value, true);
});

test('an unchanged draft cannot be saved; changing it and changing it back cannot either', async () => {
  const { nb } = await editing('舊的設定');
  assert.equal(nb.notebookCanSave.value, false);
  nb.notebookDraft.value = '新的設定';
  assert.equal(nb.notebookCanSave.value, true);
  nb.notebookDraft.value = '舊的設定';
  assert.equal(nb.notebookCanSave.value, false);
});

// ---- 儲存 --------------------------------------------------------------------------------------------------

test('saving sends the draft with the version the edit started from, then shows the response and leaves edit mode with a toast', async () => {
  const saved = NB('新的設定', { version: 5 });
  const { nb, calls, toasts } = await editing('舊的設定', { put: [{ notebook: saved }] });
  nb.notebookDraft.value = '新的設定';
  assert.equal(await nb.save(), true);
  assert.equal(calls.put.length, 1);
  assert.deepEqual(plain({ id: calls.put[0].id, text: calls.put[0].text, version: calls.put[0].version }), { id: SESSION, text: '新的設定', version: 4 });
  assert.equal(nb.notebook.value.text, '新的設定');                                      // 以回應更新全文、用量與版本
  assert.equal(nb.notebook.value.version, 5);
  assert.equal(nb.notebookUsage.value, `${lengthOf('新的設定')} / 2000`);
  assert.equal(nb.notebookEditing.value, false);
  assert.equal(nb.notebookDraft.value, '');                                              // 草稿不留在記憶體
  assert.equal(nb.notebookSaving.value, false);
  assert.deepEqual(toasts, ['記事簿已儲存。']);
  assert.equal(calls.get.length, 1);                                                      // 儲存之後沒有多讀一次
});

test('saving a reduced notebook below the capacity clears the full state', async () => {
  const { nb } = await editing('字'.repeat(2240), { put: [{ notebook: NB('字'.repeat(1800), { version: 5 }) }] });
  assert.equal(nb.notebookFull.value, true);
  nb.notebookDraft.value = '字'.repeat(1800);
  await nb.save();
  assert.equal(nb.notebookFull.value, false);
  assert.equal(nb.notebookUsage.value, '1800 / 2000');
});

test('a second click while the request is running sends only one request, and finally brings the button back', async () => {
  const gate = deferred();
  const { nb, calls } = await editing('舊的設定', { put: [gate.promise] });
  nb.notebookDraft.value = '新的設定';
  const first = nb.save();
  const second = nb.save();
  const third = nb.save();
  assert.equal(nb.notebookSaving.value, true);
  assert.equal(nb.notebookCanSave.value, false);                                          // 按鈕禁用
  assert.equal(calls.put.length, 1);                                                      // 只有一個請求
  assert.equal(await second, false);
  assert.equal(await third, false);
  gate.resolve({ notebook: NB('新的設定', { version: 5 }) });
  assert.equal(await first, true);
  assert.equal(nb.notebookSaving.value, false);
  assert.equal(calls.put.length, 1);
});

test('save() refuses by itself, without any request, when nothing changed, the draft is over the capacity, the save is ended, or no edit is open', async () => {
  const unchanged = await editing('舊的設定', { put: [{ notebook: NB('x') }] });
  assert.equal(await unchanged.nb.save(), false);
  assert.equal(unchanged.calls.put.length, 0);

  const over = await editing('舊的設定', { put: [{ notebook: NB('x') }] });
  over.nb.notebookDraft.value = '字'.repeat(2001);
  assert.equal(await over.nb.save(), false);
  assert.equal(over.calls.put.length, 0);
  assert.equal(over.nb.notebookSaving.value, false);

  const finished = await editing('舊的設定', { put: [{ notebook: NB('x') }] });
  finished.nb.notebookDraft.value = '新的';
  finished.ended.value = true;
  assert.equal(await finished.nb.save(), false);
  assert.equal(finished.calls.put.length, 0);

  const reading = setup({ get: [{ notebook: NB('舊的設定') }], put: [{ notebook: NB('x') }] });
  await reading.nb.open();
  assert.equal(await reading.nb.save(), false);                                              // 沒有進入編輯
  assert.equal(reading.calls.put.length, 0);
});

test('every failure restores the button and keeps the draft: network, timeout, 400, 429, 500, 503', async () => {
  const failures = [transportFailure('network'), transportFailure('timeout'), serverFailure(400, 'invalid'), serverFailure(429, 'rate_limited'),
    serverFailure(500, 'server_error'), serverFailure(503, 'busy')];
  for (const info of failures) {
    const { nb, calls, noted } = await editing('舊的設定', { put: [rejects(info)] });
    nb.notebookDraft.value = `${MARKER}新的設定`;
    assert.equal(await nb.save(), false, JSON.stringify(info));
    assert.equal(nb.notebookSaving.value, false);                                          // finally 恢復
    assert.equal(nb.notebookEditing.value, true);
    assert.equal(nb.notebookDraft.value, `${MARKER}新的設定`);                                // 編輯內容還在
    assert.equal(nb.notebookCanSave.value, true);
    assert.ok(nb.notebookNotice.value && nb.notebookNotice.value.text, JSON.stringify(info));
    assert.ok(!nb.notebookNotice.value.text.includes(MARKER));                              // 訊息不含記事簿內容
    assert.equal(calls.put.length, 1);
    assert.deepEqual(plain(noted), [info]);                                                 // 每個失敗都交給受限狀態的辨識
  }
});

test('a throwing API is handled like a failure, and the button is restored', async () => {
  const throwing = createNotebook({
    Vue: { ref: Vue.ref, computed: Vue.computed }, sessionId: SESSION,
    api: { notebook: () => Promise.resolve({ notebook: NB('舊的設定') }), saveNotebook: () => { throw new Error('boom'); } },
  });
  await throwing.open();
  throwing.startEdit();
  throwing.notebookDraft.value = '新的設定';
  assert.equal(await throwing.save(), false);
  assert.equal(throwing.notebookSaving.value, false);
  assert.equal(throwing.notebookDraft.value, '新的設定');
});

test('a version conflict shows the latest notebook, keeps the draft for comparison, and the next save carries the latest version', async () => {
  const latest = NB('AI 剛寫入的新設定', { version: 6 });
  const { nb, calls } = await editing('舊的設定', {
    put: [rejects(serverFailure(409, 'notebook_conflict', { notebook: latest })), { notebook: NB(`${MARKER}我的版本`, { version: 7 }) }],
  });
  nb.notebookDraft.value = `${MARKER}我的版本`;
  assert.equal(await nb.save(), false);
  assert.equal(nb.notebook.value.text, 'AI 剛寫入的新設定');                                 // 以後端最新更新全文、用量與版本
  assert.equal(nb.notebook.value.version, 6);
  assert.equal(nb.notebookUsage.value, `${lengthOf('AI 剛寫入的新設定')} / 2000`);
  assert.equal(nb.notebookConflict.value.text, 'AI 剛寫入的新設定');                          // 供對照
  assert.equal(nb.notebookDraft.value, `${MARKER}我的版本`);                                 // 玩家的編輯內容仍可取得
  assert.equal(nb.notebookEditing.value, true);
  assert.match(nb.notebookNotice.value.text, /AI 或另一個裝置剛更新了記事簿/);
  assert.match(nb.notebookNotice.value.text, /還保留著/);
  assert.equal(nb.notebookSaving.value, false);
  assert.equal(calls.get.length, 1);                                                          // 不另外重讀、不自動合併、不自動重送
  assert.equal(calls.put.length, 1);
  assert.equal(await nb.save(), true);                                                        // 玩家看過之後明確地再按一次
  assert.equal(calls.put[1].version, 6);
  assert.equal(calls.put[1].text, `${MARKER}我的版本`);
  assert.equal(nb.notebookConflict.value, null);
});

test('a conflict without a usable notebook in the body reads the latest again and keeps the draft', async () => {
  const { nb, calls } = await editing('舊的設定', {
    get: [{ notebook: NB('舊的設定', { version: 4 }) }, { notebook: NB('重讀到的最新', { version: 9 }) }],
    put: [rejects(serverFailure(409, 'something_else'))],
  });
  nb.notebookDraft.value = '我的版本';
  await nb.save();
  assert.equal(calls.get.length, 2);
  assert.equal(nb.notebook.value.text, '重讀到的最新');
  assert.equal(nb.notebookDraft.value, '我的版本');
  assert.equal(nb.notebookConflict.value.version, 9);
});

test('422 too long shows the lengths and takes the capacity the server says; the message has no notebook content', async () => {
  const { nb } = await editing('舊的設定', {
    put: [rejects(serverFailure(422, 'notebook_too_long', { length: 2100, capacity: 1800, message: `${MARKER}伺服器訊息` }))],
  });
  nb.notebookDraft.value = `${MARKER}${'字'.repeat(1900)}`;
  await nb.save();
  assert.match(nb.notebookNotice.value.text, /2100/);
  assert.match(nb.notebookNotice.value.text, /1800/);
  assert.ok(!nb.notebookNotice.value.text.includes(MARKER));
  assert.equal(nb.notebook.value.capacity, 1800);                                              // 剛降級：以伺服器說的容量為準
  assert.equal(nb.notebookOver.value, true);
  assert.equal(nb.notebookEditing.value, true);
});

test('422 from the rule layer shows only the category name, not which part of the text', async () => {
  const { nb } = await editing('舊的設定', {
    put: [rejects(serverFailure(422, 'content_rejected', { categories: ['minor_sexual'], message: `${MARKER}第 3 行有問題` }))],
  });
  nb.notebookDraft.value = `${MARKER}不能存的內容`;
  await nb.save();
  const text = nb.notebookNotice.value.text;
  assert.ok(text.includes(Labels.CATEGORY_LABELS.minor_sexual));
  assert.ok(!text.includes(MARKER) && !text.includes('第 3 行'));
  assert.equal(nb.notebookDraft.value, `${MARKER}不能存的內容`);
});

test('422 with an unknown category falls back to the fixed sentence', async () => {
  const { nb } = await editing('舊的設定', { put: [rejects(serverFailure(422, 'content_rejected', { categories: ['not_a_category'] }))] });
  nb.notebookDraft.value = '新的';
  await nb.save();
  assert.equal(nb.notebookNotice.value.text, Labels.notebookText('rejected'));
});

test('409 session_ended marks the save as ended, turns the panel read-only and leaves the draft readable', async () => {
  const { nb, endedCalls, ended } = await editing('舊的設定', { put: [rejects(serverFailure(409, 'session_ended'))] });
  nb.notebookDraft.value = `${MARKER}還沒存的內容`;
  assert.equal(await nb.save(), false);
  assert.equal(endedCalls.length, 1);
  assert.equal(ended.value, true);
  assert.equal(nb.notebookLocked.value, true);                                                 // 輸入框唯讀
  assert.equal(nb.notebookCanSave.value, false);
  assert.equal(nb.notebookCanEdit.value, false);
  assert.equal(nb.notebookDraft.value, `${MARKER}還沒存的內容`);                                // 草稿留著供複製
  assert.match(nb.notebookNotice.value.text, /已經完結/);
  assert.equal(await nb.save(), false);                                                        // 之後不再送出
});

test('404 says the save is gone, and 401 sends the player to the login page; neither loses the draft', async () => {
  const gone = await editing('舊的設定', { put: [rejects(serverFailure(404, 'not_found'))] });
  gone.nb.notebookDraft.value = '新的';
  await gone.nb.save();
  assert.match(gone.nb.notebookNotice.value.text, /找不到這個存檔/);
  assert.equal(gone.nb.notebookDraft.value, '新的');
  assert.deepEqual(gone.logins, []);

  const expired = await editing('舊的設定', { put: [rejects(serverFailure(401, 'unauthorized'))] });
  expired.nb.notebookDraft.value = '新的';
  await expired.nb.save();
  assert.deepEqual(expired.logins, [true]);
  assert.equal(expired.nb.notebookDraft.value, '新的');
  assert.equal(expired.nb.notebookSaving.value, false);
});

test('a cancelled request is silent: no notice, no toast, the draft stays', async () => {
  const { nb, toasts } = await editing('舊的設定', { put: [rejects(transportFailure('canceled'))] });
  nb.notebookDraft.value = '新的';
  assert.equal(await nb.save(), false);
  assert.equal(nb.notebookNotice.value, null);
  assert.deepEqual(toasts, []);
  assert.equal(nb.notebookDraft.value, '新的');
  assert.equal(nb.notebookSaving.value, false);
});

test('a success response that is not a notebook still counts as saved and re-reads the notebook from the server', async () => {
  const { nb, calls, toasts } = await editing('舊的設定', {
    get: [{ notebook: NB('舊的設定', { version: 4 }) }, { notebook: NB('伺服器上的內容', { version: 5 }) }],
    put: [{ ok: true }],
  });
  nb.notebookDraft.value = '新的';
  assert.equal(await nb.save(), true);
  assert.equal(calls.get.length, 2);
  assert.equal(nb.notebook.value.text, '伺服器上的內容');
  assert.equal(nb.notebookEditing.value, false);
  assert.deepEqual(toasts, ['記事簿已儲存。']);
});

// ---- 取消與未儲存 ------------------------------------------------------------------------------------------

test('cancelling without changes leaves edit mode at once; with changes it asks first and discards only after the confirmation', async () => {
  const clean = await editing('舊的設定');
  assert.equal(clean.nb.cancelEdit(), true);
  assert.equal(clean.nb.notebookEditing.value, false);
  assert.equal(clean.nb.notebookDiscardOpen.value, false);

  const { nb, calls } = await editing('舊的設定');
  nb.notebookDraft.value = '改到一半';
  assert.equal(nb.cancelEdit(), true);
  assert.equal(nb.notebookDiscardOpen.value, true);                                            // 自製 Modal 確認
  assert.equal(nb.notebookEditing.value, true);                                                // 還沒丟棄
  assert.equal(nb.notebookDraft.value, '改到一半');
  assert.equal(nb.confirmDiscard(), true);
  assert.equal(nb.notebookDiscardOpen.value, false);
  assert.equal(nb.notebookEditing.value, false);
  assert.equal(nb.notebookDraft.value, '');
  assert.equal(nb.notebook.value.text, '舊的設定');                                              // 顯示的仍是已儲存的內容
  assert.equal(calls.put.length, 0);                                                          // 丟棄不送任何請求
});

test('choosing to keep editing in the confirmation (modal closed) changes nothing', async () => {
  const { nb } = await editing('舊的設定');
  nb.notebookDraft.value = '改到一半';
  nb.cancelEdit();
  nb.notebookDiscardOpen.value = false;                                                         // 「繼續編輯」：v-model 寫回 false
  assert.equal(nb.notebookEditing.value, true);
  assert.equal(nb.notebookDraft.value, '改到一半');
});

test('cancelling while a save is running is ignored', async () => {
  const gate = deferred();
  const { nb } = await editing('舊的設定', { put: [gate.promise] });
  nb.notebookDraft.value = '新的';
  const saving = nb.save();
  assert.equal(nb.cancelEdit(), false);
  assert.equal(nb.confirmDiscard(), false);
  assert.equal(nb.notebookEditing.value, true);
  gate.resolve({ notebook: NB('新的', { version: 5 }) });
  await saving;
});

// ---- 重新開啟與讀取失敗 -------------------------------------------------------------------------------------

test('every opening reads the latest text, but reopening while editing never overwrites the draft or the version', async () => {
  const { nb, calls } = setup({ get: [{ notebook: NB('第一次', { version: 4 }) }, { notebook: NB('第二次', { version: 5 }) }] });
  await nb.open();
  assert.equal(nb.notebook.value.text, '第一次');
  await nb.open();
  assert.equal(calls.get.length, 2);
  assert.equal(nb.notebook.value.text, '第二次');
  nb.startEdit();
  nb.notebookDraft.value = '編輯中';
  assert.equal(await nb.open(), false);
  assert.equal(calls.get.length, 2);                                                           // 編輯中不重讀
  assert.equal(nb.notebookDraft.value, '編輯中');
});

test('concurrent openings share one request', async () => {
  const gate = deferred();
  const { nb, calls } = setup({ get: [gate.promise] });
  const first = nb.open();
  const second = nb.open();
  assert.equal(nb.notebookLoading.value, true);
  assert.equal(calls.get.length, 1);
  gate.resolve({ notebook: NB('內容') });
  assert.equal(await first, true);
  assert.equal(await second, true);
  assert.equal(nb.notebookLoading.value, false);
});

test('read failures show a fixed message with retry (or a link to leave), 401 goes to login, and the loading flag is restored', async () => {
  const net = setup({ get: [rejects(transportFailure('network'))] });
  assert.equal(await net.nb.open(), false);
  assert.match(net.nb.notebookError.value.message, /連線不穩/);
  assert.equal(net.nb.notebookError.value.retry, true);
  assert.equal(net.nb.notebookLoading.value, false);

  const gone = setup({ get: [rejects(serverFailure(404, 'not_found'))] });
  await gone.nb.open();
  assert.match(gone.nb.notebookError.value.message, /找不到這個存檔/);
  assert.equal(gone.nb.notebookError.value.retry, false);
  assert.equal(gone.nb.notebookError.value.link.href, '/game');

  const expired = setup({ get: [rejects(serverFailure(401, 'unauthorized'))] });
  await expired.nb.open();
  assert.deepEqual(expired.logins, [true]);
  assert.equal(expired.nb.notebookError.value, null);

  const gate = setup({ get: [rejects(serverFailure(403, 'account_locked'))] });
  await gate.nb.open();
  assert.equal(gate.nb.notebookError.value.retry, false);
  assert.deepEqual(plain(gate.noted), [serverFailure(403, 'account_locked')]);

  const retry = setup({ get: [rejects(transportFailure('timeout')), { notebook: NB('回來了') }] });
  await retry.nb.open();
  assert.ok(retry.nb.notebookError.value);
  assert.equal(await retry.nb.open(), true);
  assert.equal(retry.nb.notebookError.value, null);                                            // 重試成功：錯誤消失
  assert.equal(retry.nb.notebook.value.text, '回來了');
});

test('a cancelled read is silent, and a response that is not a notebook is "temporarily unavailable"', async () => {
  const canceled = setup({ get: [rejects(transportFailure('canceled'))] });
  assert.equal(await canceled.nb.open(), false);
  assert.equal(canceled.nb.notebookError.value, null);
  for (const bad of [undefined, null, {}, { notebook: null }, { notebook: { text: 3, version: 1 } }, { notebook: { text: 'x' } }]) {
    const { nb } = setup({ get: [bad] });
    assert.equal(await nb.open(), false);
    assert.equal(nb.notebook.value, null);
    assert.equal(nb.notebookError.value.message, Labels.notebookText('unavailable'));
  }
});

// ---- 不自動儲存、離開頁面與隱私 -----------------------------------------------------------------------------

test('nothing is ever saved by itself: not while editing, not when the page is left, not when the sidebar logic reopens the panel', async () => {
  const { nb, calls } = await editing('舊的設定', { withAbort: true });
  nb.notebookDraft.value = '編輯到一半';
  await settle(30);
  nb.dispose();                                                                                // 離開頁面（pagehide）
  await nb.open();
  await settle(30);
  assert.equal(calls.put.length, 0);
  assert.equal(nb.notebookDraft.value, '編輯到一半');
});

test('leaving the page aborts a read and a save that are still running', async () => {
  const readGate = deferred();
  const reading = setup({ get: [readGate.promise], withAbort: true });
  reading.nb.open();
  assert.equal(reading.calls.get[0].options.signal.aborted, false);
  reading.nb.dispose();
  assert.equal(reading.calls.get[0].options.signal.aborted, true);

  const saveGate = deferred();
  const saving = await editing('舊的設定', { put: [saveGate.promise], withAbort: true });
  saving.nb.notebookDraft.value = '新的';
  saving.nb.save();
  assert.equal(saving.calls.put[0].options.signal.aborted, false);
  saving.nb.dispose();
  assert.equal(saving.calls.put[0].options.signal.aborted, true);
  readGate.resolve({ notebook: NB('x') });
  saveGate.resolve({ notebook: NB('x') });
});

test('the module touches no browser storage, console, timer or document, and the notebook text never goes anywhere but the two notebook calls', async () => {
  const source = fs.readFileSync(path.join(ROOT, 'app/static/js/game/game-notebook.js'), 'utf8');
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');                // 註解不算
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'document', 'window', 'console', 'setTimeout', 'setInterval', 'fetch(', 'axios', 'XMLHttpRequest']) {
    assert.ok(!code.includes(word), word);
  }
  assert.ok(!/v-html|innerHTML/.test(code));
  const calls = [...code.matchAll(/\bapi\.(\w+)\(/g)].map((match) => match[1]).sort();
  assert.deepEqual([...new Set(calls)], ['notebook', 'saveNotebook']);                          // 只有這兩個呼叫
});

test('a whole edit session (read, conflict, failure, save) leaves the console and every browser storage untouched', async () => {
  const trap = trapGlobals();
  try {
    const { nb } = await editing(`${MARKER}舊`, {
      put: [rejects(serverFailure(409, 'notebook_conflict', { notebook: NB(`${MARKER}新`, { version: 6 }) })), rejects(transportFailure('network')),
        { notebook: NB(`${MARKER}存好`, { version: 7 }) }],
    });
    nb.notebookDraft.value = `${MARKER}我的`;
    await nb.save();
    await nb.save();
    await nb.save();
    assert.equal(nb.notebook.value.text, `${MARKER}存好`);
  } finally {
    trap.restore();
  }
  assert.deepEqual(trap.logs, []);                                                              // 記事簿全文不進主控台
  assert.deepEqual(trap.writes, []);                                                            // 也不進 localStorage、sessionStorage、indexedDB
});
