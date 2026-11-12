'use strict';
/* rpg-game-screen、rpg-stream-client、rpg-game-sync、rpg-wallet-ui、rpg-notebook-panel 規格的純邏輯（game-state.js）：
 * 串流事件到畫面指令的轉換、時間軸與合併、各種失敗的繁體中文對照（涵蓋規格列出的每個狀態碼與代碼）、世界狀態與通知的白名單、
 * 記事簿字數與後端一致、送出條件。**錯誤訊息不含任何傳入的玩家文字**，被擋與中斷的回合不含任何文字。純函式，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const JS_DIR = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game');
const FILE = path.join(JS_DIR, 'game-state.js');
const State = require(FILE);
const Labels = require(path.join(JS_DIR, 'game-labels.js'));
const LENGTH_CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'notebook-length-cases.json'), 'utf8'));

const HAN = /[一-鿿]/;
/* 「玩家文字」的標記：純 ASCII，用來證明伺服器送來的文字不會被原樣顯示。
 * 注意：不認得的 409 與 400 的逐欄位說明，伺服器的**中文固定訊息**會被顯示（規格：「其餘版本衝突依後端訊息」），
 * 過濾條件是 trusted()（含中文、無角括號與控制字元、不超過 120 字）；後端的固定訊息本來就不含玩家文字。這兩處另有專門的案例。 */
const MARKER = 'ZZPLAYER_SECRET_ZZ';
const NO_CHARGE = '沒有扣除遊戲幣';

/* 伺服器的錯誤本文形狀（controllers/errors.py）：`error` 是 { code, message, fields? }，其餘欄位與 `error` 並列。 */
const body = (code, message, extra) => Object.assign({ error: Object.assign({ code, message }, extra && extra.fields ? { fields: extra.fields } : {}) }, extra && extra.top);
/* Auth.describeError 整理過的失敗，再加上 game-api 放進來的欄位。 */
const info = (status, code, extra) => Object.assign({ kind: 'server', status, code, message: '伺服器的訊息', fields: {}, categories: [] }, extra);

