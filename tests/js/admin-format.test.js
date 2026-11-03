// 管理員後台顯示邏輯的單元測試（純函式）。執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Format = require('../../app/static/js/admin/admin-format.js');

const STATUSES = ['pending_verification', 'active', 'suspended', 'deletion_pending'];
const TIERS = ['free', 'plus', 'vip'];
const ACTIONS = ['user.lookup', 'user.suspend', 'user.unsuspend', 'user.change_tier', 'conversation.read', 'conversation.delete', 'failures.read', 'reauth.failed',
  'account.lock', 'appeal.submit', 'appeal.read', 'account.unlock'];

test('every status, tier, role, action, kind and message value has a Chinese label', () => {
  for (const value of STATUSES) { assert.notEqual(Format.statusLabel(value), value, value); }
  for (const value of TIERS) { assert.notEqual(Format.tierLabel(value), value, value); }
  for (const value of ['user', 'admin']) { assert.notEqual(Format.roleLabel(value), value, value); }
  for (const value of ACTIONS) { assert.notEqual(Format.actionLabel(value), value, value); }
  for (const value of ['chat', 'summary']) { assert.notEqual(Format.kindLabel(value), value, value); }
  for (const value of ['user', 'assistant']) { assert.notEqual(Format.messageRoleLabel(value), value, value); }
  for (const value of ['ok', 'fallback']) { assert.notEqual(Format.messageStatusLabel(value), value, value); }
});

test('the labels cover exactly the known values and no others', () => {
  assert.deepEqual(Object.keys(Format.LABELS.status).sort(), [...STATUSES].sort());
  assert.deepEqual(Object.keys(Format.LABELS.tier).sort(), [...TIERS].sort());
  assert.deepEqual(Object.keys(Format.LABELS.action).sort(), [...ACTIONS].sort());
});

test('the tier labels match the ones the customer-service page shows', () => {
  assert.deepEqual([Format.tierLabel('free'), Format.tierLabel('plus'), Format.tierLabel('vip')], ['一般', '進階', '尊榮']);
});

test('an unknown value falls back to its raw code and an empty value to a dash, never throwing', () => {
  assert.equal(Format.statusLabel('brand_new_status'), 'brand_new_status');
  assert.equal(Format.actionLabel('user.teleport'), 'user.teleport');
  assert.equal(Format.tierLabel(42), '42');
  for (const empty of [null, undefined, '']) {
    assert.equal(Format.statusLabel(empty), '—');
    assert.equal(Format.actionLabel(empty), '—');
  }
  assert.equal(Format.statusLabel('toString'), 'toString');         // 不會取到物件原型上的屬性
  assert.equal(Format.actionLabel('constructor'), 'constructor');
});

test('dates are shown in Taipei time (UTC+8) with zero padding', () => {
  assert.equal(Format.taipeiDate('2026-10-29T04:50:00+00:00'), '2026-10-29');
  assert.equal(Format.taipeiDateTime('2026-10-29T04:50:00+00:00'), '2026-10-29 12:50');
  assert.equal(Format.taipeiDateTime('2026-10-29T00:05:00Z'), '2026-10-29 08:05');
  assert.equal(Format.taipeiClock('2026-10-29T00:05:00Z'), '08:05');
});

test('the day boundary is Taipei midnight, 16:00 UTC', () => {
  assert.equal(Format.taipeiDate('2026-10-29T15:59:59+00:00'), '2026-10-29');
  assert.equal(Format.taipeiDate('2026-10-29T16:00:00+00:00'), '2026-10-30');
  assert.equal(Format.taipeiDateTime('2026-10-29T15:59:00Z'), '2026-10-29 23:59');
  assert.equal(Format.taipeiDateTime('2026-10-29T16:00:00Z'), '2026-10-30 00:00');
  assert.equal(Format.taipeiDate('2026-12-31T16:00:00Z'), '2027-01-01');       // 跨年
});

test('a time with an explicit offset is converted, and one without any is read as UTC rather than the browser zone', () => {
  assert.equal(Format.taipeiDateTime('2026-10-29T12:50:00+08:00'), '2026-10-29 12:50');
  assert.equal(Format.taipeiDateTime('2026-10-29T04:50:00'), '2026-10-29 12:50');
});

test('an unparsable or missing time shows a dash', () => {
  for (const value of [null, undefined, '', 'not a date', 12345, {}]) {
    assert.equal(Format.taipeiDate(value), '—', String(value));
    assert.equal(Format.taipeiDateTime(value), '—', String(value));
    assert.equal(Format.taipeiClock(value), '—', String(value));
  }
});

test('the overview day label drops the year and shows a dash for anything else', () => {
  assert.equal(Format.shortDate('2026-10-29'), '10/29');
  assert.equal(Format.shortDate('2027-01-01'), '01/01');
  for (const value of [null, undefined, '', '2026/10/29', 'x', 20261029]) { assert.equal(Format.shortDate(value), '—', String(value)); }
});

test('the usage percentage rounds down so 100% only appears when the budget is really used up', () => {
  assert.equal(Format.ratioPercent(0), '0%');
  assert.equal(Format.ratioPercent(0.005), '0%');
  assert.equal(Format.ratioPercent(0.5), '50%');
  assert.equal(Format.ratioPercent(0.8), '80%');
  assert.equal(Format.ratioPercent(0.7999), '79%');
  assert.equal(Format.ratioPercent(0.998), '99%');
  assert.equal(Format.ratioPercent(1), '100%');
  assert.equal(Format.ratioPercent(1.0426), '104%');
});

