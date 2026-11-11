'use strict';
/* rpg-stream-client 規格的串流客戶端（turn-stream.js）：以假的 fetch（真的 Response 與 ReadableStream）和假計時器驅動，
 * 檢查每一種結果（done、error_event、interrupted、timeout_idle、timeout_total、aborted、rejected）、閒置與總逾時、ping 重設、
 * 切在多位元組字元中間的資料塊、請求標頭與本文，以及**每一條結果路徑都清除計時器並移除監聽**。純邏輯，不需要瀏覽器。 */
const baseTest = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

/* 每個測試 5 秒逾時：串流客戶端壞掉時常見的症狀是 Promise 永遠不結案，沒有逾時的話整個測試行程會卡住而不是失敗。 */
const test = (name, fn) => baseTest(name, { timeout: 5000 }, fn);

const { createTurnStream, STREAM_OUTCOMES } = require(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'turn-stream.js'));

const encoder = new TextEncoder();
const encode = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const settleMicrotasks = async () => { for (let i = 0; i < 5; i += 1) { await tick(); } };

const IDLE = 45000;
const TOTAL = 180000;
const URL = '/api/game/sessions/abc/turns/stream';

/* 假計時器：只在 advance() 時觸發，按時間順序執行；pending() 是尚未觸發也未清除的計時器數。 */
function fakeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    setTimeout(fn, ms) { const id = nextId; nextId += 1; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        let next = null;
        for (const [id, timer] of timers) { if (timer.at <= target && (next === null || timer.at < next.timer.at)) { next = { id, timer }; } }
        if (next === null) { break; }
        now = next.timer.at;
        timers.delete(next.id);
        next.timer.fn();
        await settleMicrotasks();
      }
      now = target;
      await settleMicrotasks();
    },
    pending: () => timers.size,
  };
}

/* 可控制的串流：push 送出位元組、close 正常結束、fail 讓讀取拋出例外。cancelled 記錄 reader.cancel() 是否被呼叫。 */
function controlledStream() {
  let controller;
  const state = { cancelled: false };
  const stream = new ReadableStream({ start(c) { controller = c; }, cancel() { state.cancelled = true; } });
  return {
    stream,
    state,
    // 串流已結束、出錯或被取消後再送資料不是測試要驗證的事：靜默忽略（就像網路上遲到的位元組）
    push: (text) => { try { controller.enqueue(typeof text === 'string' ? encoder.encode(text) : text); } catch (error) { /* 已結束 */ } },
    close: () => { try { controller.close(); } catch (error) { /* 已結束 */ } },
    fail: (error) => controller.error(error),
  };
}

/* 假 fetch：記錄每次呼叫；respond(call) 回傳 Promise<Response>。`honorSignal` 為真時，signal 中止會讓串流以 AbortError 出錯（像瀏覽器）。 */
function fakeFetch(respond, { honorSignal = true } = {}) {
  const calls = [];
  const fetch = (url, init) => {
    const call = { url, init, aborted: () => init.signal.aborted };
    calls.push(call);
    return respond(call);
  };
  fetch.calls = calls;
  fetch.honorSignal = honorSignal;
  return fetch;
}

function build({ respond, honorSignal = true, describeHttpFailure } = {}) {
  const clock = fakeClock();
  const failures = [];
  const fetch = fakeFetch(respond, { honorSignal });
  const streamer = createTurnStream({
    fetch,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    AbortController,
    TextDecoder,
    describeHttpFailure: describeHttpFailure || ((status, body) => { failures.push([status, body]); return { kind: 'stub', status, body }; }),
  });
  return { clock, fetch, streamer, failures };
}