function assertFixedMessage(failure, label) {
  assert.equal(typeof failure.message, 'string', label);
  assert.ok(!failure.message.includes(MARKER), `${label}: 玩家文字不可出現在訊息裡`);
  assert.ok(!/undefined|null|NaN|\[object/.test(failure.message), `${label}: ${failure.message}`);
}

// ---- 字數：與後端 notebook_policy.length_of 一致 ----------------------------------------------------

test('notebookLength gives the same count as the backend for every golden case (the same cases are checked against the backend by pytest)', () => {
  assert.ok(LENGTH_CASES.length >= 15);
  for (const item of LENGTH_CASES) {
    assert.equal(State.notebookLength(item.text), item.length, item.name);
  }
});

test('notebookLength counts NFC code points: a combining mark merges, an emoji is one, control characters are counted', () => {
  assert.equal(State.notebookLength('é'), 1);
  assert.equal(State.notebookLength('é'), 1);
  assert.equal(State.notebookLength('😀'), 1);
  assert.equal('😀'.length, 2);                                            // JavaScript 的 length 與後端不同，所以不能直接用
  assert.equal(State.notebookLength('a\nb'), 3);
  assert.equal(State.notebookLength('a\u0000b'), 3);
});

test('notebookLength of anything that is not a string is 0', () => {
  for (const junk of [undefined, null, 5, {}, [], true, '']) { assert.equal(State.notebookLength(junk), 0, String(junk)); }
});

test('exactly the capacity is not over it, one more is (the boundary the full notice depends on)', () => {
  assert.equal(State.notebookLength('字'.repeat(2000)), 2000);
  assert.equal(State.notebookLength('字'.repeat(2001)), 2001);
});

// ---- 串流開始前的失敗 --------------------------------------------------------------------------------

const STREAM_START_CASES = [
  // [status, code, 預期 kind]
  [400, 'validation_error', 'fields'],
  [401, 'unauthorized', 'login'],
  [403, 'account_locked', 'gate'],
  [403, 'age_confirmation_required', 'gate'],
  [403, 'age_restricted', 'gate'],
  [403, 'account_deletion_pending', 'gate'],
  [403, 'content_tier_restricted', 'gate'],
  [403, 'some_new_gate', 'gate'],
  [403, 'csrf_failed', 'csrf'],
  [404, 'not_found', 'not_found'],
  [409, 'insufficient_coins', 'coins'],
  [409, 'turn_in_progress', 'busy'],
  [409, 'opening_in_progress', 'opening_wait'],
  [409, 'opening_pending', 'opening_pending'],
  [409, 'session_ended', 'ended'],
  [409, 'message_id_reused', 'conflict'],
  [409, 'something_new', 'conflict'],
  [422, 'input_blocked', 'rejected'],
  [429, 'too_many_requests', 'limit'],
  [503, 'service_unavailable', 'unavailable'],
  [503, 'system_busy', 'unavailable'],
  [503, 'wallet_busy', 'unavailable'],
  [500, 'error', 'unknown'],
  [502, '', 'unknown'],
];

for (const [status, code, kind] of STREAM_START_CASES) {
  test(`describeHttpFailure(${status}, ${code || 'no code'}) is "${kind}" with a fixed Traditional Chinese message`, () => {
    const failure = State.describeHttpFailure(status, body(code, MARKER));
    assert.equal(failure.kind, kind);
    assert.equal(failure.status, status);
    assert.ok(HAN.test(failure.message));
    assertFixedMessage(failure, `${status} ${code}`);
    if (kind !== 'login' && kind !== 'csrf' && kind !== 'opening_wait' && kind !== 'ended' && kind !== 'not_found') {
      assert.ok(failure.message.includes(NO_CHARGE), `開始前的失敗要說明沒有扣除遊戲幣：${failure.message}`);
    }
  });
}

test('an insufficient-coins failure carries the balance and the price as integers only', () => {
  const failure = State.describeHttpFailure(409, body('insufficient_coins', '遊戲幣不足', { top: { balance: 10, needed: 30 } }));
  assert.equal(failure.kind, 'coins');
  assert.equal(failure.balance, 10);
  assert.equal(failure.needed, 30);
  const odd = State.describeHttpFailure(409, body('insufficient_coins', '', { top: { balance: '10', needed: -1 } }));
  assert.equal(odd.balance, null);
  assert.equal(odd.needed, null);
});

test('the failures that need the screen to react carry the matching flags', () => {
  assert.equal(State.describeHttpFailure(409, body('turn_in_progress', '')).catchUp, true);          // 稍後以 since_seq 補齊
  assert.equal(State.describeHttpFailure(409, body('opening_in_progress', '')).retry, true);          // 等待後重讀快照
  assert.equal(State.describeHttpFailure(409, body('session_ended', '')).ended, true);
  assert.equal(State.describeHttpFailure(409, body('opening_pending', '')).reload, true);
  assert.equal(State.describeHttpFailure(409, body('something_new', '')).reload, true);
  assert.equal(State.describeHttpFailure(503, body('system_busy', '')).retry, true);
  assert.equal(State.describeHttpFailure(404, body('not_found', '')).link.href, '/game');
  assert.equal(State.describeHttpFailure(403, body('account_locked', '')).link.href, '/appeal');
  assert.equal(State.describeHttpFailure(403, body('age_confirmation_required', '')).link.href, '/account');
  assert.equal(State.describeHttpFailure(403, body('age_restricted', '')).link, null);               // 補填也沒有用，所以不引導
  assert.equal(State.describeHttpFailure(401, body('unauthorized', '')).kind, 'login');
});

test('a known code never shows the server message; an unknown 409 shows it only when it is a short Chinese fixed text', () => {
  const known = State.describeHttpFailure(409, body('turn_in_progress', '另外一段伺服器文字'));
  assert.ok(!known.message.includes('另外一段伺服器文字'));
  const fine = State.describeHttpFailure(409, body('something_new', '目前無法處理，請稍後再試。'));
  assert.ok(fine.message.includes('目前無法處理，請稍後再試。'));
  for (const bad of [`<b>${MARKER}</b> 請稍後`, 'English only message', '字'.repeat(500), '帶換行\n的訊息', '']) {
    const failure = State.describeHttpFailure(409, body('something_new', bad));
    assert.ok(!failure.message.includes('English only') && !failure.message.includes('<b>') && !failure.message.includes(MARKER) && !failure.message.includes('\n'), JSON.stringify(bad));
    assert.ok(HAN.test(failure.message));
  }
});

test('a 400 shows per-field explanations, but only trusted ones (short, Chinese, no markup)', () => {
  const failure = State.describeHttpFailure(400, body('validation_error', '', { fields: {
    action: '行動最多 1000 字。', client_msg_id: '必須是 1 至 64 字的文字。', evil: `<script>${MARKER}</script>`, english: 'too long', 'Bad Key': '不合法的鍵', long: '字'.repeat(300),
  } }));
  assert.equal(failure.kind, 'fields');
  assert.deepEqual(Object.keys(failure.fields).sort(), ['action', 'client_msg_id']);
  assert.equal(failure.fields.action, '行動最多 1000 字。');
});

test('a 422 shows only the Chinese names of known review categories, never the raw identifiers or the unknown ones', () => {
  const failure = State.describeHttpFailure(422, body('input_blocked', '', { top: { categories: ['minor_sexual', 'mystery_category', MARKER] } }));
  assert.equal(failure.kind, 'rejected');
  assert.ok(failure.message.includes(Labels.CATEGORY_LABELS.minor_sexual));
  assert.ok(!failure.message.includes('minor_sexual') && !failure.message.includes('mystery_category') && !failure.message.includes(MARKER));
  const none = State.describeHttpFailure(422, body('input_blocked', ''));
  assert.ok(!none.message.includes('（'));
});

test('a missing or broken body still gives a fixed failure for any status, and status 0 is a network failure', () => {
  for (const junk of [null, undefined, 'text', 5, [], {}, { error: 'x' }, { error: { code: 5, message: {} } }]) {
    for (const status of [400, 401, 403, 404, 409, 422, 429, 500, 502, 503]) {
      const failure = State.describeHttpFailure(status, junk);
      assert.ok(HAN.test(failure.message), `${status} ${JSON.stringify(junk)}`);
      assert.equal(failure.status, status);
    }
  }
  assert.equal(State.describeHttpFailure(0, null).kind, 'network');
});

test('describeHttpFailure never echoes a code that is not a plain lowercase identifier', () => {
  const failure = State.describeHttpFailure(409, body('<script>alert(1)</script>', '目前無法處理，請稍後再試。'));
  assert.equal(failure.code, '');
});

// ---- 其他請求的失敗（快照、回合清單、錢包…）---------------------------------------------------------

test('describeRequestFailure does not claim anything about coins (no charge happens for a read)', () => {
  for (const status of [400, 403, 404, 409, 429, 500, 503]) {
    assert.ok(!State.describeRequestFailure(info(status, 'x')).message.includes(NO_CHARGE), String(status));
  }
});

test('describeRequestFailure handles the connection-level failures from Auth.describeError', () => {
  const canceled = State.describeRequestFailure({ kind: 'canceled', status: 0, code: 'canceled' });
  assert.equal(canceled.kind, 'canceled');
  assert.equal(canceled.silent, true);
  assert.equal(canceled.message, '');
  const timeout = State.describeRequestFailure({ kind: 'timeout', status: 0, code: 'timeout' });
  assert.equal(timeout.kind, 'timeout');
  assert.equal(timeout.retry, true);
  const network = State.describeRequestFailure({ kind: 'network', status: 0, code: 'network' });
  assert.equal(network.kind, 'network');
  assert.equal(network.retry, true);
  const csrf = State.describeRequestFailure({ kind: 'csrf', status: 403, code: 'csrf_failed' });
  assert.equal(csrf.kind, 'csrf');
  assert.ok(csrf.message.includes('重新整理'));
  assert.equal(State.describeRequestFailure(null).kind, 'network');
});

test('a snapshot 404 is not_found and points back to the adventure list', () => {
  const failure = State.describeRequestFailure(info(404, 'not_found'));
  assert.equal(failure.kind, 'not_found');
  assert.equal(failure.link.href, '/game');
});

// ---- 串流結束的失敗 ----------------------------------------------------------------------------------

test('a finished stream has no failure', () => {
  assert.equal(State.describeStreamFailure({ outcome: 'done', done: {}, failure: null }), null);
});

test('interrupted and both timeouts remind the player the turn may have finished on the server, and ask for a catch-up', () => {
  for (const outcome of ['interrupted', 'timeout_idle', 'timeout_total']) {
    const failure = State.describeStreamFailure({ outcome, done: null, failure: null });
    assert.ok(failure.message.includes('可能已在伺服器完成'), outcome);
    assert.equal(failure.catchUp, true, outcome);
    assert.equal(failure.silent, false, outcome);
    assert.equal(failure.kind, outcome === 'interrupted' ? 'interrupted' : 'timeout', outcome);
    assertFixedMessage(failure, outcome);
  }
  assert.notEqual(State.describeStreamFailure({ outcome: 'timeout_idle' }).message, State.describeStreamFailure({ outcome: 'timeout_total' }).message);
});

test('a cancel is silent (not an error) but still asks for a catch-up', () => {
  const failure = State.describeStreamFailure({ outcome: 'aborted', done: null, failure: null });
  assert.equal(failure.kind, 'canceled');
  assert.equal(failure.silent, true);
  assert.equal(failure.catchUp, true);
  assert.equal(failure.message, '');
});

test('each error-event code maps to its fixed message; the server text is never shown', () => {
  for (const code of Object.keys(Labels.STREAM_ERROR_MESSAGES)) {
    const failure = State.describeStreamFailure({ outcome: 'error_event', failure: { kind: 'error_event', code, message: `${MARKER} 伺服器文字` } });
    assert.equal(failure.kind, 'stream_error');
    assert.equal(failure.message, Labels.STREAM_ERROR_MESSAGES[code]);
    assertFixedMessage(failure, code);
  }
  const unknown = State.describeStreamFailure({ outcome: 'error_event', failure: { code: 'brand_new', message: `${MARKER}` } });
  assert.equal(unknown.message, Labels.streamErrorMessage('brand_new'));
  assert.ok(!unknown.message.includes(MARKER) && !unknown.message.includes('brand_new'));
  assert.equal(State.describeStreamFailure({ outcome: 'error_event', failure: { code: 'turn_superseded' } }).catchUp, true);
  assert.equal(State.describeStreamFailure({ outcome: 'error_event', failure: { code: 'generation_failed' } }).retry, true);
});

test('a rejected stream hands back the describeHttpFailure result; a config problem becomes a reload message', () => {
  const given = State.describeHttpFailure(409, body('insufficient_coins', '', { top: { balance: 5, needed: 30 } }));
  assert.deepEqual(State.describeStreamFailure({ outcome: 'rejected', failure: given }), given);
  for (const junk of [null, undefined, {}, { kind: 'config' }, { kind: 'something_else' }, 'x']) {
    const failure = State.describeStreamFailure({ outcome: 'rejected', failure: junk });
    assert.ok(HAN.test(failure.message) && failure.reload === true, JSON.stringify(junk));
  }
});

test('an unknown or missing outcome is treated as an interruption (fail closed, ask for a catch-up)', () => {
  for (const junk of [undefined, null, 'mystery', 5]) {
    const failure = State.describeStreamFailure(junk === undefined ? undefined : { outcome: junk });
    assert.equal(failure.kind, 'interrupted');
    assert.equal(failure.catchUp, true);
  }
});

// ---- 記事簿的失敗 ------------------------------------------------------------------------------------

test('a notebook conflict returns the backend\'s latest notebook (whitelisted), keeps the draft, and explains', () => {
  const failure = State.describeNotebookFailure(info(409, 'notebook_conflict', {
    notebook: { text: '後端最新的記事簿內容', version: 7, length: 10, capacity: 2000, reserve: 500, full: false, extra_secret: MARKER },
  }));
  assert.equal(failure.kind, 'conflict');
  assert.equal(failure.keepDraft, true);
  assert.deepEqual(failure.notebook, { text: '後端最新的記事簿內容', version: 7, length: 10, capacity: 2000, reserve: 500, full: false });
  assert.equal(failure.message, Labels.notebookText('conflict'));
  const broken = State.describeNotebookFailure(info(409, 'notebook_conflict', { notebook: { text: 5, version: 'x' } }));
  assert.equal(broken.notebook, null);
});

test('a notebook that is too long shows the two integers from the server and nothing else', () => {
  const failure = State.describeNotebookFailure(info(422, 'notebook_too_long', { length: 2010, capacity: 2000, message: `${MARKER}` }));
  assert.equal(failure.kind, 'too_long');
  assert.equal(failure.message, '記事簿超過容量（2010 / 2000 字），請刪減到容量以內再儲存。');
  assert.equal(failure.length, 2010);
  assert.equal(failure.capacity, 2000);
  assert.equal(failure.keepDraft, true);
  const odd = State.describeNotebookFailure(info(422, 'notebook_too_long', { length: '2010', capacity: -1 }));
  assert.ok(!/\d/.test(odd.message));
});

test('a rule-layer hit shows only the Chinese category names, never where or what', () => {
  const failure = State.describeNotebookFailure(info(422, 'notebook_blocked', { categories: ['non_consensual', 'whatever', MARKER] }));
  assert.equal(failure.kind, 'rejected');
  assert.ok(failure.message.includes(Labels.CATEGORY_LABELS.non_consensual));
  assert.ok(!failure.message.includes('non_consensual') && !failure.message.includes('whatever') && !failure.message.includes(MARKER));
  assert.equal(failure.keepDraft, true);
  assert.equal(State.describeNotebookFailure(info(422, 'notebook_blocked')).message, Labels.notebookText('rejected'));
});

test('a notebook save when the save has just ended becomes read-only', () => {
  const failure = State.describeNotebookFailure(info(409, 'session_ended'));
  assert.equal(failure.kind, 'ended');
  assert.equal(failure.ended, true);
  assert.equal(failure.message, Labels.notebookText('ended'));
});

test('every other notebook failure has a fixed message and keeps the draft', () => {
  const cases = [
    [info(401, 'unauthorized'), 'login'], [info(403, 'account_locked'), 'gate'], [info(404, 'not_found'), 'not_found'], [info(400, 'validation_error'), 'fields'],
    [info(429, 'too_many_requests'), 'limit'], [info(503, 'service_unavailable'), 'unavailable'], [info(500, 'error'), 'unknown'], [info(409, 'weird'), 'conflict'],
    [{ kind: 'network', status: 0, code: 'network' }, 'network'], [{ kind: 'timeout', status: 0, code: 'timeout' }, 'timeout'], [{ kind: 'csrf', status: 403, code: 'csrf_failed' }, 'csrf'],
  ];
  for (const [input, kind] of cases) {
    const failure = State.describeNotebookFailure(Object.assign(input, { message: MARKER }));
    assert.equal(failure.kind, kind);
    assert.equal(failure.keepDraft, true, kind);
    assert.ok(HAN.test(failure.message), kind);
    assertFixedMessage(failure, kind);
  }
  assert.equal(State.describeNotebookFailure({ kind: 'canceled', status: 0, code: 'canceled' }).silent, true);
});

test('a network failure while saving says the edit is still there', () => {
  assert.ok(State.describeNotebookFailure({ kind: 'network', status: 0 }).message.includes('你編輯的內容還在'));
});

// ---- 簽到的失敗 --------------------------------------------------------------------------------------

test('a check-in overflow carries the preview as integers and a notice with both numbers', () => {
  const failure = State.describeCheckinFailure(info(409, 'checkin_overflow', { checkin: { would_gain: 120, would_lose: 80 } }));
  assert.equal(failure.kind, 'overflow');
  assert.deepEqual(failure.checkin, { wouldGain: 120, wouldLose: 80 });
  assert.ok(failure.message.includes('120') && failure.message.includes('80') && failure.message.includes('作廢'));
  const missing = State.describeCheckinFailure(info(409, 'checkin_overflow', { checkin: { would_gain: 'x' } }));
  assert.equal(missing.checkin, null);
  assert.ok(!/\d/.test(missing.message));
});

test('"already checked in" shows the next time in Taiwan time when the wallet gave it, else the fixed sentence', () => {
  const withTime = State.describeCheckinFailure(info(409, 'already_checked_in'), { nextResetAt: '2026-11-11T16:00:00Z' });
  assert.equal(withTime.kind, 'already');
  assert.ok(withTime.message.includes('2026-11-12 00:00') && withTime.message.includes('台灣時間'));
  const without = State.describeCheckinFailure(info(409, 'already_checked_in'));
  assert.equal(without.message, Labels.checkinText('already_default'));
});

test('every other check-in failure has a fixed message', () => {
  const cases = [
    [info(401, 'unauthorized'), 'login'], [info(403, 'account_locked'), 'gate'], [info(429, 'too_many_requests'), 'limit'], [info(503, 'wallet_busy'), 'unavailable'],
    [info(500, 'error'), 'unknown'], [info(409, 'weird'), 'unknown'], [{ kind: 'network', status: 0 }, 'network'], [{ kind: 'timeout', status: 0 }, 'timeout'],
  ];
  for (const [input, kind] of cases) {
    const failure = State.describeCheckinFailure(Object.assign(input, { message: MARKER }));
    assert.equal(failure.kind, kind);
    assert.ok(HAN.test(failure.message), kind);
    assertFixedMessage(failure, kind);
  }
  assert.equal(State.describeCheckinFailure({ kind: 'canceled', status: 0 }).silent, true);
});

test('no failure message of any kind ever contains the player text we pass in the places a server could echo it', () => {
  const places = { message: MARKER, code: MARKER, fields: { action: MARKER, text: MARKER }, categories: [MARKER], notebook: { text: MARKER, version: 1 }, checkin: { would_gain: MARKER } };
  for (const status of [0, 400, 401, 403, 404, 409, 422, 429, 500, 503]) {
    for (const fn of [State.describeRequestFailure, State.describeNotebookFailure, State.describeCheckinFailure]) {
      const failure = fn(Object.assign({ kind: status === 0 ? 'network' : 'server', status }, places));
      assert.ok(!failure.message.includes(MARKER), `${fn.name} ${status}`);
    }
    assert.ok(!State.describeHttpFailure(status, { error: { code: MARKER, message: MARKER, fields: { action: MARKER } }, categories: [MARKER] }).message.includes(MARKER), `http ${status}`);
  }
});

// ---- 世界狀態與通知 ----------------------------------------------------------------------------------

test('worldView keeps only the player-visible fields with the right types', () => {
  const view = State.worldView({
    elapsed_months: 14, location: '山門', summary: '剛入門', goals: ['拜師', 5, null, '練劍'], places: ['山門', {}, '後山'], protagonist_age: 17,
    npcs: { 師父: { trait: '隱藏的歸屬' } }, hidden_trait: '秘密', arc_stage: 'cracking',
  });
  assert.deepEqual(view, { elapsed_months: 14, location: '山門', summary: '剛入門', goals: ['拜師', '練劍'], places: ['山門', '後山'], protagonist_age: 17 });
  assert.ok(!JSON.stringify(view).includes('隱藏的歸屬') && !JSON.stringify(view).includes('cracking'));
});

test('worldView of anything unusable is a safe empty view', () => {
  for (const junk of [null, undefined, 'x', 5, [], {}, { elapsed_months: -1, location: 5, goals: 'x', protagonist_age: 'x' }]) {
    assert.deepEqual(State.worldView(junk), { elapsed_months: 0, location: '', summary: '', goals: [], places: [], protagonist_age: null }, JSON.stringify(junk));
  }
});

test('mergeWorld applies the state-event fields and keeps the places and the age until the snapshot is re-read', () => {
  const current = State.worldView({ elapsed_months: 3, location: '山門', summary: 'a', goals: ['x'], places: ['山門'], protagonist_age: 16 });
  const merged = State.mergeWorld(current, { elapsed_months: 4, location: '後山', goals: ['y'] });
  assert.deepEqual(merged, { elapsed_months: 4, location: '後山', summary: 'a', goals: ['y'], places: ['山門'], protagonist_age: 16 });
  assert.equal(current.location, '山門');                                           // 不修改傳入的物件
  assert.deepEqual(State.mergeWorld(current, null), current);
  assert.deepEqual(State.mergeWorld(current, { location: 5, elapsed_months: -2, goals: 'x' }), current);
});

test('an age correction notice says who, the age before, and the age after', () => {
  assert.equal(State.noticeText({ kind: 'age_corrected', name: '林婉', before: 19, after: 22 }), '林婉的年齡由 19 歲更正為 22 歲。');
  assert.equal(State.noticeText({ kind: 'age_corrected', name: '  ', before: 0, after: 17 }), '角色的年齡由 0 歲更正為 17 歲。');
});

test('anything that is not a complete age correction has no notice text', () => {
  for (const junk of [null, undefined, {}, 'x', { kind: 'other', before: 1, after: 2 }, { kind: 'age_corrected', before: 1 }, { kind: 'age_corrected', before: -1, after: 2 }, { kind: 'age_corrected', before: '1', after: 2 }]) {
    assert.equal(State.noticeText(junk), '', JSON.stringify(junk));
  }
});

// ---- 串流事件 -> 畫面指令 ----------------------------------------------------------------------------

const ev = (event, data) => ({ event, data });
function feed(turn, events) {
  const effects = [];
  let current = turn;
  for (const event of events) {
    const step = State.applyStreamEvent(current, event);
    current = step.turn;
    effects.push(...step.effects);
  }
  return { turn: current, effects };
}
const types = (effects) => effects.map((effect) => effect.type);

test('a normal turn: meta, tokens, state, done give push effects, a world update, the new balance, and finish', () => {
  const start = State.newStreamTurn('c1', '推開門');
  const { turn, effects } = feed(start, [
    ev('meta', { turn_id: 3, client_msg_id: 'c1', attempt: 0, replayed: false }),
    ev('token', { text: '門' }), ev('token', { text: '開了。' }), ev('ping', {}),
    ev('state', { updated: true, elapsed_months: 2, location: '山門', summary: '剛入門', goals: ['拜師'], notices: [] }),
    ev('done', { status: 'complete', turn_number: 3, coins: 170 }),
  ]);
  assert.deepEqual(types(effects), ['meta', 'push', 'push', 'world', 'finish', 'coins', 'done']);
  assert.deepEqual(effects.filter((e) => e.type === 'push').map((e) => e.text), ['門', '開了。']);
  assert.deepEqual(effects.find((e) => e.type === 'world').world, { elapsed_months: 2, location: '山門', summary: '剛入門', goals: ['拜師'] });
  assert.deepEqual(effects.find((e) => e.type === 'coins'), { type: 'coins', balance: 170 });
  assert.equal(turn.seq, 3);
  assert.equal(turn.finished, true);
  assert.equal(turn.status, 'complete');
  assert.equal(turn.coins, 170);
  assert.equal(turn.turnNumber, 3);
  assert.equal(turn.received, 4);
});

test('applyStreamEvent never modifies the turn it is given', () => {
  const start = State.newStreamTurn('c1', '行動');
  const frozen = JSON.stringify(start);
  State.applyStreamEvent(start, ev('token', { text: '字' }));
  State.applyStreamEvent(start, ev('done', { status: 'complete', coins: 1 }));
  assert.equal(JSON.stringify(start), frozen);
});

test('the first block discards the bubble and shows one transition line; the retry gets a fresh bubble', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [
    ev('meta', { turn_id: 4, replayed: false }), ev('token', { text: '被擋的字' }),
    ev('transition', { text: '話鋒一轉，場景悄悄換到了另一個角落。', will_retry: true }),
    ev('token', { text: '新的完整回應' }), ev('state', { updated: false, notices: [] }), ev('done', { status: 'complete', turn_number: 4, coins: 140 }),
  ]);
  assert.deepEqual(types(effects), ['meta', 'push', 'discard', 'transition', 'push', 'finish', 'coins', 'done']);
  assert.deepEqual(effects.find((e) => e.type === 'transition'), { type: 'transition', text: '話鋒一轉，場景悄悄換到了另一個角落。', willRetry: true });
  assert.equal(turn.bubble, 1);
  assert.equal(turn.received, 6);              // 重置後只算新氣泡
});

