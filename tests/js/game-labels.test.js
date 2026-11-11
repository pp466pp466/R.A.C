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