/* 一個成功的回應：串流由測試控制；signal 中止時串流出錯。 */
function streamingResponse(stream, call, honorSignal = true) {
  if (honorSignal) {
    call.init.signal.addEventListener('abort', () => { try { stream.fail(new DOMException('aborted', 'AbortError')); } catch (error) { /* 已結束 */ } });
  }
  return Promise.resolve(new Response(stream.stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
}

function start({ streamer, options, stream, fetch }) {
  const events = [];
  const promise = streamer.run(Object.assign({ url: URL, body: { client_msg_id: 'c1', action: '推開門' }, csrf: 'tok', idleMs: IDLE, totalMs: TOTAL, onEvent: (event) => events.push(event) }, options));
  return { promise, events };
}

function ready(options = {}) {
  const stream = controlledStream();
  const ctx = build({ respond: (call) => streamingResponse(stream, call, options.honorSignal !== false), honorSignal: options.honorSignal });
  const run = start({ streamer: ctx.streamer, options: options.run, stream });
  return Object.assign({ stream }, ctx, run);
}

// ---- 正常結束 ---------------------------------------------------------------------------------------

test('a normal sequence ends with outcome done, delivers every event in order, and clears both timers', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('meta', { turn_id: 3, replayed: false }) + encode('token', { text: '甲' }));
  await settleMicrotasks();
  t.stream.push(encode('token', { text: '乙' }) + encode('state', { updated: true, location: '山門' }) + encode('done', { status: 'complete', coins: 170 }));
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.DONE);
  assert.deepEqual(result.done, { status: 'complete', coins: 170 });
  assert.equal(result.failure, null);
  assert.deepEqual(t.events.map((event) => event.event), ['meta', 'token', 'token', 'state', 'done']);
  assert.deepEqual(t.events[1].data, { text: '甲' });
  assert.equal(t.clock.pending(), 0);
  assert.equal(t.stream.state.cancelled, true);         // done 就結案並釋放讀取，不等連線關閉
});

test('done settles immediately even if the connection stays open, and events after done are ignored', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('token', { text: '字' }) + encode('done', { status: 'complete', coins: 1 }) + encode('token', { text: '多餘' }));
  const result = await t.promise;
  assert.equal(result.outcome, 'done');
  assert.deepEqual(t.events.map((event) => event.event), ['token', 'done']);
  assert.equal(t.clock.pending(), 0);
});

test('blocked and ended turns are also "done": the status is the caller\'s business', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('transition', { text: '鏡頭一轉', will_retry: false }) + encode('done', { status: 'blocked', turn_number: null, coins: 140 }));
  const result = await t.promise;
  assert.equal(result.outcome, 'done');
  assert.equal(result.done.status, 'blocked');
});

// ---- 沒有 done 就結束 -------------------------------------------------------------------------------

test('the connection ending before done is interrupted (the HTTP status and the reader "done" flag are not completion)', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('meta', { turn_id: 1 }) + encode('token', { text: '一半' }));
  await settleMicrotasks();
  t.stream.close();
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.INTERRUPTED);
  assert.equal(result.done, null);
  assert.equal(result.failure, null);
  assert.deepEqual(t.events.map((event) => event.event), ['meta', 'token']);   // 已送達的事件照樣交給呼叫端，由它丟棄未完成的氣泡
  assert.equal(t.clock.pending(), 0);
});

test('a read exception before done is interrupted', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('token', { text: '字' }));
  await settleMicrotasks();
  t.stream.fail(new TypeError('network error'));
  const result = await t.promise;
  assert.equal(result.outcome, 'interrupted');
  assert.equal(t.clock.pending(), 0);
});

test('a network failure before any response is interrupted (the server may or may not have seen the request)', async () => {
  const ctx = build({ respond: () => Promise.reject(new TypeError('Failed to fetch')) });
  const { promise } = start({ streamer: ctx.streamer });
  const result = await promise;
  assert.equal(result.outcome, 'interrupted');
  assert.equal(ctx.clock.pending(), 0);
});

test('a synchronous exception from fetch still resolves (run never rejects)', async () => {
  const ctx = build({ respond: () => { throw new Error('boom'); } });
  const { promise } = start({ streamer: ctx.streamer });
  const result = await promise;
  assert.equal(result.outcome, 'interrupted');
  assert.equal(ctx.clock.pending(), 0);
});

test('an error event followed by the end of the connection is error_event with the code and a bounded message', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('token', { text: '字' }) + encode('error', { code: 'generation_failed', message: '故事暫時無法生成。'.padEnd(500, '。') }));
  await settleMicrotasks();
  t.stream.close();
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.ERROR_EVENT);
  assert.equal(result.failure.kind, 'error_event');
  assert.equal(result.failure.code, 'generation_failed');
  assert.ok(result.failure.message.length <= 200);
  assert.deepEqual(t.events.map((event) => event.event), ['token', 'error']);
  assert.equal(t.clock.pending(), 0);
});

test('an error event followed by a read exception is still error_event (the server already told us)', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('error', { code: 'internal_error', message: '發生了預期之外的問題。' }));
  await settleMicrotasks();
  t.stream.fail(new TypeError('reset'));
  const result = await t.promise;
  assert.equal(result.outcome, 'error_event');
  assert.equal(result.failure.code, 'internal_error');
});