test('two blocks end as blocked: no bubble, the transition line, a hint to try another action, and the balance', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [
    ev('meta', { turn_id: 5, replayed: false }), ev('token', { text: '第一次' }),
    ev('transition', { text: '一陣薄霧掠過，方才的一切隨之模糊。', will_retry: true }), ev('token', { text: '第二次' }),
    ev('transition', { text: '命運的紙頁翻過一頁，故事換了一個方向。', will_retry: false }),
    ev('done', { status: 'blocked', turn_number: null, coins: 170 }),
  ]);
  assert.deepEqual(types(effects), ['meta', 'push', 'discard', 'transition', 'push', 'discard', 'transition', 'discard', 'hint', 'coins', 'done']);
  assert.equal(effects.filter((e) => e.type === 'transition').at(-1).willRetry, false);
  assert.equal(effects.find((e) => e.type === 'hint').text, Labels.blockedHint());
  assert.equal(turn.received, 0);
  assert.equal(turn.status, 'blocked');
  assert.equal(effects.find((e) => e.type === 'done').status, 'blocked');
  assert.ok(!types(effects).includes('finish'));
});

test('a blocked turn never ends with a "finish" effect (nothing the player saw is kept)', () => {
  const { effects } = feed(State.newStreamTurn('c1', 'a'), [ev('token', { text: 'x' }), ev('done', { status: 'blocked', coins: 1 })]);
  assert.ok(!types(effects).includes('finish'));
  assert.ok(types(effects).includes('discard'));
});

