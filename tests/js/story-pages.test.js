'use strict';
/* rpg-story-pages 規格「劇情依句分段」與「串流期間逐段到達」的純函式（story-pages.js）：
 * 句界（句末標點、連續標點、收尾引號與括號）、引號深度、換行硬分界、3 句與 150 字上限、單句超長的切法（逗號、分號、頓號，其次字素安全的硬切）、
 * 串流的「湊滿才釋出」與「已釋出的段永不改變」，以及性質測試（串接還原原文、任何前綴的釋出結果都是完整結果的前綴）。純函式，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const FILE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'story-pages.js');
const { paginate, PAGE_MAX_SENTENCES, PAGE_MAX_CHARS } = require(FILE);

const finalPages = (text) => paginate(text, { done: true }).pages;
const streamPages = (text) => paginate(text, { done: false }).pages;
const strip = (text) => text.replace(/\s+/g, '');
const graphemeList = (text) => Array.from(new Intl.Segmenter('zh-Hant', { granularity: 'grapheme' }).segment(text), (part) => part.segment);

// ---- 規格列出的情境 -----------------------------------------------------------------------------------------------

test('the limits are three sentences and about 150 characters', () => {
  assert.equal(PAGE_MAX_SENTENCES, 3);
  assert.equal(PAGE_MAX_CHARS, 150);
});

test('seven short sentences without line breaks become pages of 3, 3 and 1 sentences, and concatenate back to the text', () => {
  const text = '她推開門。屋裡很暗。有人在咳嗽。燈亮了。老人抬起頭。他笑了。你好。';
  const pages = finalPages(text);
  assert.deepEqual(pages, ['她推開門。屋裡很暗。有人在咳嗽。', '燈亮了。老人抬起頭。他笑了。', '你好。']);
  assert.equal(pages.join(''), text);
});

test('a line break is a hard boundary: two one-sentence paragraphs are two pages, not one', () => {
  assert.deepEqual(finalPages('第一段。\n第二段。'), ['第一段。', '第二段。']);
  assert.deepEqual(finalPages('第一段。\r\n第二段。'), ['第一段。', '第二段。']);
  assert.deepEqual(finalPages('第一段。\r第二段。'), ['第一段。', '第二段。']);
  assert.deepEqual(finalPages('第一段。\n\n\n第二段。\n'), ['第一段。', '第二段。']);        // 空行不產生空段
});

test('a 200-character sentence with commas is cut at the commas into pages of at most 150 characters, with no character lost', () => {
  const sentence = `${'甲'.repeat(80)}，${'乙'.repeat(60)}，${'丙'.repeat(57)}。`;
  assert.equal(sentence.length, 200);
  const pages = finalPages(sentence);
  assert.ok(pages.length >= 2);
  for (const page of pages) { assert.ok(page.length <= 150, `長度 ${page.length}`); }
  assert.equal(pages.join(''), sentence);
  assert.deepEqual(pages.map((page) => page.length), [142, 58]);                          // 盡量湊滿：80+1+60+1 = 142，其餘為 57+1
  assert.ok(pages[0].endsWith('，'), '切在逗號之後');
});

test('semicolons and enumeration commas are cut points too, and half-width commas as well', () => {
  const sentence = `${'甲'.repeat(100)}；${'乙'.repeat(100)}`;
  assert.deepEqual(finalPages(sentence).map((page) => page.length), [101, 100]);
  const enumeration = `${'丙'.repeat(100)}、${'丁'.repeat(100)}。`;
  assert.deepEqual(finalPages(enumeration).map((page) => page.length), [101, 101]);
  const ascii = `${'戊'.repeat(100)},${'己'.repeat(100)}`;
  assert.deepEqual(finalPages(ascii).map((page) => page.length), [101, 100]);
});

test('with nowhere to cut, a long run is cut at 150 characters', () => {
  const pages = finalPages(`${'字'.repeat(320)}。`);
  assert.deepEqual(pages.map((page) => page.length), [150, 150, 21]);
  assert.equal(pages.join(''), `${'字'.repeat(320)}。`);
});

test('a comma-free tail of a long sentence can still be joined with the previous clauses up to the limit', () => {
  const sentence = `${'甲'.repeat(100)}，${'乙'.repeat(200)}。`;                          // 第二小句本身超過上限：硬切，剩下的一塊不與前一塊重複
  const pages = finalPages(sentence);
  assert.equal(pages.join(''), sentence);
  for (const page of pages) { assert.ok(page.length <= 150); }
});

test('a closing quote after the final punctuation stays with its sentence, and the next sentence does not start with it', () => {
  // 四句：「他低聲說：「我來晚了……了。」」「「不，你來得正好。」」「她答道。」「夜風吹起。」——收尾引號留在前一句，第二段從「夜」開始
  const pages = finalPages('他低聲說：「我來晚了……了。」「不，你來得正好。」她答道。夜風吹起。');
  assert.deepEqual(pages, ['他低聲說：「我來晚了……了。」「不，你來得正好。」她答道。', '夜風吹起。']);
  const split = finalPages('甲說：「走了。」乙說：「好。」丙說：「慢。」丁說：「等。」');
  assert.deepEqual(split, ['甲說：「走了。」乙說：「好。」丙說：「慢。」', '丁說：「等。」']);
  for (const page of split) { assert.ok(!/^[」』）)]/.test(page), page); }
});

test('closing brackets after the final punctuation stay with their sentence', () => {
  assert.deepEqual(finalPages('她笑了。）他走了。）風起了。）雨停了。'), ['她笑了。）他走了。）風起了。）', '雨停了。']);
  assert.deepEqual(finalPages('（他笑了。）甲。乙。丙。'), ['（他笑了。）甲。乙。', '丙。']);
});

test('punctuation inside quotation marks is not a sentence boundary: a whole quoted speech is one sentence', () => {
  const speech = '「你來了。我等你很久了。你知道嗎？」';
  // 引號內有三個句末標點；若被當成句界，這一句會變成三句，湊滿三句之後下一句就會被擠到下一段。
  const pages = finalPages(`${speech}老人說。他笑了。她哭了。`);
  assert.deepEqual(pages, [`${speech}老人說。他笑了。`, '她哭了。']);
  const twoSpeeches = finalPages(`${speech.repeat(2)}甲。乙。丙。丁。`);                         // 兩段引號各算一句，再加「甲。」剛好三句
  assert.deepEqual(twoSpeeches, [`${speech}${speech}甲。`, '乙。丙。丁。']);
});

test('nested single-corner quotes and curly double quotes keep their punctuation inside as well', () => {
  assert.deepEqual(finalPages('他說：「她說『走吧。快走。』然後離開。」風停了。'), ['他說：「她說『走吧。快走。』然後離開。」風停了。']);
  assert.deepEqual(finalPages('他說：“走吧。快走。”風停了。月亮出來了。燈亮了。'), ['他說：“走吧。快走。”風停了。月亮出來了。', '燈亮了。']);
});

test('a line break resets the quote depth, so an unclosed quote cannot swallow the next paragraph', () => {
  assert.deepEqual(finalPages('他說：「走吧。\n風停了。月亮出來了。'), ['他說：「走吧。', '風停了。月亮出來了。']);
});

test('a stray closing quote at depth 0 does nothing, and an unclosed quote only swallows the rest of its own paragraph', () => {
  assert.deepEqual(finalPages('走吧。」風停了。月亮出來了。燈亮了。'), ['走吧。」風停了。月亮出來了。', '燈亮了。']);
  assert.deepEqual(finalPages('他說：「走吧。風停了。月亮出來了。燈亮了。'), ['他說：「走吧。風停了。月亮出來了。燈亮了。']);
});

test('empty text, whitespace only and non-strings have no pages', () => {
  for (const junk of ['', ' ', '\n', '\r\n  \t\n', '　　', undefined, null, 5, {}]) {
    assert.deepEqual(paginate(junk, { done: true }), { pages: [], pending: '' }, String(junk));
    assert.deepEqual(paginate(junk, { done: false }), { pages: [], pending: '' }, String(junk));
  }
  assert.deepEqual(paginate('甲。'), { pages: [], pending: '甲。' });                          // 沒有選項：視為串流中
  assert.deepEqual(paginate('甲。', {}), { pages: [], pending: '甲。' });
  assert.deepEqual(paginate('甲。', { done: 'yes' }), { pages: [], pending: '甲。' });         // 只有真值 true 才算結束
});

test('consecutive terminators are one group: ellipses, mixed marks and a leading ellipsis stay with their sentence', () => {
  assert.deepEqual(finalPages('什麼？！真的假的……甲。乙。丙。'), ['什麼？！真的假的……甲。', '乙。丙。']);
  assert.deepEqual(finalPages('什麼？！她愣住了。……不可能。他低下頭。'), ['什麼？！她愣住了。……不可能。', '他低下頭。']);
  assert.deepEqual(finalPages('……真的嗎？他問。她點頭。他笑了。'), ['……真的嗎？他問。她點頭。', '他笑了。']);   // 開頭的「……」不獨立成一句
  assert.deepEqual(finalPages('。。。他走了。'), ['。。。他走了。']);
});

test('a text without any terminator is one page when it is short, and is cut at commas or 150 characters when it is long', () => {
  assert.deepEqual(finalPages('夜深了'), ['夜深了']);
  const noPunctuation = '風'.repeat(400);
  assert.deepEqual(finalPages(noPunctuation).map((page) => page.length), [150, 150, 100]);
});

test('spaces between sentences are kept inside a page and trimmed at page edges; full-width indentation is dropped', () => {
  assert.deepEqual(finalPages('　　Hello. World. Again. Last one.'), ['Hello. World. Again. Last one.']);          // 半形句點不是句末標點：整句一段
  assert.deepEqual(finalPages('　　夜深了。 風停了。 　月亮出來了。 燈亮了。'), ['夜深了。 風停了。 　月亮出來了。', '燈亮了。']);
});

test('the character limit also breaks pages between sentences: a sentence that would push the page over 150 starts a new page', () => {
  const first = `${'甲'.repeat(60)}。`;
  const second = `${'乙'.repeat(60)}。`;
  const third = `${'丙'.repeat(60)}。`;
  assert.deepEqual(finalPages(first + second + third), [first + second, third]);             // 61 + 61 = 122；再加 61 超過 150
  assert.deepEqual(finalPages(first + second), [first + second]);
});

test('a sentence longer than the limit is never merged with its neighbours', () => {
  const short = '她停下腳步。';
  const long = `${'甲'.repeat(100)}，${'乙'.repeat(100)}。`;
  const pages = finalPages(short + long + short);
  assert.equal(pages[0], short);
  assert.equal(pages[pages.length - 1], short);
  assert.equal(pages.length, 4);
});

test('a hard cut never splits a grapheme cluster (emoji with joiners, flags and combining marks)', () => {
  const family = '👨‍👩‍👧';                                                                       // 一個字素，8 個 UTF-16 單位
  const text = family.repeat(40);                                                            // 320 單位，沒有標點
  const pages = finalPages(text);
  assert.ok(pages.length >= 3);
  for (const page of pages) { assert.ok(page.length <= 150); }
  assert.equal(pages.join(''), text);
  assert.equal(pages.reduce((sum, page) => sum + graphemeList(page).length, 0), graphemeList(text).length, '每一塊單獨計算的字素數加起來等於整段的字素數');
  const flags = '🇹🇼'.repeat(60);                                                              // 國旗是兩個區域指示符號組成的一個字素
  assert.equal(finalPages(flags).reduce((sum, page) => sum + graphemeList(page).length, 0), 60);
  const accents = 'é'.repeat(200);                                                      // e + 組合重音
  assert.equal(finalPages(accents).join(''), accents);
});

test('one grapheme longer than the limit is kept whole instead of being cut', () => {
  const zalgo = `a${'̀'.repeat(200)}`;
  assert.equal(finalPages(zalgo).join(''), zalgo);
});

// ---- 串流：湊滿才釋出 -------------------------------------------------------------------------------------------------------

test('three complete sentences are released as soon as the next character shows the third one is finished', () => {
  const streamed = paginate('甲說話。乙說話。丙說話。丁', { done: false });
  assert.deepEqual(streamed.pages, ['甲說話。乙說話。丙說話。']);
  assert.equal(streamed.pending, '丁');
});

test('a sentence is not complete while its terminator group could still grow: the page waits for the next character', () => {
  assert.deepEqual(paginate('甲。乙。丙。', { done: false }), { pages: [], pending: '甲。乙。丙。' });
  assert.deepEqual(paginate('甲。乙。丙。\n', { done: false }).pages, ['甲。乙。丙。']);          // 換行也算「後面出現了下一個東西」
  assert.deepEqual(paginate('甲。乙。丙。', { done: true }).pages, ['甲。乙。丙。']);
  // 「……」分成兩個片段到達：只收到第一個「…」時不能釋出
  assert.deepEqual(paginate('甲。乙。丙…', { done: false }).pages, []);
  assert.deepEqual(paginate('甲。乙。丙……他', { done: false }).pages, ['甲。乙。丙……']);
});

test('two complete sentences are not enough: nothing is released yet', () => {
  assert.deepEqual(paginate('甲說話。乙說話。丙', { done: false }), { pages: [], pending: '甲說話。乙說話。丙' });
});

test('a sentence without a terminator is not released until done, and then it becomes the last page', () => {
  assert.deepEqual(paginate('甲說話。乙說話', { done: false }), { pages: [], pending: '甲說話。乙說話' });
  assert.deepEqual(paginate('甲說話。乙說話', { done: true }), { pages: ['甲說話。乙說話'], pending: '' });
  assert.deepEqual(paginate('甲。乙。丙。丁。戊', { done: true }).pages, ['甲。乙。丙。', '丁。戊']);
});

test('a line break releases everything before it, even fewer than three sentences', () => {
  assert.deepEqual(paginate('甲說話。\n乙', { done: false }), { pages: ['甲說話。'], pending: '乙' });
  assert.deepEqual(paginate('甲說話\n乙', { done: false }), { pages: ['甲說話'], pending: '乙' });
});

test('a page is released when the next sentence, even only its beginning, would not fit under the character limit', () => {
  const first = `${'甲'.repeat(100)}。`;
  assert.deepEqual(paginate(first, { done: false }).pages, []);
  assert.deepEqual(paginate(`${first}${'乙'.repeat(60)}`, { done: false }), { pages: [first], pending: '乙'.repeat(60) });   // 下一句只收到 60 字，但 101 + 60 已超過 150
  assert.deepEqual(paginate(`${first}${'乙'.repeat(40)}`, { done: false }).pages, []);                                      // 101 + 40 還放得下，要等更多
});

test('an unfinished sentence that is already longer than the limit closes the page before it', () => {
  const text = `她停下腳步。${'甲'.repeat(160)}`;
  assert.deepEqual(paginate(text, { done: false }), { pages: ['她停下腳步。'], pending: '甲'.repeat(160) });
});

test('released pages do not change while the rest of the text arrives, token by token', () => {
  const text = '她推開門。屋裡很暗。有人在咳嗽。燈亮了。老人抬起頭，看著你。他笑了。\n你好。\n「你來了。我等你很久了。」';
  const expected = finalPages(text);
  let released = [];
  for (let end = 1; end <= text.length; end += 1) {
    const { pages } = paginate(text.slice(0, end), { done: false });
    assert.deepEqual(pages.slice(0, released.length), released, `在 ${end} 個字之後，已釋出的段被改動`);
    assert.deepEqual(pages, expected.slice(0, pages.length), `在 ${end} 個字之後，釋出的段不是完整結果的前綴`);
    released = pages;
  }
  assert.deepEqual(paginate(text, { done: true }).pages, expected);
});

test('the player keeps reading at their own pace: pages already released are the same after more text arrives', () => {
  const head = '甲說話。乙說話。丙說話。丁說話。';
  const first = paginate(head, { done: false });
  const second = paginate(`${head}戊說話。己說話。庚說話。辛說話。`, { done: false });
  assert.deepEqual(second.pages.slice(0, first.pages.length), first.pages);
});

// ---- 性質測試：串接還原原文、前綴一致、長度上限 ---------------------------------------------------------------------------

/* 可重現的亂數（mulberry32）：失敗時能以同一個種子重現。 */
function random(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const PIECES = {
  words: ['她', '你', '門', '燈', '風', '夜', '走', '說', '看', '來', '了', '嗎', '呢', 'abc', 'Hi', '3.5', '的', '很'],
  ends: ['。', '！', '？', '…', '……', '！？', '。。'],
  opens: ['「', '『', '“', '（', '('],
  closes: ['」', '』', '”', '）', ')'],
  marks: ['，', '、', '；', ',', ';'],
  spaces: [' ', '　', '  ', '\t'],
  breaks: ['\n', '\r\n', '\n\n', '\r'],
  odd: ['😀', '👨‍👩‍👧', '🇹🇼', 'é', '​']
};
const WEIGHTS = [['words', 40], ['ends', 12], ['opens', 5], ['closes', 5], ['marks', 8], ['spaces', 4], ['breaks', 3], ['odd', 3]];

function randomText(next, pieces) {
  const total = WEIGHTS.reduce((sum, [, weight]) => sum + weight, 0);
  let out = '';
  for (let count = 0; count < pieces; count += 1) {
    let roll = next() * total;
    let kind = WEIGHTS[0][0];
    for (const [name, weight] of WEIGHTS) { if (roll < weight) { kind = name; break; } roll -= weight; }
    const list = PIECES[kind];
    out += list[Math.floor(next() * list.length)];
  }
  return out;
}

/* 偶爾插入一長串沒有標點或只有逗號的文字，讓超長句的切法也被涵蓋。 */
function randomStory(next) {
  let out = randomText(next, 5 + Math.floor(next() * 60));
  if (next() < 0.3) { out += '字'.repeat(100 + Math.floor(next() * 300)); }
  if (next() < 0.3) { out += ('句'.repeat(20 + Math.floor(next() * 40)) + '，').repeat(2 + Math.floor(next() * 6)); }
  out += randomText(next, Math.floor(next() * 30));
  return out;
}

const CASES = 400;

test('property: the pages joined together restore the text apart from whitespace (done and streaming)', () => {
  const next = random(20261123);
  for (let index = 0; index < CASES; index += 1) {
    const text = randomStory(next);
    const done = paginate(text, { done: true });
    assert.equal(done.pending, '', `案例 ${index}`);
    assert.equal(strip(done.pages.join('')), strip(text), `案例 ${index}: ${JSON.stringify(text)}`);
    const cut = Math.floor(next() * (text.length + 1));
    const prefix = text.slice(0, cut);
    const partial = paginate(prefix, { done: false });
    assert.equal(strip(partial.pages.join('') + partial.pending), strip(prefix), `案例 ${index} 前綴 ${cut}`);
  }
});

test('property: every page is non-empty, trimmed, has no line break, and is at most 150 characters (unless a single grapheme is longer)', () => {
  const next = random(7);
  for (let index = 0; index < CASES; index += 1) {
    const text = randomStory(next);
    for (const page of finalPages(text)) {
      assert.ok(page.length > 0, '空段');
      assert.equal(page, page.trim(), '頭尾有空白');
      assert.ok(!/[\r\n]/.test(page), '段裡有換行');
      assert.ok(page.length <= PAGE_MAX_CHARS, `案例 ${index}：長度 ${page.length}`);
    }
  }
});

test('property: for every prefix, the released pages are a prefix of the final pages, and later prefixes only add pages', () => {
  const next = random(424242);
  for (let index = 0; index < 120; index += 1) {
    const text = randomStory(next);
    const expected = finalPages(text);
    let previous = [];
    for (let end = 0; end <= text.length; end += 1) {
      const { pages } = paginate(text.slice(0, end), { done: false });
      assert.ok(pages.length >= previous.length, `案例 ${index}：在 ${end} 個字之後釋出的段變少`);
      assert.deepEqual(pages.slice(0, previous.length), previous, `案例 ${index}：在 ${end} 個字之後，已釋出的段被改動`);
      assert.deepEqual(pages, expected.slice(0, pages.length), `案例 ${index}：在 ${end} 個字之後，釋出的段不是完整結果的前綴\n${JSON.stringify(text)}`);
      previous = pages;
    }
  }
});

test('property: a text that arrives in random chunks (including cuts inside a surrogate pair) gives the same final pages', () => {
  const next = random(99);
  for (let index = 0; index < CASES; index += 1) {
    const text = randomStory(next);
    const expected = finalPages(text);
    let received = '';
    let released = [];
    let rest = text;
    while (rest) {
      const size = 1 + Math.floor(next() * 7);
      received += rest.slice(0, size);
      rest = rest.slice(size);
      released = paginate(received, { done: false }).pages;
      assert.deepEqual(released, expected.slice(0, released.length), `案例 ${index}`);
    }
    assert.deepEqual(paginate(received, { done: true }).pages, expected, `案例 ${index}`);
  }
});

test('property: the same text always gives the same result (no state is kept between calls)', () => {
  const next = random(5);
  for (let index = 0; index < 100; index += 1) {
    const text = randomStory(next);
    assert.deepEqual(paginate(text, { done: true }), paginate(text, { done: true }));
    assert.deepEqual(paginate(text, { done: false }), paginate(text, { done: false }));
  }
});

test('property: a page never holds more than three sentences worth of terminators outside quotes, unless it is a piece of an over-long sentence', () => {
  // 沒有引號與括號的文字：每一句剛好一個「。」，所以一段（不是超長句的切塊）至多三個「。」。
  const next = random(31337);
  for (let index = 0; index < CASES; index += 1) {
    let text = '';
    for (let count = 0; count < 1 + Math.floor(next() * 12); count += 1) { text += `${'甲'.repeat(1 + Math.floor(next() * 40))}。${next() < 0.15 ? '\n' : ''}`; }
    for (const page of finalPages(text)) {
      assert.ok((page.match(/。/g) || []).length <= PAGE_MAX_SENTENCES, page);
    }
  }
});

// ---- 不碰環境、不留痕跡 ---------------------------------------------------------------------------------------------------------

test('the module runs in a bare sandbox with no browser APIs, exposes Game.paginate, and never writes to the console', () => {
  const logged = [];
  const sandbox = { console: { log: (...args) => logged.push(args), warn: (...args) => logged.push(args), error: (...args) => logged.push(args) }, Intl, Array, String, RegExp, Object, Symbol };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), sandbox);
  assert.equal(typeof sandbox.Game.paginate, 'function');
  assert.equal(sandbox.Game.PAGE_MAX_CHARS, 150);
  const result = sandbox.Game.paginate('玩家的祕密行動。\n他說：「不要記下來。」', { done: true });
  assert.equal(result.pages.length, 2);
  assert.deepEqual(logged, []);
});

test('the file has no browser storage, no network, no DOM writes and no HTML rendering (player text is plain text)', () => {
  const source = fs.readFileSync(FILE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'fetch(', 'XMLHttpRequest', 'innerHTML', 'v-html', 'console.', 'Math.random', 'Date.now']) {
    assert.ok(!source.includes(forbidden), forbidden);
  }
});