test('a non-string code or message in the error event becomes empty strings', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('error', { code: 5, message: { x: 1 } }));
  await settleMicrotasks();
  t.stream.close();
  const result = await t.promise;
  assert.deepEqual(result.failure, { kind: 'error_event', code: '', message: '' });
});

// ---- 逾時 -------------------------------------------------------------------------------------------

test('45 seconds without any byte is timeout_idle: the request is aborted and the timers are cleared', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push(encode('meta', { turn_id: 1 }));
  await settleMicrotasks();
  await t.clock.advance(IDLE - 1);
  assert.equal(t.fetch.calls[0].aborted(), false);
  await t.clock.advance(1);
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.TIMEOUT_IDLE);
  assert.equal(t.fetch.calls[0].aborted(), true);
  assert.equal(t.clock.pending(), 0);
});

test('ping chunks reset the idle timer: 40 seconds of model silence with a ping every 15 seconds never times out', async () => {
  const t = ready();
  await settleMicrotasks();
  for (let i = 0; i < 6; i += 1) {                      // 90 秒的靜默，每 15 秒一個 ping
    await t.clock.advance(15000);
    t.stream.push(encode('ping', {}));
    await settleMicrotasks();
  }
  assert.equal(t.fetch.calls[0].aborted(), false);
  t.stream.push(encode('token', { text: '終於' }) + encode('done', { status: 'complete', coins: 1 }));
  const result = await t.promise;
  assert.equal(result.outcome, 'done');
  assert.equal(t.clock.pending(), 0);
});

test('a chunk with only a ping still counts as bytes; a comment-only chunk too', async () => {
  const t = ready();
  await settleMicrotasks();
  await t.clock.advance(IDLE - 1000);
  t.stream.push(': keep-alive\n\n');
  await settleMicrotasks();
  await t.clock.advance(IDLE - 1000);                    // 距離上一塊只有 44 秒
  assert.equal(t.fetch.calls[0].aborted(), false);
  await t.clock.advance(1000);
  assert.equal((await t.promise).outcome, 'timeout_idle');
});

test('180 seconds is timeout_total even when bytes keep arriving (the total timer is never reset)', async () => {
  const t = ready();
  await settleMicrotasks();
  for (let i = 0; i < 11; i += 1) {                      // 165 秒，每 15 秒一個 ping
    await t.clock.advance(15000);
    t.stream.push(encode('ping', {}));
    await settleMicrotasks();
  }
  assert.equal(t.fetch.calls[0].aborted(), false);
  await t.clock.advance(15000);                          // 第 180 秒
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.TIMEOUT_TOTAL);
  assert.equal(t.fetch.calls[0].aborted(), true);
  assert.equal(t.clock.pending(), 0);
});

test('the timeouts are whatever the caller passes in (the module has no numbers of its own)', async () => {
  const t = ready({ run: { idleMs: 1000, totalMs: 5000 } });
  await settleMicrotasks();
  await t.clock.advance(1000);
  assert.equal((await t.promise).outcome, 'timeout_idle');
});

test('receiving the response headers resets the idle timer too (a slow start is not a silent connection)', async () => {
  const stream = controlledStream();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const ctx = build({ respond: (call) => gate.then(() => streamingResponse(stream, call)) });
  const { promise } = start({ streamer: ctx.streamer });
  await ctx.clock.advance(IDLE - 5000);                  // 40 秒才拿到標頭
  release();
  await settleMicrotasks();
  await ctx.clock.advance(IDLE - 1);                     // 標頭之後還沒滿 45 秒
  assert.equal(ctx.fetch.calls[0].aborted(), false);
  await ctx.clock.advance(1);
  assert.equal((await promise).outcome, 'timeout_idle');
});

test('a timeout settles even if fetch ignores the abort signal (settling does not depend on the browser reporting abort)', async () => {
  const t = ready({ honorSignal: false });
  await settleMicrotasks();
  await t.clock.advance(IDLE);
  const result = await t.promise;
  assert.equal(result.outcome, 'timeout_idle');
  assert.equal(t.clock.pending(), 0);
});

test('a hung connection before the response also times out', async () => {
  const ctx = build({ respond: () => new Promise(() => {}) });
  const { promise } = start({ streamer: ctx.streamer });
  await ctx.clock.advance(IDLE);
  assert.equal((await promise).outcome, 'timeout_idle');
  assert.equal(ctx.fetch.calls[0].aborted(), true);
  assert.equal(ctx.clock.pending(), 0);
});

// ---- 外部取消 ---------------------------------------------------------------------------------------