test('an ended turn finishes and marks the save as ended', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [ev('token', { text: '終章。' }), ev('done', { status: 'ended', turn_number: 99, coins: 10 })]);
  assert.ok(types(effects).includes('finish'));
  assert.equal(effects.find((e) => e.type === 'done').ended, true);
  assert.equal(turn.ended, true);
});

test('a replayed turn is one whole token; the balance is unchanged because the server says so', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [
    ev('meta', { turn_id: 2, client_msg_id: 'c1', attempt: 1, replayed: true }), ev('token', { text: '整段重播的劇情' }), ev('done', { status: 'complete', turn_number: 2, coins: 140 }),
  ]);
  assert.equal(turn.replayed, true);
  assert.deepEqual(effects.find((e) => e.type === 'meta'), { type: 'meta', seq: 2, replayed: true });
  assert.equal(effects.filter((e) => e.type === 'push').length, 1);
});

test('the opening: meta says opening, done carries the opening state and does not count as a turn', () => {
  const { turn, effects } = feed(State.newStreamTurn('', '', 'opening'), [
    ev('meta', { kind: 'opening', replayed: false }), ev('token', { text: '序幕。' }), ev('done', { status: 'complete', turn_number: null, coins: 200, opening_state: 'ready' }),
  ]);
  assert.equal(turn.kind, 'opening');
  assert.equal(turn.openingState, 'ready');
  assert.equal(effects.find((e) => e.type === 'done').openingState, 'ready');
  assert.equal(turn.turnNumber, null);
  const odd = feed(State.newStreamTurn('', '', 'opening'), [ev('done', { status: 'complete', coins: 1, opening_state: 'mystery' })]);
  assert.equal(odd.turn.openingState, null);
});

