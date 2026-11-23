'use strict';
/* rpg-story-pages 規格的邊界情境（story-pages.js）：變異驗證（rpg-story-view 8.1）找出原本沒有測試守住的判斷，這裡逐一補上——
 * 每一種句末標點、『』與巢狀引號、句末空白與收尾括號、字數剛好 150 與 151 的邊界（湊段與串流釋出）、含空白的字數計算、
 * 超長句的逗號切法（剛好 150、硬切的最後一塊留給後面的小句、連續的逗號、逗號後的空白）。純函式，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const FILE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'story-pages.js');
const { paginate, PAGE_MAX_CHARS } = require(FILE);

const finalPages = (text) => paginate(text, { done: true }).pages;
const streamResult = (text) => paginate(text, { done: false });
const rep = (char, count) => char.repeat(count);

// ---- 句界：每一種句末標點、引號與括號 --------------------------------------------------------------------------------

for (const mark of ['。', '！', '？', '…']) {
  test(`the terminator ${mark} ends a sentence: four one-word sentences become pages of three and one`, () => {
    const text = `甲${mark}乙${mark}丙${mark}丁${mark}`;
    assert.deepEqual(finalPages(text), [`甲${mark}乙${mark}丙${mark}`, `丁${mark}`]);
  });
}

test('terminators inside 『』 do not end the sentence, and the quote closing lets the next sentence start', () => {
  const text = '甲說『一。二。三。四。』乙說。丙說。丁說。';
  assert.deepEqual(finalPages(text), ['甲說『一。二。三。四。』乙說。丙說。', '丁說。']);
});

test('terminators inside “” do not end the sentence', () => {
  assert.deepEqual(finalPages('甲說“一。二。三。四。”乙說。丙說。丁說。'), ['甲說“一。二。三。四。”乙說。丙說。', '丁說。']);
});

test('a nested quote closing does not end the outer quote: terminators after 』 but before 」 still do not split', () => {
  const text = '他說「他喊『走』。快走。」她笑了。丙。丁。';
  assert.deepEqual(finalPages(text), ['他說「他喊『走』。快走。」她笑了。丙。', '丁。']);
});

test('spaces and a closing bracket after a terminator stay with the sentence that ended, not the next one', () => {
  assert.deepEqual(finalPages('甲。乙。丙。 ）丁。'), ['甲。乙。丙。 ）', '丁。']);
  assert.deepEqual(finalPages('甲。乙。丙。）丁。'), ['甲。乙。丙。）', '丁。']);
});

// ---- 湊段的邊界：字數剛好 150 放得下，151 放不下 ------------------------------------------------------------------------

const unit = (count) => `${rep('字', count - 1)}。`;      // 長度剛好 count 的一句

test('two sentences that together are exactly 150 characters stay on one page; 151 splits them', () => {
  assert.deepEqual(finalPages(unit(75) + unit(75)), [unit(75) + unit(75)]);
  assert.equal(finalPages(unit(75) + unit(75))[0].length, PAGE_MAX_CHARS);
  assert.deepEqual(finalPages(unit(75) + unit(76)), [unit(75), unit(76)]);
});

test('while streaming, a page is not released when the next sentence would still fit exactly (150), only when it would not (151)', () => {
  const head = unit(75);
  assert.deepEqual(streamResult(head + rep('字', 75)), { pages: [], pending: head + rep('字', 75) });
  assert.deepEqual(finalPages(head + rep('字', 75)), [head + rep('字', 75)]);               // 完整結果就是一段：串流不能提早釋出而讓它被拆開
  assert.deepEqual(streamResult(head + rep('字', 76)), { pages: [head], pending: rep('字', 76) });
});

test('the length of a sentence includes the whitespace after it, so a page never exceeds 150 characters', () => {
  const first = `${rep('字', 74)}。   `;       // 78 字（含 3 個空白）
  const second = unit(74);                   // 74 字：78 + 74 = 152，放不下
  const pages = finalPages(first + second);
  assert.deepEqual(pages, [unit(75), unit(74)]);
  for (const page of pages) { assert.ok(page.length <= PAGE_MAX_CHARS, `長度 ${page.length}`); }
});

// ---- 超長句：逗號切法的邊界 ---------------------------------------------------------------------------------------------

test('an over-long sentence is cut at commas, and clauses that together are exactly 150 characters stay on one page', () => {
  const clauseA = `${rep('甲', 74)}，`;
  const clauseB = `${rep('乙', 74)}，`;
  const clauseC = `${rep('丙', 30)}。`;
  assert.deepEqual(finalPages(clauseA + clauseB + clauseC), [clauseA + clauseB, clauseC]);
});

test('the last piece of a hard-cut clause stays open so the following clauses join it', () => {
  const long = `${rep('甲', 160)}，`;       // 沒有可切的逗號，只能硬切成 150 與 11（含逗號）
  const tail = `${rep('乙', 5)}。`;
  assert.deepEqual(finalPages(long + tail), [rep('甲', 150), `${rep('甲', 10)}，${rep('乙', 5)}。`]);
});

test('consecutive commas are one cut point: a page never starts with a comma', () => {
  const sentence = `${rep('甲', 100)}，${rep('乙', 48)}，，${rep('丙', 10)}。`;
  const pages = finalPages(sentence);
  assert.deepEqual(pages, [`${rep('甲', 100)}，`, `${rep('乙', 48)}，，${rep('丙', 10)}。`]);
  for (const page of pages) { assert.ok(!/^[，、；]/.test(page), page.slice(0, 3)); }
});

test('whitespace after a comma belongs to the clause before it when measuring what fits on a page', () => {
  const sentence = `${rep('甲', 100)}，${rep('乙', 48)}， ${rep('丙', 30)}。`;
  assert.deepEqual(finalPages(sentence), [`${rep('甲', 100)}，`, `${rep('乙', 48)}， ${rep('丙', 30)}。`]);
});