/* 一個會記錄監聽的 signal，用來證明結案時監聽被移除。 */
function trackedSignal() {
  const controller = new AbortController();
  const listeners = new Set();
  return {
    controller,
    listeners,
    signal: {
      get aborted() { return controller.signal.aborted; },
      addEventListener(type, handler) { listeners.add(handler); controller.signal.addEventListener(type, handler); },
      removeEventListener(type, handler) { listeners.delete(handler); controller.signal.removeEventListener(type, handler); },
    },
  };
}

test('an external abort (player cancel) is aborted: the request is aborted, the timers cleared, the listener removed', async () => {
  const tracked = trackedSignal();
  const t = ready({ run: { signal: tracked.signal } });
  await settleMicrotasks();
  t.stream.push(encode('token', { text: '字' }));
  await settleMicrotasks();
  assert.equal(tracked.listeners.size, 1);
  tracked.controller.abort();
  const result = await t.promise;
  assert.equal(result.outcome, STREAM_OUTCOMES.ABORTED);
  assert.equal(t.fetch.calls[0].aborted(), true);
  assert.equal(t.clock.pending(), 0);
  assert.equal(tracked.listeners.size, 0);
});

test('a pagehide-style handler that aborts the shared controller ends the stream as aborted', async () => {
  const page = new AbortController();
  const handlers = [];
  const pageWindow = { addEventListener: (type, handler) => handlers.push([type, handler]) };
  pageWindow.addEventListener('pagehide', () => page.abort());
  const t = ready({ run: { signal: page.signal } });
  await settleMicrotasks();
  handlers.filter(([type]) => type === 'pagehide').forEach(([, handler]) => handler());
  assert.equal((await t.promise).outcome, 'aborted');
  assert.equal(t.clock.pending(), 0);
});

test('a signal that is already aborted never calls fetch and starts no timers', async () => {
  const page = new AbortController();
  page.abort();
  const ctx = build({ respond: () => { throw new Error('should not be called'); } });
  const { promise } = start({ streamer: ctx.streamer, options: { signal: page.signal } });
  assert.equal((await promise).outcome, 'aborted');
  assert.equal(ctx.fetch.calls.length, 0);
  assert.equal(ctx.clock.pending(), 0);
});

test('cancelling after done does nothing: the result stays done', async () => {
  const tracked = trackedSignal();
  const t = ready({ run: { signal: tracked.signal } });
  await settleMicrotasks();
  t.stream.push(encode('done', { status: 'complete', coins: 1 }));
  const result = await t.promise;
  tracked.controller.abort();
  await settleMicrotasks();
  assert.equal(result.outcome, 'done');
  assert.equal(tracked.listeners.size, 0);
});

// ---- 開始前的 JSON 錯誤 -----------------------------------------------------------------------------

const jsonResponse = (status, body) => Promise.resolve(new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));

for (const [status, code] of [[409, 'insufficient_coins'], [401, 'unauthorized'], [503, 'service_unavailable'], [409, 'turn_in_progress'], [403, 'age_confirmation_required'], [404, 'not_found'], [400, 'validation_error'], [429, 'too_many_requests']]) {
  test(`a ${status} ${code} before the stream starts is rejected, with the body handed to describeHttpFailure, and nothing is read`, async () => {
    const body = { error: { code, message: '固定訊息' }, balance: 10, needed: 30 };
    const ctx = build({ respond: () => jsonResponse(status, body) });
    const { promise, events } = start({ streamer: ctx.streamer });
    const result = await promise;
    assert.equal(result.outcome, STREAM_OUTCOMES.REJECTED);
    assert.equal(result.done, null);
    assert.deepEqual(result.failure, { kind: 'stub', status, body });
    assert.deepEqual(ctx.failures, [[status, body]]);
    assert.deepEqual(events, []);
    assert.equal(ctx.clock.pending(), 0);
  });
}

test('an error response whose body is not JSON is still rejected, with a null body', async () => {
  const ctx = build({ respond: () => jsonResponse(502, '<html>Bad Gateway</html>') });
  const { promise } = start({ streamer: ctx.streamer });
  const result = await promise;
  assert.equal(result.outcome, 'rejected');
  assert.deepEqual(ctx.failures, [[502, null]]);
});

test('without a describeHttpFailure the rejected result still has a minimal failure and run still resolves', async () => {
  const clock = fakeClock();
  const streamer = createTurnStream({ fetch: () => jsonResponse(500, {}), setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, AbortController, TextDecoder });
  const result = await streamer.run({ url: URL, body: {}, csrf: 't', idleMs: IDLE, totalMs: TOTAL });
  assert.equal(result.outcome, 'rejected');
  assert.equal(result.failure.status, 500);
});