test('a state event with only known fields becomes a world update and notices; everything else is dropped', () => {
  const { effects } = feed(State.newStreamTurn('c1', 'a'), [ev('state', {
    updated: true, elapsed_months: 6, location: '後山', summary: '修行', goals: ['x', 5], npcs: { 師父: '隱藏' }, hidden_trait: '秘密', arc_stage: 'cracking',
    notices: [
      { kind: 'age_corrected', name: '林婉', before: 19, after: 22, text: '伺服器的文字不採用', secret: MARKER },
      { kind: 'something_else', name: 'x', before: 1, after: 2 }, { kind: 'age_corrected', name: 'y', before: 'x' }, 'junk',
    ],
  })]);
  assert.deepEqual(effects.find((e) => e.type === 'world').world, { elapsed_months: 6, location: '後山', summary: '修行', goals: ['x'] });
  assert.deepEqual(effects.find((e) => e.type === 'notices').notices, [{ kind: 'age_corrected', name: '林婉', before: 19, after: 22, text: '林婉的年齡由 19 歲更正為 22 歲。' }]);
  assert.ok(!JSON.stringify(effects).includes('隱藏') && !JSON.stringify(effects).includes('cracking') && !JSON.stringify(effects).includes(MARKER));
});

test('a state event that says nothing was updated has no world effect, but its notices still show', () => {
  const quiet = feed(State.newStreamTurn('c1', 'a'), [ev('state', { updated: false, notices: [] })]);
  assert.deepEqual(quiet.effects, []);
  const onlyNotice = feed(State.newStreamTurn('c1', 'a'), [ev('state', { updated: false, notices: [{ kind: 'age_corrected', name: 'z', before: 1, after: 2 }] })]);
  assert.deepEqual(types(onlyNotice.effects), ['notices']);
});

test('an error event discards the bubble and shows its fixed message; it does not finish the turn by itself', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [ev('token', { text: '一半' }), ev('error', { code: 'generation_failed', message: `${MARKER}` })]);
  assert.deepEqual(types(effects), ['push', 'discard', 'failure']);
  assert.equal(effects.find((e) => e.type === 'failure').failure.message, Labels.STREAM_ERROR_MESSAGES.generation_failed);
  assert.equal(turn.failed, true);
  assert.equal(turn.finished, false);
  assert.equal(turn.received, 0);
});

test('events after done are ignored', () => {
  const { effects } = feed(State.newStreamTurn('c1', 'a'), [ev('done', { status: 'complete', coins: 1 }), ev('token', { text: '多餘' }), ev('transition', { text: 'x', will_retry: true })]);
  assert.deepEqual(types(effects), ['finish', 'coins', 'done']);
});

test('a done with an unknown status keeps no text and asks nothing of the bubble except to drop it', () => {
  const { turn, effects } = feed(State.newStreamTurn('c1', 'a'), [ev('token', { text: '字' }), ev('done', { status: 'mystery', coins: 7 })]);
  assert.equal(turn.status, 'unknown');
  assert.ok(!types(effects).includes('finish'));
  assert.ok(types(effects).includes('discard'));
  assert.ok(!types(effects).includes('hint'));
});

