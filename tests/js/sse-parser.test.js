'use strict';
/* rpg-stream-client 規格「以 fetch 與 ReadableStream 接收事件」的解析部分（sse-parser.js）：
 * 一塊含多個事件、一個事件被切成任意小塊（含中文）、\r\n 結尾、註解行、未知事件名稱照樣回傳、壞 JSON 被丟棄而後續事件正常、
 * 不完整的尾端留在緩衝並在補齊後才出現。純函式，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const { createParser } = require(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'sse-parser.js'));

/* 後端 SseEvent.encode() 的格式：`event:` 一行、單行 JSON 的 `data:` 一行、空行。 */
const encode = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const feedAll = (parser, pieces) => pieces.flatMap((piece) => parser.feed(piece));
/* 依字元（UTF-16 單位）切成固定大小的塊。 */
const slices = (text, size) => { const out = []; for (let i = 0; i < text.length; i += size) { out.push(text.slice(i, i + size)); } return out; };

test('one chunk with several events returns them in order', () => {
  const parser = createParser();
  const text = encode('meta', { turn_id: 3, replayed: false }) + encode('token', { text: '第一段' }) + encode('token', { text: '第二段' }) + encode('ping', {});
  assert.deepEqual(parser.feed(text), [
    { event: 'meta', data: { turn_id: 3, replayed: false } },
    { event: 'token', data: { text: '第一段' } },
    { event: 'token', data: { text: '第二段' } },
    { event: 'ping', data: {} },
  ]);
});

test('an event split into one-character chunks (Chinese text included) comes out whole, exactly once', () => {
  const text = encode('token', { text: '她推開門，看見一盞燈在風裡搖晃。' }) + encode('done', { status: 'complete', coins: 170 });
  for (const size of [1, 2, 3, 5, 7]) {
    const parser = createParser();
    const events = feedAll(parser, slices(text, size));
    assert.deepEqual(events, [
      { event: 'token', data: { text: '她推開門，看見一盞燈在風裡搖晃。' } },
      { event: 'done', data: { status: 'complete', coins: 170 } },
    ], `size ${size}`);
  }
});

test('an event is only returned after its blank line arrives', () => {
  const parser = createParser();
  assert.deepEqual(parser.feed('event: token\ndata: {"text":"半'), []);
  assert.deepEqual(parser.feed('句"}\n'), []);                                  // 還差結尾的空行
  assert.deepEqual(parser.feed('\n'), [{ event: 'token', data: { text: '半句' } }]);
  assert.deepEqual(parser.feed(''), []);                                       // 已取走的事件不會再出現
});

test('the incomplete tail stays buffered while earlier complete events are returned', () => {
  const parser = createParser();
  const first = parser.feed(encode('meta', { turn_id: 1 }) + 'event: token\ndata: {"te');
  assert.deepEqual(first, [{ event: 'meta', data: { turn_id: 1 } }]);
  assert.deepEqual(parser.feed('xt":"後半"}\n\n'), [{ event: 'token', data: { text: '後半' } }]);
});

test('CRLF line endings work, including a CR and LF split across two chunks', () => {
  const crlf = 'event: token\r\ndata: {"text":"甲"}\r\n\r\nevent: done\r\ndata: {"status":"complete"}\r\n\r\n';
  assert.deepEqual(createParser().feed(crlf), [
    { event: 'token', data: { text: '甲' } },
    { event: 'done', data: { status: 'complete' } },
  ]);
  for (const size of [1, 2, 3, 4]) {
    assert.equal(feedAll(createParser(), slices(crlf, size)).length, 2, `size ${size}`);
  }
  const parser = createParser();
  assert.deepEqual(parser.feed('event: ping\ndata: {}\r\n\r'), []);           // 結尾的 \r 先保留，因為下一塊可能以 \n 開頭
  assert.deepEqual(parser.feed('\n'), [{ event: 'ping', data: {} }]);
  const lone = createParser();                                                 // 單獨的 \r 也是換行（SSE 標準）
  assert.deepEqual(lone.feed('event: ping\rdata: {}\r\r\n'), [{ event: 'ping', data: {} }]);   // 結尾的 \r\n 是一個換行，前面的 \r 是空行
  const held = createParser();                                                 // 結尾單獨的 \r 要等下一塊才知道它是不是 \r\n 的前半
  assert.deepEqual(held.feed('event: ping\rdata: {}\r\r'), []);
  assert.deepEqual(held.feed('event: done\rdata: {}\r\r\n'), [{ event: 'ping', data: {} }, { event: 'done', data: {} }]);
});

