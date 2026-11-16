'use strict';
/* rpg-entry-pages 規格的顯示對照（game-labels.js）：所有列舉值都有繁體中文對照、未知值退回固定的安全文字（不顯示原始值）、
 * 閘門與內容模式的提示、站內路徑與台灣時間的格式。純函式，不需要瀏覽器。
 * 後端的列舉值是否與這裡的對照表一致，由 tests/test_game_pages.py 以後端常數比對。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const FILE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'game-labels.js');
const Game = require(FILE);

const HAN = /[一-鿿]/;
const JUNK = [undefined, null, '', 0, 1, true, {}, [], ['safe'], 'SAFE', ' safe', 'unknown', '__proto__', 'constructor', 'toString', 'hasOwnProperty'];

// ---- 對照表 ---------------------------------------------------------------------------------------

const TABLES = [
  ['MEMBER_LABELS', ['free', 'plus', 'vip'], 'memberLabel'],
  ['TIER_LABELS', ['safe', 'standard', 'adult'], 'tierLabel'],
  ['AUDIENCE_LABELS', ['all', 'male', 'female'], 'audienceLabel'],
  ['ORIGIN_LABELS', ['native', 'transmigrator'], 'originLabel'],
  ['STATUS_LABELS', ['active', 'ended'], 'statusLabel'],
  ['CATEGORY_LABELS', [
    'minor_sexual', 'sexual_violence', 'non_consensual', 'real_person_sexual', 'minor_suspected', 'real_person_suspected', 'tier_mismatch', 'other_concern',
  ], 'categoryLabel'],
  ['REVISION_CATEGORY_LABELS', ['setting_mismatch', 'repetitive_or_dull', 'typo_or_quality', 'blocked_transition', 'other'], 'revisionCategoryLabel'],
];

for (const [table, values, fn] of TABLES) {
  test(`${table} has exactly the expected enum values, each with a Traditional Chinese label`, () => {
    assert.deepEqual(Object.keys(Game[table]).sort(), [...values].sort());
    for (const value of values) {
      const label = Game[fn](value);
      assert.equal(label, Game[table][value]);
      assert.ok(HAN.test(label), `${value} -> ${label}`);
      assert.ok(!/[A-Za-z_]/.test(label), `${value} 的對照文字不該含英文或底線：${label}`);
    }
  });

  test(`${fn} never shows an unknown value; it falls back to one fixed text`, () => {
    const fallback = Game[fn](undefined);
    assert.ok(HAN.test(fallback));
    for (const junk of JUNK) {
      if (typeof junk === 'string' && values.includes(junk)) { continue; }
      assert.equal(Game[fn](junk), fallback, JSON.stringify(junk));
    }
    assert.ok(!values.some((value) => Game[fn](value) === fallback && fallback === ''), '預設文字不可為空');
  });
}

test('the fallback texts say "not marked" or the safest reading, never a stronger rating than the data', () => {
  assert.equal(Game.tierLabel('mystery'), '未標示');            // 不把未知的分級當成安全或成人
  assert.equal(Game.audienceLabel('mystery'), '全讀者');         // 與後端一致：缺值與未知值視為全讀者
  assert.equal(Game.originLabel('mystery'), '本地人');           // 既有存檔缺值視為本地人
});

// ---- 帳號閘門 -------------------------------------------------------------------------------------

test('no gate means no notice', () => {
  for (const gate of [null, undefined, '', 0, false, {}, []]) { assert.equal(Game.gateNotice(gate), null, JSON.stringify(gate)); }
});

test('each gate code has its own Traditional Chinese notice; only the ones that can be fixed offer the account settings link', () => {
  const missing = Game.gateNotice('age_confirmation_required');
  assert.ok(HAN.test(missing.text) && missing.link === '/account' && missing.linkText === '前往帳號設定');
  const young = Game.gateNotice('age_restricted');
  assert.ok(HAN.test(young.text) && young.link === null);        // 年齡不符：補填也沒有用，所以不引導
  const forbidden = Game.gateNotice('forbidden');
  assert.ok(HAN.test(forbidden.text) && forbidden.link === '/account');
  assert.equal(new Set([missing.text, young.text, forbidden.text]).size, 3);
});

test('an unknown gate code still disables starting (fails closed) and never echoes the code', () => {
  for (const code of ['something_new', '__proto__', 'constructor', 'x'.repeat(50)]) {
    const notice = Game.gateNotice(code);
    assert.ok(notice && HAN.test(notice.text) && !notice.text.includes(code), code);
  }
});

test('a gate notice is a copy: changing it does not change the next one', () => {
  const first = Game.gateNotice('forbidden');
  first.text = '被改掉了';
  first.link = null;
  assert.notEqual(Game.gateNotice('forbidden').text, '被改掉了');
  assert.equal(Game.gateNotice('forbidden').link, '/account');
});

// ---- 內容模式的說明 -------------------------------------------------------------------------------

test('a player below the adult threshold is told the mode is fixed and why, with no way to change it', () => {
  const notice = Game.contentModeNotice({ effective: 'safe', is_adult: false });
  assert.ok(notice.text.includes('安全模式') && notice.text.includes('18'));
  assert.equal(notice.link, null);                                // 固定的限制：沒有任何調整的連結
});

test('an adult in safe mode is only reminded that the account settings can change it', () => {
  const notice = Game.contentModeNotice({ effective: 'safe', is_adult: true });
  assert.ok(notice.text.includes('帳號設定') && notice.link === '/account' && notice.linkText === '前往帳號設定');
});

test('an adult in standard or adult mode needs no explanation', () => {
  assert.equal(Game.contentModeNotice({ effective: 'standard', is_adult: true }), null);
  assert.equal(Game.contentModeNotice({ effective: 'adult', is_adult: true }), null);
});

test('a missing or malformed mode summary gives no notice and never throws', () => {
  for (const junk of [null, undefined, 0, 'safe', [], {}, { effective: 7 }, { is_adult: 'false' }]) {
    assert.equal(Game.contentModeNotice(junk), null, JSON.stringify(junk));
  }
});

// ---- 路徑 -----------------------------------------------------------------------------------------

test('start and save paths are site-internal and encode the identifier', () => {
  const id = '3f2b8a40-1c2d-4e5f-8a9b-0c1d2e3f4a5b';
  assert.equal(Game.startPath(id), `/game/start/${id}`);
  assert.equal(Game.sessionPath(id), `/game/${id}`);
  assert.equal(Game.startPath('a/b?c#d'), '/game/start/a%2Fb%3Fc%23d');
  assert.equal(Game.sessionPath('../x'), '/game/..%2Fx');
  assert.ok(!Game.startPath('//evil.example').startsWith('//'));
});

// ---- 台灣時間 -------------------------------------------------------------------------------------

test('UTC times are shown in Taiwan time (UTC+8, no daylight saving)', () => {
  assert.equal(Game.formatTaipeiTime('2026-10-27T12:00:00Z'), '2026-10-27 20:00');
  assert.equal(Game.formatTaipeiTime('2026-10-27T17:30:00Z'), '2026-10-28 01:30');       // 跨日
  assert.equal(Game.formatTaipeiTime('2026-12-31T16:00:00Z'), '2027-01-01 00:00');       // 跨年
  assert.equal(Game.formatTaipeiTime('2026-07-01T00:05:09Z'), '2026-07-01 08:05');       // 夏季也一樣
});

test('times that already carry an offset are converted correctly', () => {
  assert.equal(Game.formatTaipeiTime('2026-10-27T20:00:00+08:00'), '2026-10-27 20:00');
  assert.equal(Game.formatTaipeiTime('2026-10-27T12:00:00.123Z'), '2026-10-27 20:00');
});

test('a missing or unreadable time shows nothing', () => {
  for (const junk of [undefined, null, '', 'not a date', 0, 1790000000000, {}, [], '2026-13-45T99:99:99Z']) {
    assert.equal(Game.formatTaipeiTime(junk), '', JSON.stringify(junk));
  }
});

// ---- 遊戲主畫面：串流結束、轉場與中斷、記事簿與簽到（rpg-game-ui 1.4）---------------------------------

const GAME_SCREEN_TABLES = [
  ['TURN_STATUS_LABELS', ['streaming', 'complete', 'blocked', 'interrupted'], 'turnStatusLabel'],
  ['DONE_STATUS_LABELS', ['complete', 'blocked', 'ended'], 'doneStatusLabel'],
  ['STREAM_OUTCOME_LABELS', ['done', 'error_event', 'interrupted', 'timeout_idle', 'timeout_total', 'aborted', 'rejected'], 'streamOutcomeLabel'],
];

for (const [table, values, fn] of GAME_SCREEN_TABLES) {
  test(`${table} labels every value in Traditional Chinese and unknown values fall back to one fixed text`, () => {
    assert.deepEqual(Object.keys(Game[table]).sort(), [...values].sort());
    for (const value of values) {
      const label = Game[fn](value);
      assert.equal(label, Game[table][value]);
      assert.ok(HAN.test(label) && !/[A-Za-z_]/.test(label), `${value} -> ${label}`);
    }
    const fallback = Game[fn](undefined);
    assert.ok(HAN.test(fallback) && !/[A-Za-z_]/.test(fallback));
    for (const junk of JUNK) {
      if (typeof junk === 'string' && values.includes(junk)) { continue; }
      assert.equal(Game[fn](junk), fallback, JSON.stringify(junk));
    }
  });
}

test('an unknown turn status is treated as interrupted: never as a turn that has content', () => {
  assert.equal(Game.turnStatusLabel('mystery'), Game.TURN_STATUS_LABELS.interrupted);
  assert.equal(Game.historyLine('mystery'), Game.HISTORY_LINES.interrupted);
  assert.equal(Game.historyLine('complete'), Game.HISTORY_LINES.interrupted);   // complete 的回合有內容，不走這一行；萬一誤用也不顯示內容
});

test('the history lines for blocked, interrupted and streaming turns are fixed Chinese sentences without any placeholder', () => {
  assert.deepEqual(Object.keys(Game.HISTORY_LINES).sort(), ['blocked', 'interrupted', 'streaming']);
  for (const key of Object.keys(Game.HISTORY_LINES)) {
    const line = Game.historyLine(key);
    assert.ok(HAN.test(line) && !/[A-Za-z_{}$]/.test(line), line);
  }
  assert.equal(new Set(Object.values(Game.HISTORY_LINES)).size, 3);
});

test('every connection-ended notice says the turn may already be finished on the server (a frontend abort is not a backend abort)', () => {
  assert.deepEqual(Object.keys(Game.STREAM_OUTCOME_NOTICES).sort(), ['aborted', 'interrupted', 'timeout_idle', 'timeout_total']);
  for (const outcome of Object.keys(Game.STREAM_OUTCOME_NOTICES)) {
    const notice = Game.streamOutcomeNotice(outcome);
    assert.ok(notice.includes('可能已在伺服器完成') && notice.includes('以伺服器為準'), `${outcome}: ${notice}`);
    assert.ok(!/[A-Za-z_]/.test(notice), notice);
  }
  assert.equal(new Set(['interrupted', 'timeout_idle', 'timeout_total', 'aborted'].map(Game.streamOutcomeNotice)).size, 4);
});

test('a finished turn has no notice; any other unknown outcome gets the same general notice with the same reminder', () => {
  assert.equal(Game.streamOutcomeNotice('done'), '');
  const general = Game.streamOutcomeNotice('something_new');
  assert.ok(HAN.test(general) && general.includes('可能已在伺服器完成'));
  for (const junk of JUNK) {
    if (junk === 'done' || (typeof junk === 'string' && Object.keys(Game.STREAM_OUTCOME_NOTICES).includes(junk))) { continue; }
    assert.equal(Game.streamOutcomeNotice(junk), general, JSON.stringify(junk));
  }
});

test('stream error codes map to the fixed message; unknown codes never echo the server text or the code', () => {
  assert.deepEqual(Object.keys(Game.STREAM_ERROR_MESSAGES).sort(), ['generation_failed', 'generation_timeout', 'internal_error', 'opening_superseded', 'turn_superseded']);
  for (const code of Object.keys(Game.STREAM_ERROR_MESSAGES)) { assert.ok(HAN.test(Game.streamErrorMessage(code)) && !/[A-Za-z_]/.test(Game.streamErrorMessage(code)), code); }
  const fallback = Game.streamErrorMessage('brand_new_code');
  assert.ok(HAN.test(fallback) && !fallback.includes('brand_new_code'));
  for (const junk of JUNK) {
    if (typeof junk === 'string' && junk in Game.STREAM_ERROR_MESSAGES && Object.prototype.hasOwnProperty.call(Game.STREAM_ERROR_MESSAGES, junk)) { continue; }
    assert.equal(Game.streamErrorMessage(junk), fallback, JSON.stringify(junk));
  }
});

test('the failures that happened before the charge say no coins were taken', () => {
  for (const code of ['generation_timeout', 'generation_failed', 'internal_error']) { assert.ok(Game.streamErrorMessage(code).includes('沒有扣除遊戲幣'), code); }
  assert.ok(Game.blockedHint().includes('沒有扣除遊戲幣') && Game.blockedHint().includes('換個行動'));
});

test('notebook texts are fixed Chinese sentences; an unknown key falls back to one general sentence', () => {
  for (const key of Object.keys(Game.NOTEBOOK_TEXTS)) {
    const text = Game.notebookText(key);
    assert.ok(HAN.test(text) && !/[A-Za-z_{}$<>]/.test(text.replace(/AI/g, '')), `${key}: ${text}`);       // 「AI」是專案慣用字，其餘不含英文
  }
  assert.ok(Game.notebookText('empty').includes('10 輪'));
  assert.ok(Game.notebookText('full').includes('AI 可能會遺忘') && Game.notebookText('full').includes('之後的設定不再被記住'));
  assert.ok(Game.notebookText('full_edit').includes('一次刪到容量以內'));
  assert.ok(Game.notebookText('conflict').includes('保留'));
  const fallback = Game.notebookText('nope');
  assert.ok(HAN.test(fallback));
  for (const junk of JUNK) {
    if (typeof junk === 'string' && Object.prototype.hasOwnProperty.call(Game.NOTEBOOK_TEXTS, junk)) { continue; }
    assert.equal(Game.notebookText(junk), fallback, JSON.stringify(junk));
  }
});

test('notebook usage shows "used / capacity" and a dash when a number is not a count', () => {
  assert.equal(Game.notebookUsage(1800, 2000), '1800 / 2000');
  assert.equal(Game.notebookUsage(0, 2000), '0 / 2000');
  assert.equal(Game.notebookUsage(2240, 2000), '2240 / 2000');
  for (const junk of [undefined, null, '5', -1, 1.5, NaN, Infinity, {}, []]) {
    assert.equal(Game.notebookUsage(junk, 2000), '—', JSON.stringify(junk));
    assert.equal(Game.notebookUsage(5, junk), '—', JSON.stringify(junk));
  }
});

test('the too-long notice has only integers in it, and no number means no numbers shown', () => {
  assert.equal(Game.notebookTooLongNotice(2010, 2000), '記事簿超過容量（2010 / 2000 字），請刪減到容量以內再儲存。');
  for (const junk of [undefined, '2010', -1, 2.5, NaN]) {
    const text = Game.notebookTooLongNotice(junk, 2000);
    assert.ok(HAN.test(text) && !/\d/.test(text), `${JSON.stringify(junk)}: ${text}`);
  }
});

test('the reserve note says the reserve is only for the AI, and only exists when there is a reserve', () => {
  const note = Game.notebookReserveNote(500);
  assert.ok(note.includes('預備空間 500 字') && note.includes('只供 AI 寫入') && note.includes('你自己編輯時不能使用'));
  for (const junk of [0, -1, 2.5, '500', undefined, null, NaN]) { assert.equal(Game.notebookReserveNote(junk), '', JSON.stringify(junk)); }
});

test('the over-capacity note says how many characters are over and that the text must be cut to the capacity; no count means no note', () => {
  assert.equal(Game.notebookOverNote(240), '超出容量 240 字，須刪到容量以內才能儲存。');
  for (const junk of [0, -1, 2.5, '240', undefined, null, NaN]) { assert.equal(Game.notebookOverNote(junk), '', JSON.stringify(junk)); }
});

test('the check-in overflow notice shows what would be credited and what would be lost, as integers only', () => {
  const notice = Game.checkinOverflowNotice(120, 80);
  assert.ok(notice.includes('120') && notice.includes('80') && notice.includes('作廢') && notice.includes('要繼續領取嗎'));
  for (const junk of [undefined, null, '80', -3, 1.2, NaN]) {
    assert.ok(!/\d/.test(Game.checkinOverflowNotice(junk, 80)), JSON.stringify(junk));
    assert.ok(!/\d/.test(Game.checkinOverflowNotice(120, junk)), JSON.stringify(junk));
  }
});

test('"already checked in" shows the next time in Taiwan time, or the fixed sentence when the time is missing or unreadable', () => {
  const withTime = Game.checkinAlreadyNotice('2026-11-11T16:00:00Z');
  assert.ok(withTime.includes('2026-11-12 00:00') && withTime.includes('台灣時間'));
  for (const junk of [undefined, null, '', 'not a date', 5, {}]) {
    assert.equal(Game.checkinAlreadyNotice(junk), Game.CHECKIN_TEXTS.already_default, JSON.stringify(junk));
  }
});

test('check-in texts fall back to one general sentence for an unknown key', () => {
  assert.ok(Game.checkinText('done').includes('簽到成功'));
  const fallback = Game.checkinText('nope');
  assert.ok(HAN.test(fallback));
  assert.equal(Game.checkinText(undefined), fallback);
  assert.equal(Game.checkinText('__proto__'), fallback);
});

test('none of the new fixed texts contain a placeholder, markup or a template leftover', () => {
  const pools = [Game.TURN_STATUS_LABELS, Game.DONE_STATUS_LABELS, Game.STREAM_OUTCOME_LABELS, Game.STREAM_OUTCOME_NOTICES, Game.HISTORY_LINES, Game.STREAM_ERROR_MESSAGES, Game.NOTEBOOK_TEXTS, Game.CHECKIN_TEXTS];
  for (const pool of pools) { for (const text of Object.values(pool)) { assert.ok(!/[<>{}$]|undefined|null|NaN/.test(text), text); } }
});

// ---- 重新生成與編輯最後一輪（rpg-turn-revision 4.1）-----------------------------------------------

test('the regenerate note shows the free count while there is one, the price once it is used up, and nothing when neither is certain', () => {
  assert.equal(Game.revisionRegenerateNote(2, 30), '免費還剩 2 次');
  assert.equal(Game.revisionRegenerateNote(1, 30), '免費還剩 1 次');
  assert.equal(Game.revisionRegenerateNote(0, 30), '30 幣');
  assert.equal(Game.revisionRegenerateNote(0, 45), '45 幣');                                  // 幣價來自伺服器，不寫死
  assert.equal(Game.revisionRegenerateNote(0, null), '');                                    // 缺值時不顯示數字
  assert.equal(Game.revisionRegenerateNote(null, null), '');
  assert.equal(Game.revisionRegenerateNote(null, 30), '30 幣');
  for (const junk of [-1, 1.5, '2', NaN, undefined]) {
    assert.equal(Game.revisionRegenerateNote(junk, junk), '', String(junk));
  }
  assert.equal(Game.revisionRegenerateNote(0, 0), '');                                       // 幣價 0 不是有意義的價格
});

// 2026-11-16 規則修訂：編輯與重新生成共用同一組每回合免費次數，所以「編輯」旁的說明與「重新生成」旁完全相同。
test('the edit note shares the free count with the regenerate note: the remaining free times first, the price once they are used up, and nothing when neither is certain', () => {
  assert.equal(Game.revisionEditNote(2, 30), '免費還剩 2 次');
  assert.equal(Game.revisionEditNote(1, 30), '免費還剩 1 次');
  assert.equal(Game.revisionEditNote(1, 0), '免費還剩 1 次');                                 // 免費次數內伺服器給的編輯幣價是 0，不影響顯示
  assert.equal(Game.revisionEditNote(0, 30), '30 幣');
  assert.equal(Game.revisionEditNote(0, 45), '45 幣');
  assert.equal(Game.revisionEditNote(0, null), '');
  assert.equal(Game.revisionEditNote(null, 30), '30 幣');
  assert.equal(Game.revisionEditNote(null, null), '');
  for (const junk of [-1, 1.5, '2', NaN, undefined]) { assert.equal(Game.revisionEditNote(junk, junk), '', String(junk)); }
  for (const [freeLeft, cost] of [[2, 30], [1, 30], [0, 30], [0, null], [null, 30], [null, null], [0, 0], [3, 0]]) {
    assert.equal(Game.revisionEditNote(freeLeft, cost), Game.revisionRegenerateNote(freeLeft, cost), `${freeLeft}/${cost}`);   // 兩個按鈕旁永遠一致
  }
});

test('the regenerate modal says whether it is free or what it costs, and leaves out a number it does not know', () => {
  assert.equal(Game.revisionRegenerateCostLine(2, 30, 230), '這次重新生成不用遊戲幣（免費還剩 2 次）。');
  assert.equal(Game.revisionRegenerateCostLine(0, 30, 230), '這次重新生成會消耗 30 遊戲幣（目前餘額 230 幣）。');
  assert.equal(Game.revisionRegenerateCostLine(0, 30, null), '這次重新生成會消耗 30 遊戲幣。');
  assert.equal(Game.revisionRegenerateCostLine(0, null, 230), '這次重新生成會消耗遊戲幣（目前餘額 230 幣）。');
  assert.equal(Game.revisionRegenerateCostLine(null, null, null), '這次重新生成會消耗遊戲幣。');
  assert.ok(!/undefined|null|NaN/.test(Game.revisionRegenerateCostLine(undefined, undefined, undefined)));
});

test('the edit modal says it is free while there are free times left, without asking about the balance', () => {
  assert.equal(Game.revisionEditCostLine(2, 0, 230, false), '這次編輯免費（免費還剩 2 次）。');
  assert.equal(Game.revisionEditCostLine(1, 30, 230, false), '這次編輯免費（免費還剩 1 次）。');
  for (const balance of [0, 10, 230, null, undefined]) {                                       // 餘額多少都不影響：免費次數內不要求餘額，也不提餘額
    const line = Game.revisionEditCostLine(1, 30, balance, false);
    assert.equal(line, '這次編輯免費（免費還剩 1 次）。', String(balance));
    assert.ok(!line.includes('餘額') && !line.includes('消耗') && !line.includes('不退'), String(balance));
  }
  assert.ok(!Game.revisionEditCostLine(2, 30, 0, true).includes('不足'), '免費次數內即使被誤傳「不足」也不顯示不足');   // 免費優先：防呆
});

test('once the free times are used up the edit modal reminds that it consumes game coins and that the coins already spent on the turn are not refunded, with the price and the balance', () => {
  const line = Game.revisionEditCostLine(0, 30, 230, false);
  assert.equal(line, '這次編輯會消耗 30 幣，原本那輪已扣的幣不退（目前餘額 230 幣）。');
  assert.equal(Game.revisionEditCostLine(0, 30, null, false), '這次編輯會消耗 30 幣，原本那輪已扣的幣不退。');
  assert.equal(Game.revisionEditCostLine(0, null, 230, false), '這次編輯會消耗遊戲幣，原本那輪已扣的幣不退（目前餘額 230 幣）。');
  assert.equal(Game.revisionEditCostLine(null, 30, 230, false), line);                         // 免費次數不確定：照需付費說明（真正是否收費由伺服器決定）
  assert.ok(!line.includes('歸零'), '編輯不再使免費次數歸零');
});

test('when the free times are used up and the balance is short the edit modal says so and points to the daily check-in', () => {
  const short = Game.revisionEditCostLine(0, 30, 10, true);
  assert.ok(short.includes('遊戲幣不足') && short.includes('目前餘額 10 幣') && short.includes('需要 30 幣') && short.includes('簽到'));
  assert.ok(Game.revisionEditCostLine(0, null, null, true).includes('簽到'));
  assert.ok(!/undefined|null|NaN/.test(Game.revisionEditCostLine(undefined, undefined, undefined, true)));
  assert.ok(!/undefined|null|NaN/.test(Game.revisionEditCostLine(undefined, undefined, undefined, false)));
});

test('every not-revisable reason has its own explanation; an unknown reason has the general one and never shows the raw value', () => {
  const reasons = ['ended', 'not_last', 'in_progress', 'no_snapshot'];
  assert.deepEqual(Object.keys(Game.REVISION_NOT_REVISABLE_MESSAGES).sort(), [...reasons].sort());
  const seen = new Set(reasons.map((reason) => Game.revisionNotRevisableMessage(reason)));
  assert.equal(seen.size, 4);
  for (const junk of JUNK) {
    if (typeof junk === 'string' && reasons.includes(junk)) { continue; }
    assert.equal(Game.revisionNotRevisableMessage(junk), '這一輪目前不能重新生成或編輯。', JSON.stringify(junk));
  }
});

test('every revision stream error code says the original is kept; an unknown code falls back to the general message', () => {
  assert.deepEqual(Object.keys(Game.REVISION_ERROR_MESSAGES).sort(), ['generation_failed', 'generation_timeout', 'internal_error', 'turn_superseded']);
  for (const code of ['generation_failed', 'generation_timeout', 'internal_error']) {
    const message = Game.revisionErrorMessage(code);
    assert.ok(message.includes('已保留原本的回應') && message.includes('沒有扣遊戲幣') && message.includes('沒有用掉免費次數'), code);
  }
  for (const junk of JUNK) {
    if (typeof junk === 'string' && Object.keys(Game.REVISION_ERROR_MESSAGES).includes(junk)) { continue; }
    assert.equal(Game.revisionErrorMessage(junk), Game.revisionErrorMessage('nope'), JSON.stringify(junk));
  }
});

test('the reason counter shows "count / max" and a dash when the count is not usable', () => {
  assert.equal(Game.revisionReasonCount(12, 50), '12 / 50');
  assert.equal(Game.revisionReasonCount(0, 50), '0 / 50');
  assert.equal(Game.revisionReasonCount(12, null), '12');
  assert.equal(Game.revisionReasonCount(null, 50), '—');
  assert.equal(Game.revisionReasonCount('12', 50), '—');
});

test('the result texts have a fixed wording for the three promises: kept, synced and still processing; an unknown key falls back to the general line', () => {
  assert.equal(Game.revisionText('kept'), '已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。');
  assert.ok(Game.revisionText('kept_blocked').includes('已保留原本的回應'));
  assert.ok(Game.revisionText('kept_failed').includes('已保留原本的回應'));
  assert.ok(Game.revisionText('synced').includes('伺服器上最新的內容'));
  assert.ok(Game.revisionText('processing').includes('伺服器還在處理'));
  assert.ok(Game.revisionText('incomplete').includes('原本的回應已保留'));
  for (const junk of JUNK) {
    if (typeof junk === 'string' && Object.keys(Game.REVISION_TEXTS).includes(junk)) { continue; }
    assert.equal(Game.revisionText(junk), Game.REVISION_TEXTS.unavailable, JSON.stringify(junk));
  }
  assert.equal(Game.revisionText('__proto__'), Game.REVISION_TEXTS.unavailable);
});

test('the revision texts are Traditional Chinese and carry no placeholder, markup or template leftover', () => {
  for (const pool of [Game.REVISION_TEXTS, Game.REVISION_NOT_REVISABLE_MESSAGES, Game.REVISION_ERROR_MESSAGES, Game.REVISION_CATEGORY_LABELS]) {
    for (const value of Object.values(pool)) {
      assert.ok(HAN.test(value), value);
      assert.ok(!/[<>{}$]|undefined|null|NaN/.test(value), value);
    }
  }
  assert.ok(HAN.test(Game.REVISION_CONFLICT_MESSAGE) && !/[<>{}$]/.test(Game.REVISION_CONFLICT_MESSAGE));
});

// ---- 在瀏覽器載入 ---------------------------------------------------------------------------------

test('in the browser the helpers join the shared Game namespace and keep what another script already put there', () => {
  const source = fs.readFileSync(FILE, 'utf8');
  for (const order of ['labels-first', 'api-first']) {
    const sandbox = { console };
    sandbox.self = sandbox;
    vm.createContext(sandbox);
    if (order === 'api-first') { vm.runInContext('self.Game = { api: { marker: 1 } };', sandbox); }
    vm.runInContext(source, sandbox, { filename: 'game-labels.js' });
    assert.equal(typeof sandbox.Game.tierLabel, 'function', order);
    assert.equal(sandbox.Game.tierLabel('safe'), '安全模式', order);
    if (order === 'api-first') { assert.equal(sandbox.Game.api.marker, 1); }
  }
});