test('unexpected input changes nothing and does not throw', () => {
  const start = State.newStreamTurn('c1', 'a');
  for (const junk of [null, undefined, 'x', 5, {}, { event: 5, data: {} }, { event: 'token' }, { event: 'token', data: 'x' }, { event: 'mystery', data: {} }, { event: 'ping', data: {} }]) {
    const step = State.applyStreamEvent(start, junk);
    assert.deepEqual(step.effects, [], JSON.stringify(junk));
    assert.deepEqual(step.turn, start);
  }
  assert.deepEqual(State.applyStreamEvent(start, ev('token', { text: 5 })).effects, []);
  assert.deepEqual(State.applyStreamEvent(start, ev('token', { text: '' })).effects, []);
});

test('a transition line that is empty or absurdly long falls back to the fixed blocked line', () => {
  for (const text of ['', '字'.repeat(500), 5, undefined]) {
    const { effects } = feed(State.newStreamTurn('c1', 'a'), [ev('transition', { text, will_retry: false })]);
    assert.equal(effects.find((e) => e.type === 'transition').text, Labels.historyLine('blocked'));
  }
});

test('a non-numeric balance is not shown: no coins effect', () => {
  const { effects } = feed(State.newStreamTurn('c1', 'a'), [ev('done', { status: 'complete', coins: '170' })]);
  assert.ok(!types(effects).includes('coins'));
  const negative = feed(State.newStreamTurn('c1', 'a'), [ev('done', { status: 'complete', coins: -5 })]);
  assert.ok(!types(negative.effects).includes('coins'));
});

// ---- 沒有 done 的結束 --------------------------------------------------------------------------------

test('an interruption discards the unfinished bubble and explains, asking for a catch-up', () => {
  const { turn } = feed(State.newStreamTurn('c1', 'a'), [ev('meta', { turn_id: 6 }), ev('token', { text: '一半的字' })]);
  const end = State.applyStreamEnd(turn, { outcome: 'interrupted', done: null, failure: null });
  assert.deepEqual(types(end.effects), ['discard', 'failure']);
  assert.equal(end.effects[1].failure.catchUp, true);
  assert.ok(end.effects[1].failure.message.includes('可能已在伺服器完成'));
  assert.equal(end.turn.finished, true);
  assert.equal(end.turn.status, 'interrupted');
  assert.equal(end.turn.received, 0);                                              // 沒有保留任何部分文字
});

test('a cancel ends the turn silently but still asks for a catch-up', () => {
  const end = State.applyStreamEnd(State.newStreamTurn('c1', 'a'), { outcome: 'aborted' });
  const failure = end.effects.find((e) => e.type === 'failure').failure;
  assert.equal(failure.silent, true);
  assert.equal(failure.catchUp, true);
});

test('a rejected start shows the describeHttpFailure result and no bubble was ever started', () => {
  const rejected = State.describeHttpFailure(409, body('insufficient_coins', '', { top: { balance: 5, needed: 30 } }));
  const end = State.applyStreamEnd(State.newStreamTurn('c1', 'a'), { outcome: 'rejected', failure: rejected });
  assert.equal(end.effects.find((e) => e.type === 'failure').failure.kind, 'coins');
  assert.equal(end.turn.status, 'rejected');
});

test('after an error event the end of the stream does not show a second failure', () => {
  const { turn } = feed(State.newStreamTurn('c1', 'a'), [ev('error', { code: 'generation_timeout', message: '' })]);
  const end = State.applyStreamEnd(turn, { outcome: 'error_event', failure: { code: 'generation_timeout' } });
  assert.deepEqual(types(end.effects), ['discard']);
  assert.equal(end.turn.finished, true);
});

test('after done the stream end changes nothing', () => {
  const { turn } = feed(State.newStreamTurn('c1', 'a'), [ev('done', { status: 'complete', coins: 1 })]);
  assert.deepEqual(State.applyStreamEnd(turn, { outcome: 'done' }).effects, []);
  assert.deepEqual(State.applyStreamEnd(turn, { outcome: 'interrupted' }).effects, []);
  assert.deepEqual(State.applyStreamEnd(State.newStreamTurn('c1', 'a'), { outcome: 'done' }).effects, []);
});

// ---- 回合、合併與時間軸 ------------------------------------------------------------------------------

const turnRecord = (seq, status, extra) => Object.assign({ seq, client_msg_id: `c${seq}`, turn_number: seq, status, action: `行動${seq}`, story: `劇情${seq}` }, extra);

test('normalizeTurn keeps text only for complete turns: blocked, interrupted and streaming turns have none', () => {
  for (const status of ['blocked', 'interrupted', 'streaming']) {
    const turn = State.normalizeTurn(turnRecord(5, status, { action: MARKER, story: MARKER }));
    assert.equal(turn.action, '', status);
    assert.equal(turn.story, '', status);
    assert.equal(turn.status, status);
  }
  const complete = State.normalizeTurn(turnRecord(5, 'complete'));
  assert.deepEqual(complete, { seq: 5, client_msg_id: 'c5', turn_number: 5, status: 'complete', action: '行動5', story: '劇情5' });
});

test('an unknown status is treated as interrupted (fail closed) and a record without an integer seq is dropped', () => {
  assert.equal(State.normalizeTurn(turnRecord(5, 'mystery')).status, 'interrupted');
  assert.equal(State.normalizeTurn(turnRecord(5, 'mystery', { story: MARKER })).story, '');
  for (const junk of [null, undefined, 5, 'x', [], {}, { seq: '5' }, { seq: -1 }, { seq: 1.5 }, { seq: NaN }]) { assert.equal(State.normalizeTurn(junk), null, JSON.stringify(junk)); }
});

test('mergeTurns orders by seq and the backend wins for the same seq', () => {
  const local = [turnRecord(2, 'complete', { story: '本機的舊內容' }), turnRecord(1, 'complete')];
  const remote = [turnRecord(2, 'complete', { story: '後端的內容' }), turnRecord(3, 'complete')];
  const merged = State.mergeTurns(local, remote);
  assert.deepEqual(merged.map((t) => t.seq), [1, 2, 3]);
  assert.equal(merged[1].story, '後端的內容');
});

test('mergeTurns: local 40 plus remote 41 and 42 gives 42 ordered turns', () => {
  const local = Array.from({ length: 40 }, (_, i) => turnRecord(i + 1, 'complete'));
  const merged = State.mergeTurns(local, [turnRecord(42, 'complete'), turnRecord(41, 'complete')]);
  assert.equal(merged.length, 42);
  assert.deepEqual(merged.map((t) => t.seq), Array.from({ length: 42 }, (_, i) => i + 1));
});

test('mergeTurns: a turn that was complete locally and is blocked on the backend loses its text', () => {
  const merged = State.mergeTurns([turnRecord(2, 'complete')], [turnRecord(2, 'blocked')]);
  assert.equal(merged[0].status, 'blocked');
  assert.equal(merged[0].story, '');
  assert.equal(merged[0].action, '');
});

