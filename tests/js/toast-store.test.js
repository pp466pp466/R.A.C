// Toast 狀態庫的單元測試（假計時器，不需要等待）。
// 執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createToastStore } = require('../../app/static/js/ui/toast-store.js');

function fakeTimers() {
  let now = 0;
  let id = 0;
  const tasks = new Map();
  return {
    timers: {
      setTimeout(fn, ms) { const handle = ++id; tasks.set(handle, { at: now + ms, fn }); return handle; },
      clearTimeout(handle) { tasks.delete(handle); }
    },
    advance(ms) {
      now += ms;
      const due = [...tasks].filter(([, task]) => task.at <= now).sort((a, b) => a[1].at - b[1].at);
      for (const [handle, task] of due) { tasks.delete(handle); task.fn(); }
    },
    get pending() { return tasks.size; }
  };
}

const setup = (options = {}) => {
  const clock = fakeTimers();
  const store = createToastStore({ timers: clock.timers, ...options });
  return { clock, store };
};

test('show 會加入一則提示並回傳唯一編號', () => {
  const { store } = setup();
  const a = store.show('已儲存');
  const b = store.show('已刪除');
  assert.notEqual(a, b);
  assert.deepEqual(store.state.items.map((i) => i.message), ['已儲存', '已刪除']);
  assert.equal(store.state.items[0].type, 'info');
});

test('預設 4 秒後自動消失，之前不會消失', () => {
  const { store, clock } = setup();
  store.show('會自動消失');
  clock.advance(3999);
  assert.equal(store.state.items.length, 1);
  clock.advance(1);
  assert.equal(store.state.items.length, 0);
  assert.equal(clock.pending, 0, '計時器應已清除');
});

test('可自訂持續時間；時間為 0 代表不會自動消失', () => {
  const { store, clock } = setup();
  store.show('短', { duration: 1000 });
  store.show('常駐', { duration: 0 });
  clock.advance(60000);
  assert.deepEqual(store.state.items.map((i) => i.message), ['常駐']);
});

test('多則提示依序堆疊，各自在自己的時間消失', () => {
  const { store, clock } = setup();
  store.show('第一則', { duration: 1000 });
  store.show('第二則', { duration: 2000 });
  store.show('第三則', { duration: 3000 });
  assert.deepEqual(store.state.items.map((i) => i.message), ['第一則', '第二則', '第三則']);
  clock.advance(1000);
  assert.deepEqual(store.state.items.map((i) => i.message), ['第二則', '第三則']);
  clock.advance(1000);
  assert.deepEqual(store.state.items.map((i) => i.message), ['第三則']);
  clock.advance(1000);
  assert.deepEqual(store.state.items, []);
});

test('手動關閉會移除提示並清除計時器', () => {
  const { store, clock } = setup();
  const id = store.show('手動關閉');
  store.dismiss(id);
  assert.equal(store.state.items.length, 0);
  assert.equal(clock.pending, 0);
  store.dismiss(id);           // 重複關閉不應出錯
  store.dismiss(99999);        // 不存在的編號也不應出錯
});

test('超過可見上限時，最舊的先被移除', () => {
  const { store, clock } = setup({ maxVisible: 3 });
  for (let i = 1; i <= 5; i++) { store.show(`第 ${i} 則`); }
  assert.deepEqual(store.state.items.map((i) => i.message), ['第 3 則', '第 4 則', '第 5 則']);
  assert.equal(clock.pending, 3, '被擠掉的提示，計時器也要一併清除');
});

test('clear 會移除全部提示與計時器', () => {
  const { store, clock } = setup();
  store.show('a'); store.show('b', { duration: 0 }); store.show('c');
  store.clear();
  assert.deepEqual(store.state.items, []);
  assert.equal(clock.pending, 0);
});

test('訊息一律當作純文字字串保存，不解析 HTML', () => {
  const { store } = setup();
  store.show('<img src=x onerror=alert(1)><b>粗體</b>');
  store.show(404);
  assert.equal(store.state.items[0].message, '<img src=x onerror=alert(1)><b>粗體</b>');
  assert.equal(store.state.items[1].message, '404');
  assert.equal(typeof store.state.items[1].message, 'string');
});

test('錯誤類型會被保留，供畫面決定無障礙角色', () => {
  const { store } = setup();
  store.show('失敗了', { type: 'error' });
  assert.equal(store.state.items[0].type, 'error');
});

test('狀態透過注入的 reactive 建立，使畫面能追蹤變化', () => {
  let wrapped = 0;
  const seen = [];
  const { store } = setup({ reactive: (value) => { wrapped += 1; seen.push(value); return value; } });
  assert.equal(wrapped, 1);
  assert.deepEqual(seen[0], { items: [] });
  assert.equal(store.state, seen[0]);
});