test('comment lines are ignored, whether alone, inside an event, or as a heartbeat block', () => {
  const parser = createParser();
  const events = parser.feed(': heartbeat\n\nevent: token\n: 這是註解\ndata: {"text":"字"}\n\n:keep-alive\n\n');
  assert.deepEqual(events, [{ event: 'token', data: { text: '字' } }]);
});

test('multi-line data is joined with a newline before JSON parsing', () => {
  const parser = createParser();
  const events = parser.feed('event: state\ndata: {"updated":true,\ndata: "location":"山門"}\n\n');
  assert.deepEqual(events, [{ event: 'state', data: { updated: true, location: '山門' } }]);
});

test('only one leading space after the colon is removed, and a field without a space still works', () => {
  assert.deepEqual(createParser().feed('event:token\ndata:{"text":" 前導空白保留"}\n\n'), [{ event: 'token', data: { text: ' 前導空白保留' } }]);
  assert.deepEqual(createParser().feed('event:  token\ndata: {}\n\n'), [{ event: ' token', data: {} }]);   // 第二個空格屬於名稱，呼叫端不認得就忽略
});

test('unknown event names are returned as they are; the caller decides what to do with them', () => {
  const parser = createParser();
  assert.deepEqual(parser.feed(encode('mystery', { x: 1 }) + encode('token', { text: '照常' })), [
    { event: 'mystery', data: { x: 1 } },
    { event: 'token', data: { text: '照常' } },
  ]);
});

test('an event with bad JSON is dropped and the next event is unaffected', () => {
  const parser = createParser();
  const events = parser.feed('event: token\ndata: {壞掉\n\n' + encode('token', { text: '好的' }));
  assert.deepEqual(events, [{ event: 'token', data: { text: '好的' } }]);
});

test('events that are not a JSON object, have no data, or have no event name are dropped', () => {
  const parser = createParser();
  const dropped = [
    'event: token\ndata: "只是字串"\n\n',
    'event: token\ndata: 42\n\n',
    'event: token\ndata: null\n\n',
    'event: token\ndata: [1,2]\n\n',
    'event: token\n\n',                                  // 沒有 data
    'data: {"text":"沒有事件名稱"}\n\n',
    'event: \ndata: {"text":"空的事件名稱"}\n\n',
    'id: 7\nretry: 1000\n\n',                           // 只有被忽略的欄位
  ];
  assert.deepEqual(parser.feed(dropped.join('') + encode('done', { status: 'complete' })), [{ event: 'done', data: { status: 'complete' } }]);
});

test('extra blank lines between events do not produce events', () => {
  const parser = createParser();
  assert.deepEqual(parser.feed('\n\n\n' + encode('ping', {}) + '\n\n\n\n'), [{ event: 'ping', data: {} }]);
});

test('a JSON text containing "\\n\\n" inside a string is safe: data is one line, the escape is not a blank line', () => {
  const parser = createParser();
  const events = parser.feed(encode('token', { text: '第一段\n\n第二段' }));
  assert.deepEqual(events, [{ event: 'token', data: { text: '第一段\n\n第二段' } }]);
});

test('feed never throws on odd input', () => {
  const parser = createParser();
  for (const junk of [undefined, null, 5, {}, [], true]) { assert.deepEqual(parser.feed(junk), [], String(junk)); }
  assert.deepEqual(parser.feed(encode('ping', {})), [{ event: 'ping', data: {} }]);
});

test('a "__proto__" key in the JSON stays plain data and does not pollute anything', () => {
  const parser = createParser();
  const [event] = parser.feed('event: state\ndata: {"__proto__":{"polluted":true},"updated":false}\n\n');
  assert.equal(event.data.updated, false);
  assert.equal({}.polluted, undefined);
});

test('two parsers do not share a buffer', () => {
  const a = createParser();
  const b = createParser();
  a.feed('event: token\ndata: {"text":"A');
  assert.deepEqual(b.feed(encode('ping', {})), [{ event: 'ping', data: {} }]);
  assert.deepEqual(a.feed('"}\n\n'), [{ event: 'token', data: { text: 'A' } }]);
});
