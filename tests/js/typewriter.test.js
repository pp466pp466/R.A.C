// 打字機緩衝區的單元測試（Node 內建測試執行器，無任何 npm 相依）。
// 執行：node --test tests/js/
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { TypewriterBuffer } = require('../../app/static/js/ui/typewriter.js');

/** 假時鐘：讓測試不必真的等待，也不受機器速度影響。 */
function fakeClock() {
  let now = 0;
  let callback = null;
  let handle = 0;
  return {
    scheduler: {
      now: () => now,
      request(fn) { callback = fn; return ++handle; },
      cancel() { callback = null; }
    },
    advance(ms, step = 16) {
      const end = now + ms;
      while (now < end && callback) {
        now = Math.min(end, now + step);
        const fn = callback;
        callback = null;
        fn(now);
      }
      now = end;
    }
  };
}

function setup(options = {}) {
  const clock = fakeClock();
  const updates = [];
  let doneCount = 0;
  const buffer = new TypewriterBuffer({
    scheduler: clock.scheduler,
    onUpdate: (text) => updates.push(text),
    onDone: () => { doneCount += 1; },
    ...options
  });
  return { buffer, clock, updates, done: () => doneCount };
}

const graphemes = (text) => Array.from(new Intl.Segmenter('zh-Hant', { granularity: 'grapheme' }).segment(text), (s) => s.segment);
const boundaryLengths = (text) => {
  const lengths = new Set([0]);
  let total = 0;
  for (const g of graphemes(text)) { total += g.length; lengths.add(total); }
  return lengths;
};
const hasLoneSurrogate = (text) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(text);

test('完整文字逐字顯示，最終內容與原文完全相同，且只通知完成一次', () => {
  const original = '您好，這是一段客服回覆。請稍候，我來幫您查詢。';
  const { buffer, clock, updates, done } = setup();
  buffer.play(original);
  clock.advance(10000);

  assert.equal(buffer.text, original);
  assert.equal(done(), 1);
  assert.ok(updates.length > 3, '應該是逐步顯示，而不是一次出現');
  assert.equal(updates.at(-1), original);
});

test('每一步顯示的都是原文的前綴，而且只增不減', () => {
  const original = '逐字顯示的內容必須永遠是原文的開頭部分。';
  const { buffer, clock, updates } = setup();
  buffer.play(original);
  clock.advance(10000);

  let previous = '';
  for (const text of updates) {
    assert.ok(original.startsWith(text), `「${text}」不是原文的前綴`);
    assert.ok(text.length > previous.length, '顯示內容必須持續增加');
    previous = text;
  }
});

test('片段陸續到達時，順序不錯亂，最終內容完整', () => {
  const { buffer, clock, updates, done } = setup();
  const parts = ['您好，', '我是', '智能客服。', '請問有什麼可以', '協助您的嗎？'];
  for (const part of parts) {
    buffer.push(part);
    clock.advance(120);
  }
  assert.equal(done(), 0, '尚未呼叫 finish，不應完成');
  buffer.finish();
  clock.advance(5000);

  const full = parts.join('');
  assert.equal(buffer.text, full);
  assert.equal(done(), 1);
  for (const text of updates) { assert.ok(full.startsWith(text)); }
});

test('中文、表情符號、ZWJ 組合符號與組合重音不會被切開', () => {
  const original = '中文😀👨‍👩‍👧‍👦é🇹🇼完';
  const allowed = boundaryLengths(original);
  const { buffer, clock, updates } = setup();
  buffer.play(original);
  clock.advance(20000);

  assert.equal(buffer.text, original);
  for (const text of updates) {
    assert.ok(allowed.has(text.length), `「${text}」停在字素中間`);
    assert.equal(hasLoneSurrogate(text), false, '出現了落單的代理字元');
  }
});

test('一個字素被拆在兩個片段之間時，不會先顯示一半', () => {
  const { buffer, clock, updates } = setup();
  buffer.push('👨‍');      // 男人加上 ZWJ，後面還沒到
  clock.advance(200);
  assert.deepEqual(updates, [], '字素尚未完整，不應顯示');
  buffer.push('👩‍👧');
  buffer.finish();
  clock.advance(5000);

  assert.equal(buffer.text, '👨‍👩‍👧');
  assert.ok(updates.every((t) => !t.endsWith('‍')), '不應停在 ZWJ 結尾');
});

test('組合重音在下一個片段才到達時，不會先顯示沒有重音的字母', () => {
  const { buffer, clock, updates } = setup();
  buffer.push('e');
  clock.advance(100);
  buffer.push('́x');
  buffer.finish();
  clock.advance(5000);

  assert.equal(buffer.text, 'éx');
  assert.ok(!updates.includes('e'), '不應先出現沒有重音的 e');
});

test('HTML 標記以原樣文字處理，不會被解析', () => {
  const html = '<script>alert(1)</script><img src=x onerror=alert(2)> &amp; <b>粗體</b>';
  const { buffer, clock } = setup();
  buffer.play(html);
  clock.advance(20000);
  assert.equal(buffer.text, html);          // 輸出是原樣字串；顯示時只用文字插值，不用 v-html
  assert.equal(typeof buffer.text, 'string');
});