test('mergeTurns does not modify its inputs, ignores junk, and accepts anything that is not an array', () => {
  const local = [turnRecord(1, 'complete'), null, 5];
  const frozen = JSON.stringify(local);
  const merged = State.mergeTurns(local, 'x');
  assert.equal(JSON.stringify(local), frozen);
  assert.equal(merged.length, 1);
  assert.deepEqual(State.mergeTurns(undefined, undefined), []);
  assert.deepEqual(State.mergeTurns('x', {}), []);
});

test('mergeTurns survives a hostile seq or field name', () => {
  const merged = State.mergeTurns([{ seq: 1, status: 'complete', client_msg_id: '__proto__', action: 'a', story: 's' }], [{ seq: 2, constructor: { prototype: 1 }, status: 'complete' }]);
  assert.deepEqual(merged.map((t) => t.seq), [1, 2]);
  assert.equal({}.polluted, undefined);
});

test('timelineFrom lays out the opening, then turns by seq, and a blocked turn is one line with no text', () => {
  const items = State.timelineFrom(
    { state: 'ready', text: '第 0 幕的文字' },
    [turnRecord(2, 'blocked', { action: MARKER, story: MARKER }), turnRecord(1, 'complete'), turnRecord(3, 'interrupted', { story: MARKER }), turnRecord(4, 'streaming')],
    null,
  );
  assert.deepEqual(items.map((i) => i.kind), ['opening', 'turn', 'note', 'note', 'note']);
  assert.equal(items[0].text, '第 0 幕的文字');
  assert.deepEqual([items[1].action, items[1].story], ['行動1', '劇情1']);
  assert.deepEqual(items.slice(2).map((i) => i.tone), ['transition', 'interrupted', 'busy']);
  assert.deepEqual(items.slice(2).map((i) => i.text), [Labels.historyLine('blocked'), Labels.historyLine('interrupted'), Labels.historyLine('streaming')]);
  assert.ok(!JSON.stringify(items).includes(MARKER));
});

test('timelineFrom: no opening text means no opening item, and keys are unique and stable', () => {
  assert.equal(State.timelineFrom({ state: 'fallback', text: '' }, [], null).length, 0);
  assert.equal(State.timelineFrom(null, [], null).length, 0);
  const items = State.timelineFrom({ state: 'ready', text: 'x' }, [turnRecord(1, 'complete'), turnRecord(2, 'blocked')], null);
  assert.deepEqual(items.map((i) => i.key), ['opening', 'turn:c1', 'note:c2']);
  assert.equal(new Set(items.map((i) => i.key)).size, items.length);
});

test('a complete turn\'s key is the same one a streaming bubble for that client_msg_id uses, so a catch-up replaces it in place', () => {
  const [item] = State.timelineFrom(null, [turnRecord(7, 'complete', { client_msg_id: 'abc-123' })], null);
  assert.equal(item.key, 'turn:abc-123');
});

test('an in-flight turn shows its action until the backend has a complete turn with that client_msg_id', () => {
  const inflight = { client_msg_id: 'pending-1', action: '我想要的行動', created_at: 'x' };
  const waiting = State.timelineFrom(null, [turnRecord(1, 'complete')], inflight);
  assert.deepEqual(waiting.map((i) => i.kind), ['turn', 'inflight']);
  assert.equal(waiting[1].action, '我想要的行動');
  assert.equal(waiting[1].clientMsgId, 'pending-1');
  const done = State.timelineFrom(null, [turnRecord(1, 'complete'), turnRecord(2, 'complete', { client_msg_id: 'pending-1' })], inflight);
  assert.deepEqual(done.map((i) => i.kind), ['turn', 'turn']);
  const blocked = State.timelineFrom(null, [turnRecord(2, 'blocked', { client_msg_id: 'pending-1' })], inflight);
  assert.deepEqual(blocked.map((i) => i.kind), ['note', 'inflight']);        // 被擋的不算完成：仍可重送或放棄
});

test('timelineFrom ignores a broken in-flight record', () => {
  for (const junk of [null, undefined, {}, { client_msg_id: '' }, { client_msg_id: 5 }, 'x']) {
    assert.equal(State.timelineFrom(null, [], junk).length, 0, JSON.stringify(junk));
  }
});

test('hasCompleteTurn only counts complete turns with the same client_msg_id', () => {
  const turns = [turnRecord(1, 'complete'), turnRecord(2, 'blocked')];
  assert.equal(State.hasCompleteTurn(turns, 'c1'), true);
  assert.equal(State.hasCompleteTurn(turns, 'c2'), false);
  assert.equal(State.hasCompleteTurn(turns, 'nope'), false);
  for (const junk of [undefined, null, '', 5]) { assert.equal(State.hasCompleteTurn(turns, junk), false); }
  assert.equal(State.hasCompleteTurn(null, 'c1'), false);
});

// ---- 記事簿檢視 --------------------------------------------------------------------------------------

test('notebookView keeps the known fields and drops the rest; a broken record is null', () => {
  assert.deepEqual(State.notebookView({ text: '內容', version: 3, length: 2, capacity: 2000, reserve: 500, full: false, other: MARKER }), { text: '內容', version: 3, length: 2, capacity: 2000, reserve: 500, full: false });
  assert.equal(State.notebookView({ text: '', version: 0 }).length, 0);
  assert.equal(State.notebookView({ text: 'ab', version: 1 }).length, 2);                    // 沒有 length 時自己算
  assert.equal(State.notebookView({ text: 'x', version: 1, full: 'yes' }).full, false);
  for (const junk of [null, undefined, 'x', 5, [], {}, { text: 5, version: 1 }, { text: 'x', version: -1 }, { text: 'x', version: '1' }]) { assert.equal(State.notebookView(junk), null, JSON.stringify(junk)); }
});

test('notebookSummary keeps the four fields of the snapshot summary; a missing or mistyped field makes it null (nothing is shown unless certain)', () => {
  assert.deepEqual(State.notebookSummary({ version: 3, length: 2240, capacity: 2000, full: true, text: MARKER, reserve: 500 }), { version: 3, length: 2240, capacity: 2000, full: true });
  assert.equal(State.notebookSummary({ version: 0, length: 0, capacity: 2000 }).full, false);
  assert.equal(State.notebookSummary({ version: 1, length: 1, capacity: 2000, full: 'yes' }).full, false);
  for (const junk of [null, undefined, 'x', 5, [], {}, { version: 1 }, { version: 1, length: 5 }, { version: '1', length: 5, capacity: 2000 },
    { version: 1, length: -1, capacity: 2000 }, { version: 1, length: 5, capacity: 1.5 }]) {
    assert.equal(State.notebookSummary(junk), null, JSON.stringify(junk));
  }
});