test('a describeHttpFailure that throws does not make run reject', async () => {
  const ctx = build({ respond: () => jsonResponse(409, {}), describeHttpFailure: () => { throw new Error('x'); } });
  const { promise } = start({ streamer: ctx.streamer });
  const result = await promise;
  assert.equal(result.outcome, 'rejected');
  assert.equal(result.failure.status, 409);
});

// ---- 位元組邊界 -------------------------------------------------------------------------------------

test('a chunk boundary inside a multi-byte character does not corrupt the text (also one byte at a time)', async () => {
  const text = '她推開那扇門，看見一盞燈在風裡搖晃。🙂';
  const bytes = encoder.encode(encode('token', { text }) + encode('done', { status: 'complete', coins: 1 }));
  for (const size of [1, 2, 4, 7]) {
    const t = ready();
    await settleMicrotasks();
    for (let i = 0; i < bytes.length; i += size) {
      t.stream.push(bytes.slice(i, i + size));
      await settleMicrotasks();
    }
    const result = await t.promise;
    assert.equal(result.outcome, 'done', `size ${size}`);
    assert.equal(t.events[0].data.text, text, `size ${size}`);
  }
});

test('unknown event names are not delivered, bad events are skipped, and later events still arrive', async () => {
  const t = ready();
  await settleMicrotasks();
  t.stream.push('event: mystery\ndata: {"x":1}\n\nevent: token\ndata: {壞\n\n' + encode('constructor', {}) + encode('token', { text: '好' }) + encode('done', { status: 'complete', coins: 1 }));
  const result = await t.promise;
  assert.equal(result.outcome, 'done');
  assert.deepEqual(t.events.map((event) => event.event), ['token', 'done']);
});

test('an onEvent that throws does not break the stream', async () => {
  const stream = controlledStream();
  const ctx = build({ respond: (call) => streamingResponse(stream, call) });
  const seen = [];
  const promise = ctx.streamer.run({ url: URL, body: {}, csrf: 't', idleMs: IDLE, totalMs: TOTAL, onEvent: (event) => { seen.push(event.event); throw new Error('ui bug'); } });
  await settleMicrotasks();
  stream.push(encode('token', { text: '甲' }) + encode('token', { text: '乙' }) + encode('done', { status: 'complete', coins: 1 }));
  assert.equal((await promise).outcome, 'done');
  assert.deepEqual(seen, ['token', 'token', 'done']);
});

test('no onEvent at all is fine', async () => {
  const stream = controlledStream();
  const ctx = build({ respond: (call) => streamingResponse(stream, call) });
  const promise = ctx.streamer.run({ url: URL, body: {}, csrf: 't', idleMs: IDLE, totalMs: TOTAL });
  await settleMicrotasks();
  stream.push(encode('done', { status: 'complete', coins: 5 }));
  assert.deepEqual((await promise).done, { status: 'complete', coins: 5 });
});

// ---- 請求 -------------------------------------------------------------------------------------------

test('the request is a same-origin POST with the CSRF header and the JSON body', async () => {
  const t = ready();
  await settleMicrotasks();
  const [call] = t.fetch.calls;
  assert.equal(call.url, URL);
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.credentials, 'same-origin');
  assert.equal(call.init.headers['X-CSRF-Token'], 'tok');
  assert.equal(call.init.headers['Content-Type'], 'application/json');
  assert.equal(call.init.headers.Accept, 'text/event-stream');
  assert.deepEqual(JSON.parse(call.init.body), { client_msg_id: 'c1', action: '推開門' });
  assert.ok(call.init.signal instanceof AbortSignal);
  t.stream.push(encode('done', { status: 'complete', coins: 1 }));
  await t.promise;
});

test('a request without a body (the opening stream) sends no body and no content type', async () => {
  const stream = controlledStream();
  const ctx = build({ respond: (call) => streamingResponse(stream, call) });
  const promise = ctx.streamer.run({ url: '/api/game/sessions/abc/opening/stream', csrf: 'tok', idleMs: IDLE, totalMs: TOTAL });
  await settleMicrotasks();
  const [call] = ctx.fetch.calls;
  assert.equal(call.init.body, undefined);
  assert.equal('Content-Type' in call.init.headers, false);
  assert.equal(call.init.headers['X-CSRF-Token'], 'tok');
  stream.push(encode('done', { status: 'complete', coins: 1 }));
  await promise;
});

