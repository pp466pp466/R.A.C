// 客服用量顯示邏輯的單元測試（純函式）。執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Usage = require('../../app/static/js/cs/cs-usage.js');

const usage = (used, limit, extra) => Object.assign({ tier: 'free', daily: { used, limit, resets_at: '2026-10-29T16:00:00+00:00' }, suspended_until: null }, extra || {});

test('the remaining turns are the limit minus the used turns and never negative', () => {
  assert.equal(Usage.remainingTurns(usage(3, 30)), 27);
  assert.equal(Usage.remainingTurns(usage(30, 30)), 0);
  assert.equal(Usage.remainingTurns(usage(35, 30)), 0);        // 多個裝置同時送出時顯示可能暫時超過
});

test('the sidebar line shows the remaining turns, says so when they are used up, and is empty without data', () => {
  assert.equal(Usage.remainingText(usage(3, 30)), '今日剩餘 27 輪');
  assert.equal(Usage.remainingText(usage(299, 300)), '今日剩餘 1 輪');
  assert.equal(Usage.remainingText(usage(30, 30)), '今日額度已用完');
  for (const missing of [null, undefined, {}, { daily: null }, { daily: { used: '3', limit: 30 } }, { daily: { used: 3 } }]) {
    assert.equal(Usage.remainingText(missing), '', JSON.stringify(missing));      // 取得失敗或資料不完整：整行隱藏
    assert.equal(Usage.remainingTurns(missing), null);
  }
});

test('Taipei time is UTC+8 with zero padding and wraps over midnight', () => {
  assert.equal(Usage.taipeiClock(Date.parse('2026-10-29T04:50:00Z')), '12:50');
  assert.equal(Usage.taipeiClock(Date.parse('2026-10-29T00:05:00Z')), '08:05');
  assert.equal(Usage.taipeiClock(Date.parse('2026-10-29T16:30:00Z')), '00:30');
  assert.equal(Usage.taipeiClock(Date.parse('2026-10-29T15:59:00Z')), '23:59');
});

test('a suspension is reported only while it lasts, with the resume time in Taipei time', () => {
  const data = usage(3, 30, { suspended_until: '2026-10-29T04:50:00+00:00' });
  const before = Date.parse('2026-10-29T04:00:00Z');
  const found = Usage.suspension(data, before);
  assert.deepEqual(found, { untilMs: Date.parse('2026-10-29T04:50:00Z'), text: '客服對話已暫停，將於 12:50（台灣時間）恢復。' });
  assert.equal(Usage.suspension(data, Date.parse('2026-10-29T04:50:00Z')), null);      // 到點就解除
  assert.equal(Usage.suspension(data, Date.parse('2026-10-29T05:00:00Z')), null);
});

test('no suspension when the field is empty, missing or not a date', () => {
  const now = Date.parse('2026-10-29T04:00:00Z');
  for (const value of [null, undefined, '', 'not a date', 12345]) {
    assert.equal(Usage.suspension(usage(0, 30, { suspended_until: value }), now), null, String(value));
  }
  assert.equal(Usage.suspension(null, now), null);
});

test('only the temporary rejections have a notice for queued messages', () => {
  assert.match(Usage.queuedNotice('rate_limited'), /速度太快/);
  assert.match(Usage.queuedNotice('system_busy'), /使用人數較多/);
  for (const code of ['daily_quota_exceeded', 'chat_suspended', 'service_unavailable', 'conversation_busy', null, undefined]) {
    assert.equal(Usage.queuedNotice(code), null, String(code));
  }
});