test('notebookIsFull means longer than the capacity: the boundary is exact, and an unknown capacity falls back to the full flag', () => {
  assert.equal(State.notebookIsFull({ length: 2000, capacity: 2000, full: false }), false);
  assert.equal(State.notebookIsFull({ length: 2001, capacity: 2000, full: false }), true);            // 以數字為準
  assert.equal(State.notebookIsFull({ length: 2240, capacity: 5000, full: true }), false);
  assert.equal(State.notebookIsFull({ length: 0, capacity: 2000, full: true }), false);
  assert.equal(State.notebookIsFull({ length: 10, capacity: 0, full: true }), true);                 // 容量不明：退回後端旗標
  assert.equal(State.notebookIsFull({ length: 10, capacity: 0, full: false }), false);
  for (const junk of [null, undefined, 'x', 5, [], {}]) { assert.equal(State.notebookIsFull(junk), false, JSON.stringify(junk)); }
});

test('notebookCurrent picks the newer version of the panel view and the snapshot summary; a tie goes to the view', () => {
  const view = { text: 'a', version: 4, length: 1, capacity: 2000, reserve: 500, full: false };
  const summary = { version: 5, length: 9, capacity: 2000, full: false };
  assert.equal(State.notebookCurrent(view, summary), summary);
  assert.equal(State.notebookCurrent(view, { version: 4, length: 9, capacity: 2000, full: false }), view);
  assert.equal(State.notebookCurrent(view, { version: 3, length: 9, capacity: 2000, full: false }), view);
  assert.equal(State.notebookCurrent(view, null), view);
  assert.equal(State.notebookCurrent(null, summary), summary);
  assert.equal(State.notebookCurrent(null, null), null);
  assert.equal(State.notebookCurrent('x', 5), null);
});

test('notebookEditState counts like the backend and flags only what is over the capacity (no reserve for the player)', () => {
  assert.deepEqual(State.notebookEditState('字'.repeat(2000), 2000), { length: 2000, over: false, overBy: 0 });
  assert.deepEqual(State.notebookEditState('字'.repeat(2001), 2000), { length: 2001, over: true, overBy: 1 });
  assert.deepEqual(State.notebookEditState('字'.repeat(2400), 2000), { length: 2400, over: true, overBy: 400 });
  assert.deepEqual(State.notebookEditState('é😀', 2000), { length: 2, over: false, overBy: 0 });      // NFC、代理對算一個
  assert.deepEqual(State.notebookEditState('', 2000), { length: 0, over: false, overBy: 0 });
  for (const capacity of [0, undefined, null, -1, 1.5, '2000', NaN]) {
    assert.deepEqual(State.notebookEditState('字'.repeat(50), capacity), { length: 50, over: false, overBy: 0 }, String(capacity));   // 容量不明：不在前端判斷，由伺服器為準
  }
  assert.equal(State.notebookEditState(undefined, 2000).length, 0);
});

// ---- 送出行動 ----------------------------------------------------------------------------------------

const OPEN = { gate: '', ended: false, offline: false, openingReady: true, inflight: null, busy: false };

test('a send is allowed only when nothing blocks it', () => {
  assert.equal(State.sendBlocker(OPEN), '');
  assert.equal(State.canSend(OPEN), true);
});

test('each blocker disables sending with its own reason, in a fixed priority', () => {
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { gate: 'age_confirmation_required' })), 'gate');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { ended: true })), 'ended');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { offline: true })), 'offline');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { openingReady: false })), 'opening');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { inflight: { client_msg_id: 'x' } })), 'inflight');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { busy: true })), 'busy');
  assert.equal(State.sendBlocker({ gate: 'x', ended: true, offline: true, openingReady: false, inflight: {}, busy: true }), 'gate');
  assert.equal(State.sendBlocker({ ended: true, offline: true, openingReady: false, busy: true }), 'ended');
  assert.equal(State.canSend(Object.assign({}, OPEN, { busy: true })), false);
});

test('missing or doubtful state disables sending (fail closed): the opening must be explicitly ready', () => {
  for (const junk of [null, undefined, 'x', 5, [], {}]) { assert.notEqual(State.sendBlocker(junk), '', JSON.stringify(junk)); }
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { openingReady: 'yes' })), 'opening');
  assert.equal(State.sendBlocker(Object.assign({}, OPEN, { openingReady: undefined })), 'opening');
});

test('an empty or whitespace-only action is a problem; a normal one is not', () => {
  for (const junk of ['', '   ', '\n\t ', undefined, null, 5, {}]) { assert.equal(State.actionProblem(junk, 1000), '請輸入你的行動。', JSON.stringify(junk)); }
  assert.equal(State.actionProblem('推開門', 1000), '');
  assert.equal(State.actionProblem('  推開門  ', 1000), '');
});

test('the action limit counts NFC Unicode characters after trimming, and exactly the limit is fine', () => {
  assert.equal(State.actionProblem('字'.repeat(1000), 1000), '');
  assert.equal(State.actionProblem('字'.repeat(1001), 1000), '行動最多 1000 字（目前 1001 字）。');
  assert.equal(State.actionProblem(`  ${'字'.repeat(1000)}  `, 1000), '');            // 頭尾空白不算
  assert.equal(State.actionProblem('😀'.repeat(1000), 1000), '');                      // 一個表情符號算一個字元
  assert.equal(State.actionProblem('é'.repeat(1000), 1000), '');                 // 組合字合成一個字元
});

test('without a usable limit the length is left to the server, but an empty action is still a problem', () => {
  for (const junk of [undefined, null, 0, -5, '1000', NaN, 1.5]) {
    assert.equal(State.actionProblem('字'.repeat(5000), junk), '', JSON.stringify(junk));
    assert.equal(State.actionProblem('', junk), '請輸入你的行動。', JSON.stringify(junk));
  }
});

// ---- 在瀏覽器載入 ------------------------------------------------------------------------------------

test('in the browser the helpers join the shared Game namespace after the labels, and keep what another script already put there', () => {
  const labelsSource = fs.readFileSync(path.join(JS_DIR, 'game-labels.js'), 'utf8');
  const source = fs.readFileSync(FILE, 'utf8');
  const sandbox = { console };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext('self.Game = { api: { marker: 1 } };', sandbox);
  vm.runInContext(labelsSource, sandbox, { filename: 'game-labels.js' });
  vm.runInContext(source, sandbox, { filename: 'game-state.js' });
  assert.equal(sandbox.Game.api.marker, 1);
  assert.equal(typeof sandbox.Game.applyStreamEvent, 'function');
  assert.equal(sandbox.Game.notebookLength('😀'), 1);
  assert.ok(sandbox.Game.describeHttpFailure(409, { error: { code: 'insufficient_coins' } }).message.includes('沒有扣除遊戲幣'));
});

test('the module touches no browser storage, no network, no timers and no console', () => {
  const source = fs.readFileSync(FILE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'XMLHttpRequest', 'axios', 'setTimeout', 'setInterval', 'console.', 'innerHTML', 'eval(']) {
    assert.ok(!source.includes(forbidden), forbidden);
  }
});