test('a missing CSRF token is sent as an empty header value (the server rejects it; the module does not invent one)', async () => {
  const stream = controlledStream();
  const ctx = build({ respond: (call) => streamingResponse(stream, call) });
  const promise = ctx.streamer.run({ url: URL, body: {}, idleMs: IDLE, totalMs: TOTAL });
  await settleMicrotasks();
  assert.equal(ctx.fetch.calls[0].init.headers['X-CSRF-Token'], '');
  stream.push(encode('done', {}));
  await promise;
});

// ---- 設定錯誤（程式錯誤）----------------------------------------------------------------------------

for (const [label, options] of [
  ['no idleMs', { idleMs: undefined }],
  ['idleMs of zero', { idleMs: 0 }],
  ['negative totalMs', { totalMs: -1 }],
  ['a string totalMs', { totalMs: '180000' }],
  ['NaN idleMs', { idleMs: NaN }],
  ['no url', { url: '' }],
]) {
  test(`${label} is rejected as a config error without sending the request and without timers`, async () => {
    const ctx = build({ respond: () => { throw new Error('should not be called'); } });
    const { promise } = start({ streamer: ctx.streamer, options });
    const result = await promise;
    assert.equal(result.outcome, 'rejected');
    assert.equal(result.failure.kind, 'config');
    assert.equal(ctx.fetch.calls.length, 0);
    assert.equal(ctx.clock.pending(), 0);
  });
}

test('a body that cannot be serialised is a config error and the request is not sent', async () => {
  const circular = {};
  circular.self = circular;
  const ctx = build({ respond: () => { throw new Error('should not be called'); } });
  const { promise } = start({ streamer: ctx.streamer, options: { body: circular } });
  const result = await promise;
  assert.equal(result.outcome, 'rejected');
  assert.equal(result.failure.kind, 'config');
  assert.equal(ctx.fetch.calls.length, 0);
  assert.equal(ctx.clock.pending(), 0);
});

test('missing dependencies are a config error, not an exception', async () => {
  for (const missing of ['fetch', 'setTimeout', 'clearTimeout', 'AbortController', 'TextDecoder']) {
    const deps = { fetch: () => {}, setTimeout, clearTimeout, AbortController, TextDecoder };
    delete deps[missing];
    const result = await createTurnStream(deps).run({ url: URL, idleMs: IDLE, totalMs: TOTAL });
    assert.equal(result.outcome, 'rejected', missing);
    assert.equal(result.failure.kind, 'config', missing);
  }
  assert.equal((await createTurnStream().run({ url: URL, idleMs: IDLE, totalMs: TOTAL })).outcome, 'rejected');
  assert.equal((await createTurnStream({ fetch() {}, setTimeout, clearTimeout, AbortController, TextDecoder }).run()).outcome, 'rejected');
});

test('a response without a readable body is interrupted', async () => {
  const ctx = build({ respond: () => Promise.resolve({ ok: true, status: 200, body: null }) });
  const { promise } = start({ streamer: ctx.streamer });
  assert.equal((await promise).outcome, 'interrupted');
  assert.equal(ctx.clock.pending(), 0);
});

// ---- 計時器：每條路徑都清除 -------------------------------------------------------------------------

test('every outcome leaves no timer behind and no abort listener behind', async () => {
  const scenarios = {
    done: async (t) => { t.stream.push(encode('done', {})); },
    interrupted: async (t) => { t.stream.close(); },
    error_event: async (t) => { t.stream.push(encode('error', { code: 'x', message: 'y' })); await settleMicrotasks(); t.stream.close(); },
    timeout_idle: async (t) => { await t.clock.advance(IDLE); },
    timeout_total: async (t) => { for (let i = 0; i < 12; i += 1) { await t.clock.advance(15000); t.stream.push(encode('ping', {})); await settleMicrotasks(); } },
    aborted: async (t) => { t.tracked.controller.abort(); },
  };
  for (const [expected, act] of Object.entries(scenarios)) {
    const tracked = trackedSignal();
    const t = ready({ run: { signal: tracked.signal } });
    t.tracked = tracked;
    await settleMicrotasks();
    await act(t);
    const result = await t.promise;
    assert.equal(result.outcome, expected);
    assert.equal(t.clock.pending(), 0, `${expected}: timers`);
    assert.equal(tracked.listeners.size, 0, `${expected}: listeners`);
  }
});
