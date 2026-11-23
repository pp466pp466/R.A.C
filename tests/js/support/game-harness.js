'use strict';
/* 遊戲主畫面的測試環境（game-session.test.js 與 game.test.js 共用，不是測試檔）。
 * 用的都是**真的**模組：真的 Vue 響應式、真的 game-store（記憶體後端）、真的 game-sync、真的 turn-stream（SSE 解析與逾時）、真的 game-state／game-labels；
 * 只有「伺服器」是替身——一個記錄呼叫的假後端：
 *   - `server.session`／`server.turns`：快照與回合清單（`api.snapshot`、`api.turns`；回合清單依 `since_seq` 與 `pageSize` 分頁）；
 *   - `fetch`：回合與第 0 幕的串流端點。依序消耗 `server.streamPlans`／`server.openingPlans`；每個計畫 `{ http }`（開始前的 JSON 錯誤）或 `{ events, cut, hold, commit }`：
 *     `events` 是 [事件名稱, 資料] 的陣列，`cut` 為真代表送完事件就斷線（沒有 done），`hold` 為真代表連線一直開著直到被中止，`commit` 是「伺服器端完成這一輪」時要寫入的回合；
 *   - 打字機是同步的替身（`typewriters` 記錄每一個；`autoFinish: false` 時 finish 後要測試呼叫 `complete()` 才算打完）；
 *   - 計時器是假的（`timers.fire()` 執行到期的回呼），用來測第 0 幕等待的 5 秒輪詢；
 *   - `console`、`localStorage`、`sessionStorage`、`indexedDB` 在 `trapGlobals()` 之間是會記錄的陷阱，流程結束後斷言它們沒有被用到。 */
const path = require('node:path');
const { loadVue, plain } = require('./vue-runtime.js');

const GAME = path.join(__dirname, '..', '..', '..', 'app', 'static', 'js', 'game');
const Labels = require(path.join(GAME, 'game-labels.js'));
const State = require(path.join(GAME, 'game-state.js'));
const Store = require(path.join(GAME, 'game-store.js'));
const Sync = require(path.join(GAME, 'game-sync.js'));
const { createTurnStream } = require(path.join(GAME, 'turn-stream.js'));
const { createWallet } = require(path.join(GAME, 'game-wallet.js'));
const { createSession } = require(path.join(GAME, 'game-session.js'));
const { createRevision } = require(path.join(GAME, 'game-revision.js'));

const Vue = loadVue();
const SESSION_ID = '99999999-9999-4999-8999-999999999999';
const encoder = new TextEncoder();

const WORLD = { elapsed_months: 0, location: '青雲山門', summary: '初入山門', goals: ['通過入門測試'], places: ['青雲山門'], protagonist_age: 16 };

function initialState(overrides) {
  return Object.assign({
    session: { id: SESSION_ID, title: '青雲問道', content_tier: 'safe', ended: false, player_name: '林初' },
    user: { public_id: 'user-public-1', member_tier: 'free' },
    gate: null,
    content_mode: { effective: 'standard', is_adult: true },
    stream: { idle_timeout_ms: 45000, total_timeout_ms: 180000 },
    limits: { action_max: 1000 },
    revision_limits: { reason_min: 5, reason_max: 50, categories: ['setting_mismatch', 'repetitive_or_dull', 'typo_or_quality', 'blocked_transition', 'other'] },
  }, overrides);
}

function failureInfo(status, code, extra) {
  return Object.assign({ kind: 'server', status, code, message: '', fields: {}, categories: [] }, extra);
}

function turnView(seq, clientMsgId, action, story, extra) {
  return Object.assign({ seq, client_msg_id: clientMsgId, turn_number: seq, status: 'complete', action, story }, extra);
}

function event(name, data) { return [name, data]; }
const sse = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;

/* 一個正常的回合：meta、token…、state、done。 */
function normalTurn({ seq, clientMsgId, story, coins = 170, tokens, state, doneExtra } = {}) {
  const pieces = tokens || [story];
  return {
    events: [
      event('meta', { turn_id: seq, attempt: 1, replayed: false }),
      ...pieces.map((text) => event('token', { text })),
      event('state', state || { updated: true, elapsed_months: 1, location: '藏經閣', summary: '翻閱典籍', goals: ['找出失落的功法'] }),
      event('done', Object.assign({ status: 'complete', turn_number: seq, coins }, doneExtra)),
    ],
    commit: { view: turnView(seq, clientMsgId, undefined, story) },
    apply: (server) => {
      const world = state || { updated: true, elapsed_months: 1, location: '藏經閣', summary: '翻閱典籍', goals: ['找出失落的功法'] };
      if (world.updated) {
        Object.assign(server.session.world, { elapsed_months: world.elapsed_months, location: world.location, summary: world.summary, goals: world.goals || [] });
      }
      server.session.completed_turns += 1;
    },
  };
}