test('the usage level changes at exactly 80% and 100%', () => {
  assert.equal(Format.ratioLevel(0), 'ok');
  assert.equal(Format.ratioLevel(0.7999), 'ok');
  assert.equal(Format.ratioLevel(0.8), 'warn');
  assert.equal(Format.ratioLevel(0.9999), 'warn');
  assert.equal(Format.ratioLevel(1), 'full');
  assert.equal(Format.ratioLevel(1.5), 'full');
});

test('the percentage and level are consistent with each other at the boundaries', () => {
  for (const ratio of [0.7999, 0.8, 0.9999, 1]) {
    const percent = parseInt(Format.ratioPercent(ratio), 10);
    const level = Format.ratioLevel(ratio);
    assert.equal(percent >= 100, level === 'full', String(ratio));
    assert.equal(percent >= 80 && percent < 100, level === 'warn', String(ratio));
  }
});

test('incomplete usage data is unknown, not zero or full', () => {
  for (const value of [null, undefined, '0.5', NaN, Infinity, -0.1, {}]) {
    assert.equal(Format.ratioPercent(value), '—', String(value));
    assert.equal(Format.ratioLevel(value), 'unknown', String(value));
  }
});

test('the bar width is clamped to 0 through 100', () => {
  assert.equal(Format.ratioWidth(0), 0);
  assert.equal(Format.ratioWidth(0.456), 45);
  assert.equal(Format.ratioWidth(1), 100);
  assert.equal(Format.ratioWidth(7), 100);
  for (const value of [null, undefined, -1, NaN, 'x']) { assert.equal(Format.ratioWidth(value), 0, String(value)); }
});

test('counts get thousands separators and bad data shows a dash', () => {
  assert.equal(Format.count(0), '0');
  assert.equal(Format.count(999), '999');
  assert.equal(Format.count(1000), '1,000');
  assert.equal(Format.count(1234567), '1,234,567');
  assert.equal(Format.count(-12345), '-12,345');
  for (const value of [null, undefined, '12', NaN, Infinity]) { assert.equal(Format.count(value), '—', String(value)); }
});

test('a public id is shortened to its first eight characters for tables', () => {
  assert.equal(Format.shortId('4d6f5c2a-1b3e-4f70-9a8c-0123456789ab'), '4d6f5c2a');
  for (const value of [null, undefined, '', 123]) { assert.equal(Format.shortId(value), '—', String(value)); }
});

test('the suspension note appears only while the suspension lasts, with the resume time in Taipei time', () => {
  const until = '2026-10-29T04:50:00+00:00';
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T04:00:00Z')), '客服對話暫停中，12:50 恢復');
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T04:50:00Z')), '');       // 到點就解除
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T05:00:00Z')), '');
  for (const value of [null, undefined, '', 'nope']) { assert.equal(Format.suspensionText(value, 0), '', String(value)); }
});

test('audit details are turned into one readable line and never throw on odd shapes', () => {
  assert.equal(Format.auditDetailText('user.change_tier', { from_tier: 'free', to_tier: 'plus' }), '一般 → 進階');
  assert.equal(Format.auditDetailText('user.suspend', { from_status: 'active' }), '原狀態：使用中');
  assert.equal(Format.auditDetailText('user.unsuspend', { to_status: 'pending_verification' }), '還原為：待驗證信箱');
  assert.equal(Format.auditDetailText('user.lookup', { via: 'email', found: 1 }), '以信箱');
  assert.equal(Format.auditDetailText('user.lookup', { via: 'id', found: 0 }), '以識別碼，查無此人');
  assert.equal(Format.auditDetailText('failures.read', { count: 3 }), '3 筆');
  assert.equal(Format.auditDetailText('reauth.failed', { attempted: 'user.suspend' }), '嘗試：停權');
  assert.equal(Format.auditDetailText('reauth.failed', { attempted: 'account.unlock' }), '嘗試：解除帳號鎖定');
  assert.equal(Format.auditDetailText('account.lock', { reason: 'rate_limited' }), '原因：短時間內訊息過多');
  assert.equal(Format.auditDetailText('account.lock', { reason: 'identity_rejected' }), '原因：登入或年齡確認多次被拒');
  assert.equal(Format.auditDetailText('appeal.submit', { length: 42 }), '42 字');
  assert.equal(Format.auditDetailText('appeal.read', { count: 2 }), '2 則');
  assert.equal(Format.auditDetailText('account.unlock', {}), '');
  assert.equal(Format.lockReasonLabel('rate_limited'), '短時間內訊息過多');
  assert.equal(Format.lockReasonLabel('something_new'), 'something_new');          // 不認得的值退回原始代碼
  for (const detail of [null, undefined, {}, [], 'text', 5]) {
    for (const action of ACTIONS.concat(['something.else'])) {
      assert.equal(typeof Format.auditDetailText(action, detail), 'string', action + ' ' + JSON.stringify(detail));
    }
  }
  assert.equal(Format.auditDetailText('conversation.read', {}), '');
  assert.equal(Format.auditDetailText('something.else', { x: 1 }), '');
});