test('積壓越多顯示越快', () => {
  const small = setup();
  small.buffer.push('字'.repeat(10) + '尾');
  small.clock.advance(200);

  const large = setup();
  large.buffer.push('字'.repeat(300) + '尾');
  large.clock.advance(200);

  assert.ok(large.buffer.text.length > small.buffer.text.length * 1.5,
    `積壓多的應顯著較快：${large.buffer.text.length} vs ${small.buffer.text.length}`);
});

test('一次到達的一般長度回覆仍保有逐字效果（不會瞬間全部出現），但也不會拖太久', () => {
  const reply = '字'.repeat(58);
  const { buffer, clock, done } = setup();
  buffer.play(reply);
  clock.advance(500);
  assert.ok(buffer.text.length > 0 && buffer.text.length < reply.length * 0.6,
    `0.5 秒時不該顯示超過六成：${buffer.text.length}/${reply.length}`);
  clock.advance(1500);
  assert.equal(buffer.text, reply);
  assert.equal(done(), 1);

  const long = setup();
  long.buffer.play('字'.repeat(300));
  long.clock.advance(4000);
  assert.equal(long.buffer.text.length, 300, '300 字的回覆應在 4 秒內顯示完');
});

test('沒有積壓時使用平穩的基本速度', () => {
  const { buffer, clock } = setup({ baseCharsPerSecond: 20 });
  buffer.push('字'.repeat(4) + '尾');
  clock.advance(1000);
  assert.ok(buffer.text.length <= 5);
});

test('finish 之後會加速補完，而且保證在期限內完成', () => {
  const { buffer, clock, done } = setup({ finishSeconds: 0.8 });
  buffer.push('字'.repeat(1500));   // 積壓很多
  clock.advance(100);
  assert.equal(done(), 0);
  buffer.finish();
  clock.advance(900);               // 最晚 0.8 秒補完（再留一點畫面更新的誤差）
  assert.equal(buffer.text.length, 1500);
  assert.equal(done(), 1);
});

test('預設期限 3 秒：超長文字也會在 finish 之後 3 秒內顯示完', () => {
  const { buffer, clock, done } = setup();
  buffer.play('字'.repeat(3000));
  clock.advance(3100);
  assert.equal(buffer.text.length, 3000);
  assert.equal(done(), 1);
});

test('不同的積壓量與期限設定，finish 之後都會在期限內補完', () => {
  for (const [size, finishSeconds] of [[50, 0.5], [400, 1], [5000, 0.8], [20, 0.3]]) {
    const { buffer, clock, done } = setup({ finishSeconds });
    buffer.push('字'.repeat(size));
    clock.advance(50);
    buffer.finish();
    clock.advance(finishSeconds * 1000 + 100);
    assert.equal(buffer.text.length, size, `${size} 個字、期限 ${finishSeconds} 秒`);
    assert.equal(done(), 1);
  }
});

test('沒有呼叫 finish 就不會完成，最後一個字素也不會提前釋出', () => {
  const { buffer, clock, done } = setup();
  buffer.push('ABC');
  clock.advance(10000);
  assert.equal(done(), 0);
  assert.equal(buffer.text, 'AB');     // 最後一個字素保留到 finish
  buffer.finish();
  clock.advance(1000);
  assert.equal(buffer.text, 'ABC');
  assert.equal(done(), 1);
});

test('沒有任何文字時 finish 也會通知完成，且只通知一次', () => {
  const { buffer, clock, done } = setup();
  buffer.finish();
  buffer.finish();
  clock.advance(500);
  assert.equal(buffer.text, '');
  assert.equal(done(), 1);
});

test('finish 之後再 push 會明確報錯', () => {
  const { buffer } = setup();
  buffer.finish();
  assert.throws(() => buffer.push('晚到的文字'), /finish/);
});

test('cancel 之後不再更新，也不會通知完成', () => {
  const { buffer, clock, updates, done } = setup();
  buffer.play('這段文字會在顯示到一半時被取消，後面不應該再出現。');
  clock.advance(200);
  buffer.cancel();
  const frozen = buffer.text;
  const updateCount = updates.length;
  clock.advance(20000);
  assert.equal(buffer.text, frozen);
  assert.equal(updates.length, updateCount);
  assert.equal(done(), 0);
});

test('flush 立刻顯示全部並通知完成', () => {
  const { buffer, done } = setup();
  buffer.push('一次顯示完畢');
  buffer.flush();
  assert.equal(buffer.text, '一次顯示完畢');
  assert.equal(done(), 1);
});

test('reset 之後可以重新使用', () => {
  const { buffer, clock, done } = setup();
  buffer.play('第一次');
  clock.advance(5000);
  assert.equal(done(), 1);
  buffer.reset();
  assert.equal(buffer.text, '');
  buffer.play('第二次');
  clock.advance(5000);
  assert.equal(buffer.text, '第二次');
  assert.equal(done(), 2);
});

test('平常使用正常，分頁在背景很久之後回來也不會一次噴出全部文字', () => {
  const { buffer, clock } = setup();
  buffer.push('字'.repeat(100) + '尾');
  clock.advance(32);                 // 開始顯示
  const before = buffer.text.length;
  clock.advance(60000, 60000);       // 一次跳過一分鐘（模擬背景分頁）
  const gained = buffer.text.length - before;
  assert.ok(gained < 100, `單次更新最多採計 200 毫秒，卻一次增加了 ${gained} 個字`);
});
