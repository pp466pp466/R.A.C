'use strict';
/* script-authoring 規格「里程碑預覽」：純前端、依觸發排序、不呼叫任何 API。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const { previewMilestones } = require('../../app/static/js/scripts/milestone-preview.js');

const row = (title, kind, value) => ({ title, kind, value });

test('three different triggers are listed in the order they would happen', () => {
  const items = previewMilestones([row('晚期', 'age', '60'), row('宗門大測', 'age', '20'), row('少年期', 'age', '12')], null);
  assert.deepEqual(items.map((item) => item.title), ['少年期', '宗門大測', '晚期']);
  assert.deepEqual(items.map((item) => item.label), ['玩家 12 歲時', '玩家 20 歲時', '玩家 60 歲時']);
});

test('age triggers come before year triggers and each group is ordered by its own value', () => {
  const items = previewMilestones([row('年份二', 'year', '1210'), row('年齡二', 'age', '30'), row('年份一', 'year', '1205'), row('年齡一', 'age', '8')], { start_year: 1200 });
  assert.deepEqual(items.map((item) => item.title), ['年齡一', '年齡二', '年份一', '年份二']);
});

test('a year trigger says how many years after the start it is when the calendar has a start year', () => {
  const [item] = previewMilestones([row('大戰', 'year', '1205')], { start_year: 1200 });
  assert.equal(item.label, '遊戲內第 1205 年（開局後第 5 年）');
  const [plain] = previewMilestones([row('大戰', 'year', '1205')], null);
  assert.equal(plain.label, '遊戲內第 1205 年');
  const [before] = previewMilestones([row('舊事', 'year', '1100')], { start_year: 1200 });
  assert.equal(before.label, '遊戲內第 1100 年');                          // 早於起始年：不編造「開局後」的說法，由伺服器回報錯誤
});

test('equal values keep the order they were written in', () => {
  const items = previewMilestones([row('甲', 'age', '20'), row('乙', 'age', '20'), row('丙', 'age', '20')], null);
  assert.deepEqual(items.map((item) => item.title), ['甲', '乙', '丙']);
});

test('incomplete or invalid triggers are left out of the preview instead of breaking it', () => {
  const items = previewMilestones([
    row('沒填數值', 'age', ''), row('不是數字', 'age', 'abc'), row('負數', 'age', '-5'), row('未知種類', 'month', '3'), null, 'x', row('完整', 'age', '25'),
  ], null);
  assert.deepEqual(items.map((item) => item.title), ['完整']);
});

test('a missing title shows a placeholder and notes are kept as plain text', () => {
  const [item] = previewMilestones([{ title: '  ', kind: 'age', value: 9, note: '  備註  ' }], null);
  assert.equal(item.title, '（未命名）');
  assert.equal(item.note, '備註');
});

test('server style milestones with a nested trigger work too', () => {
  const items = previewMilestones([{ title: '甲', trigger: { kind: 'age', value: 20 } }, { title: '乙', trigger: { kind: 'year', value: 1203 } }], { start_year: 1200 });
  assert.deepEqual(items.map((item) => item.label), ['玩家 20 歲時', '遊戲內第 1203 年（開局後第 3 年）']);
});

test('the calendar may be given with the editors field name and strings', () => {
  const [item] = previewMilestones([row('甲', 'year', '1203')], { calendar_start_year: '1200' });
  assert.ok(item.label.endsWith('（開局後第 3 年）'));
});

test('empty and non-array inputs give an empty preview', () => {
  for (const input of [[], null, undefined, 'x', 3, {}]) { assert.deepEqual(previewMilestones(input, null), []); }
});

test('the preview does not modify its input', () => {
  const input = [row('乙', 'age', '30'), row('甲', 'age', '10')];
  const before = JSON.stringify(input);
  previewMilestones(input, null);
  assert.equal(JSON.stringify(input), before);
});