function createHarness(options = {}) {
  const calls = { snapshot: [], turns: [], streams: [], openings: [], fetch: [], wallet: [], checkin: [], suggest: [] };
  const logs = [];
  const toasts = [];
  const logins = [];
  const typewriters = [];
  const readerTypewriters = [];                                                                                         // 劇情卡（閱讀器）的打字機：與每個氣泡一個的 `typewriters` 分開記錄
  const noted = [];
  const server = {
    session: Object.assign({
      id: SESSION_ID, title: '青雲問道', content_tier: 'safe', status: 'active', ended: false, protagonist: { name: '林初' }, version: 1, completed_turns: 0,
      opening: { state: 'ready', text: '第零幕：山門前的石階。' }, world: Object.assign({}, WORLD),
      notebook: { version: 0, length: 0, capacity: 2000, reserve: 500, full: false },
    }, options.session),
    turns: (options.turns || []).map((turn) => Object.assign({}, turn)),
    pageSize: options.pageSize || 200,
    snapshotFailure: null, turnsFailure: null, walletFailure: null,
    streamPlans: [], openingPlans: [], suggestPlans: [], suggestGate: null,
    wallet: options.wallet || { total: 230 },
  };
  const state = initialState(options.state);

  const api = {
    snapshot: (id, opts) => {
      calls.snapshot.push({ id, opts });
      if (server.snapshotFailure) { return Promise.reject(server.snapshotFailure); }
      return Promise.resolve({ session: JSON.parse(JSON.stringify(server.session)) });
    },
    turns: async (id, since, opts) => {
      calls.turns.push({ id, since, opts });
      if (server.turnsGate) { await server.turnsGate; }
      if (server.turnsFailure) { throw server.turnsFailure; }
      const rest = server.turns.filter((turn) => turn.seq > since).sort((a, b) => a.seq - b.seq);
      return { turns: JSON.parse(JSON.stringify(rest.slice(0, server.pageSize))), has_more: rest.length > server.pageSize };
    },
    wallet: (opts) => {
      calls.wallet.push(opts);
      if (server.walletFailure) { return Promise.reject(server.walletFailure); }
      const exempt = server.wallet.exempt === true;                                                                         // exempt: true 代表管理員（回 coin_exempt: true、每輪幣價 0）；沒有設定時不帶這個欄位（舊版回應）
      const cost = exempt ? 0 : (server.wallet.cost === undefined ? 30 : server.wallet.cost);                               // cost: null 代表錢包沒有給每輪幣價
      const flag = server.wallet.exempt === undefined ? {} : { coin_exempt: exempt };
      return Promise.resolve({ ok: true, wallet: Object.assign({ free_balance: server.wallet.total, paid_balance: 0, total: server.wallet.total, free_cap: 1000, turn_cost: cost === null ? {} : { standard: cost }, checkin: { claimable: true, grant: 200, would_gain: 200, would_lose: 0, next_reset_at: '2026-11-12T16:00:00+00:00' } }, flag) });
    },
    checkin: (confirm, opts) => { calls.checkin.push({ confirm, opts }); return Promise.resolve({ ok: true, wallet: { total: server.wallet.total + 200, turn_cost: { standard: 30 }, checkin: { claimable: false } } }); },
    /* 行動建議端點：依序消耗 `server.suggestPlans`（`{ suggestions, reused }` 或 `{ failure }`；`apply(server)` 在回應前改動伺服器的狀態）；`server.suggestGate` 是讓測試卡住請求的 Promise。
       被中止（`opts.signal`）時比照 axios 以 canceled 拒絕。 */
    suggestActions: async (id, opts) => {
      calls.suggest.push({ id, opts });
      if (server.suggestGate) {
        await new Promise((resolve, reject) => {
          const signal = opts && opts.signal;
          if (signal) { signal.addEventListener('abort', () => reject(Object.assign({ kind: 'canceled', status: 0, code: '', message: '', fields: {} })), { once: true }); }
          server.suggestGate.then(resolve, reject);
        });
      }
      const plan = server.suggestPlans.shift();
      if (!plan) { throw new Error('沒有為建議準備計畫'); }
      if (plan.failure) { throw plan.failure; }
      if (plan.apply) { plan.apply(server); }
      return { ok: true, suggestions: plan.suggestions, reused: plan.reused === true };
    },
  };

  async function fetch(url, init) {
    calls.fetch.push({ url, init });
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    const isOpening = /\/opening\/stream$/.test(url);
    (isOpening ? calls.openings : calls.streams).push({ url, body, headers: init && init.headers, credentials: init && init.credentials, signal: init && init.signal });
    const plan = (isOpening ? server.openingPlans : server.streamPlans).shift();
    if (!plan) { throw new Error('沒有為這個串流準備計畫：' + url); }
    if (plan.http) {
      return new Response(JSON.stringify(plan.http.body || { ok: false, error: { code: plan.http.code, message: plan.http.message || '' } }), {
        status: plan.http.status, headers: { 'Content-Type': 'application/json' },
      });
    }
    if (plan.networkError) { throw new TypeError('network'); }
    const signal = init && init.signal;
    const body2 = new ReadableStream({
      async start(controller) {
        if (plan.commit) { server.turns.push(Object.assign({}, plan.commit.view, { action: plan.commit.view.action !== undefined ? plan.commit.view.action : body && body.action })); }
        if (plan.apply) { plan.apply(server); }
        if (signal) {
          const onAbort = () => { try { controller.error(new DOMException('aborted', 'AbortError')); } catch (ignored) { /* 已結束 */ } };
          if (signal.aborted) { onAbort(); return; }
          signal.addEventListener('abort', onAbort, { once: true });
        }
        try {
          for (const item of plan.events || []) {
            if (typeof item === 'function') { await item(); await new Promise((resolve) => setImmediate(resolve)); continue; }   // 事件之間的鉤子：讓測試在串流中途觀察或操作
            controller.enqueue(encoder.encode(sse(item[0], item[1])));
            await new Promise((resolve) => setImmediate(resolve));                                                           // 一次一個事件，讓畫面有機會在中途更新
          }
          if (!plan.hold) { controller.close(); }
        } catch (ignored) { /* 串流已被中止 */ }
      },
    });
    return new Response(body2, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }

  class FakeTypewriter {
    constructor(settings) {
      this.settings = settings;
      this.raw = '';
      this.text = '';
      this.finished = false;
      this.completed = false;
      this.cancelled = false;
      typewriters.push(this);
    }
    push(value) { if (this.cancelled) { return; } this.raw += value; this.text = this.raw; this.settings.onUpdate(this.text); }
    finish() {
      if (this.cancelled || this.finished) { return; }
      this.finished = true;
      if (options.autoFinish !== false) { this.complete(); }
    }
    complete() { if (this.cancelled || this.completed) { return; } this.completed = true; this.settings.onDone(); }
    flush() { this.finish(); this.complete(); }
    cancel() { this.cancelled = true; }
  }

  /* 劇情卡的打字機替身（story-reader.js 用 `play`、`flush`、`cancel`）：預設 `play` 立刻打完（整段直接顯示）；`readerAutoFinish: false` 時要測試呼叫 `complete()`，
     並可用 `progress(文字)` 模擬打到一半。 */
  class FakeReaderTypewriter {
    constructor(handlers) {
      this.handlers = handlers;
      this.played = '';
      this.text = '';
      this.completed = false;
      this.cancelled = false;
      readerTypewriters.push(this);
    }
    play(value) { this.played = value; if (options.readerAutoFinish !== false) { this.complete(); } }
    progress(value) { if (!this.cancelled && !this.completed) { this.text = value; this.handlers.onUpdate(value); } }
    complete() { if (this.cancelled || this.completed) { return; } this.completed = true; this.text = this.played; this.handlers.onUpdate(this.played); this.handlers.onDone(); }
    flush() { this.complete(); }
    cancel() { this.cancelled = true; }
  }

  const timerList = [];
  let timerId = 0;
  const timers = {
    set: (callback, ms) => { timerId += 1; timerList.push({ id: timerId, callback, ms }); return timerId; },
    clear: (id) => { const index = timerList.findIndex((timer) => timer.id === id); if (index >= 0) { timerList.splice(index, 1); } },
    pending: () => timerList.map((timer) => timer.ms),
    fire: () => { const due = timerList.splice(0, timerList.length); due.forEach((timer) => timer.callback()); return due.length; },
  };

  let idCounter = 0;
  let online = options.online !== false;
  let modalOpen = false;
  let reducedMotion = false;
  let store = null;
  let sync = null;
  let wallet = null;
  let session = null;
  let revision = null;

  async function build() {
    store = Store.createStore(Store.createMemoryBackend());
    if (options.seed) { await options.seed(store); }
    sync = Sync.createSync({ api, store });
    wallet = createWallet({
      Vue: { ref: Vue.ref, computed: Vue.computed, watch: Vue.watch }, api, toast: (message) => toasts.push(message),
      goLogin: () => logins.push('wallet'), noteFailure: (info) => noted.push(info),
    });
    const stream = createTurnStream({
      fetch, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, AbortController: globalThis.AbortController,
      TextDecoder: globalThis.TextDecoder, describeHttpFailure: State.describeHttpFailure,
    });
    session = createSession({
      Vue: { ref: Vue.ref, computed: Vue.computed, reactive: Vue.reactive }, state, api, store, sync, stream, wallet,
      TypewriterBuffer: FakeTypewriter, createTypewriter: (handlers) => new FakeReaderTypewriter(handlers), reducedMotion: () => reducedMotion,
      isModalOpen: () => modalOpen, toast: (message) => toasts.push(message), goLogin: () => logins.push('session'),
      newId: () => { idCounter += 1; return 'msg-' + idCounter; }, now: () => Date.UTC(2026, 10, 11, 12, 0, 0), csrf: () => 'CSRF-TOKEN',
      isOnline: () => online, setTimeout: timers.set, clearTimeout: timers.clear, AbortController: globalThis.AbortController,
      restrictionOf: (info) => (info && info.status === 403 && info.code === 'account_locked' ? 'locked' : null),
    });
    revision = createRevision({
      Vue: { ref: Vue.ref, computed: Vue.computed }, state, host: session.revisionHost, wallet, stream, store, TypewriterBuffer: FakeTypewriter,
      toast: (message) => toasts.push(message), goLogin: () => logins.push('revision'), csrf: () => 'CSRF-TOKEN', now: () => Date.UTC(2026, 10, 12, 12, 0, 0),
      AbortController: globalThis.AbortController,
    });
    return harness;
  }

  /* 等到條件成立（讓 Promise、串流與 Vue 的排程跑完）；逾時就丟出，不會無限等。 */
  async function until(predicate, label = '條件') {
    for (let index = 0; index < 400; index += 1) {
      if (predicate()) { return; }
      await new Promise((resolve) => setImmediate(resolve));
    }
    throw new Error('等不到：' + label);
  }
  const idle = async (turns = 12) => { for (let index = 0; index < turns; index += 1) { await new Promise((resolve) => setImmediate(resolve)); } };

  const harness = {
    build, calls, logs, toasts, logins, typewriters, readerTypewriters, noted, server, state, timers, until, idle,
    get store() { return store; }, get sync() { return sync; }, get wallet() { return wallet; }, get session() { return session; }, get revision() { return revision; },
    setOnline: (value) => { online = value; },
    setModalOpen: (value) => { modalOpen = value; },
    setReducedMotion: (value) => { reducedMotion = value; },
    /* 劇情卡目前的顯示欄位（純資料）。 */
    card: () => plain(session.card.value),
    /* 時間軸目前的項目（純資料）。 */
    timeline: () => plain(session.items.value),
    kinds: () => plain(session.items.value).map((item) => item.kind),
    storyOf: (clientMsgId) => { const item = plain(session.items.value).find((entry) => entry.key === 'turn:' + clientMsgId); return item ? item.story : undefined; },
  };
  return harness;
}

/* 攔截 console 與瀏覽器儲存：回傳 { logs, writes, restore }。 */
function trapGlobals() {
  const logs = [];
  const writes = [];
  const original = { console: globalThis.console };
  const names = ['log', 'info', 'warn', 'error', 'debug', 'trace'];
  const saved = {};
  names.forEach((name) => { saved[name] = console[name]; console[name] = (...args) => { logs.push(args.map(String).join(' ')); }; });
  const trap = (name) => new Proxy({}, {
    get: (_target, key) => (key === 'getItem' || key === 'key' ? () => null : (...args) => { writes.push([name, String(key), ...args]); }),
    set: (_target, key, value) => { writes.push([name, String(key), value]); return true; },
  });
  const had = {};
  ['localStorage', 'sessionStorage', 'indexedDB'].forEach((name) => {
    had[name] = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value: trap(name), configurable: true, writable: true });
  });
  return {
    logs, writes,
    restore() {
      names.forEach((name) => { console[name] = saved[name]; });
      Object.keys(had).forEach((name) => {
        if (had[name]) { Object.defineProperty(globalThis, name, had[name]); } else { delete globalThis[name]; }
      });
      void original;
    },
  };
}

module.exports = {
  createHarness, trapGlobals, initialState, failureInfo, turnView, normalTurn, event, sse, plain, SESSION_ID, WORLD, Labels, State, Store, Sync, Vue,
};
