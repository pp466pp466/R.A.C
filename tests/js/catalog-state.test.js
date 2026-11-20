'use strict';
/* 公開目錄的搜尋、篩選與游標分頁狀態（catalog-state.js；見 rpg-script-catalog 規格「目錄畫面的搜尋與篩選」）。
 * 純函式，直接 require；取得資料的函式用可控的替身（延遲到測試決定何時回應），所以請求序號、接續與失敗保留都能逐項驗證。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);

const FILE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'catalog-state.js');
const Catalog = require(FILE);

const card = (id, extra = {}) => ({ id, title: `劇本${id}`, tags: [], ...extra });
const page = (ids, extra = {}) => ({ ok: true, scripts: ids.map((id) => card(id)), has_more: false, next_cursor: null, content_mode: { effective: 'safe', forced: false }, ...extra });
const ids = (state) => state.items.map((item) => item.id);

/* 可控的請求替身：每次呼叫記下選項，回傳的 Promise 由測試決定何時以什麼結果完成。 */
function controlled() {
  const calls = [];
  const fetchPage = (options) => new Promise((resolve, reject) => { calls.push({ options: JSON.parse(JSON.stringify(options)), resolve, reject }); });
  return { calls, fetchPage };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

// ---- 條件的整理 --------------------------------------------------------------------------------------

t('a fresh state is empty and every state is independent', () => {
  const a = Catalog.blank();
  const b = Catalog.blank();
  a.items.push(card('x'));
  assert.deepEqual(b.items, []);
  assert.deepEqual(
    { q: b.q, tag: b.tag, audience: b.audience, seq: b.seq, loading: b.loading, loaded: b.loaded, hasMore: b.hasMore, nextCursor: b.nextCursor, error: b.error },
    { q: '', tag: '', audience: '', seq: 0, loading: false, loaded: false, hasMore: false, nextCursor: '', error: '' },
  );
});

t('filters are tidied without changing their meaning: strings only, trimmed, one space between words, audience limited to the three channels', () => {
  assert.deepEqual(Catalog.normalizeFilters({ q: '  青雲   仙俠 ', tag: ' 仙俠 ', audience: 'female' }), { q: '青雲 仙俠', tag: '仙俠', audience: 'female' });
  assert.deepEqual(Catalog.normalizeFilters({ q: 7, tag: null, audience: 'other' }), { q: '', tag: '', audience: '' });
  assert.deepEqual(Catalog.normalizeFilters(null), { q: '', tag: '', audience: '' });
  assert.deepEqual(Catalog.normalizeFilters('x'), { q: '', tag: '', audience: '' });
  for (const audience of ['male', 'female', 'all']) { assert.equal(Catalog.normalizeFilters({ audience }).audience, audience); }
  assert.equal(Catalog.normalizeFilters({ audience: '__proto__' }).audience, '');
});

t('toggling a tag selects it, and the same tag again (any letter case) clears it; other conditions stay', () => {
  const state = Catalog.blank();
  Catalog.reset(state, { q: '青雲', audience: 'male' });
  const picked = Catalog.withTag(state, 'SciFi');
  assert.deepEqual(picked, { q: '青雲', tag: 'SciFi', audience: 'male' });
  Catalog.reset(state, picked);
  assert.deepEqual(Catalog.withTag(state, 'scifi'), { q: '青雲', tag: '', audience: 'male' });
  assert.deepEqual(Catalog.withTag(state, '仙俠'), { q: '青雲', tag: '仙俠', audience: 'male' });         // 選另一個是換成它，不是取消
  assert.equal(Catalog.sameTag('SciFi', 'scifi'), true);
  assert.equal(Catalog.sameTag('', ''), false);
  assert.equal(Catalog.sameTag(null, 'x'), false);
});

t('with helpers change one condition and never touch the state', () => {
  const state = Catalog.blank();
  Catalog.reset(state, { q: 'a', tag: 'b', audience: 'all' });
  const seq = state.seq;
  assert.deepEqual(Catalog.withQuery(state, '新字'), { q: '新字', tag: 'b', audience: 'all' });
  assert.deepEqual(Catalog.withAudience(state, 'female'), { q: 'a', tag: 'b', audience: 'female' });
  assert.deepEqual(Catalog.filtersOf(state), { q: 'a', tag: 'b', audience: 'all' });
  assert.equal(state.seq, seq);
  assert.equal(Catalog.hasFilters(state), true);
  assert.equal(Catalog.hasFilters(Catalog.blank()), false);
});

// ---- 請求的選項與接續 ---------------------------------------------------------------------------------

t('the request carries only the conditions in use, plus the cursor when there is one', () => {
  const state = Catalog.blank();
  assert.deepEqual(Catalog.requestOptions(state), {});
  Catalog.reset(state, { q: '青雲', audience: 'all' });
  assert.deepEqual(Catalog.requestOptions(state), { q: '青雲', audience: 'all' });
  state.nextCursor = 'abc';
  assert.deepEqual(Catalog.requestOptions(state), { q: '青雲', audience: 'all', cursor: 'abc' });
  assert.ok(!('offset' in Catalog.requestOptions(state)));
});

t('the first page fills the list and remembers the next cursor; load more continues with the same conditions and the cursor', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  Catalog.reset(state, { q: '青雲', tag: '仙俠', audience: 'female' });
  const first = Catalog.load(state, fetchPage);
  assert.equal(state.loading, true);
  calls[0].resolve(page(['a', 'b'], { has_more: true, next_cursor: 'c1' }));
  assert.equal(await first, true);
  assert.deepEqual(ids(state), ['a', 'b']);
  assert.deepEqual([state.hasMore, state.nextCursor, state.loaded, state.loading], [true, 'c1', true, false]);

  const more = Catalog.load(state, fetchPage);
  assert.deepEqual(calls[1].options, { q: '青雲', tag: '仙俠', audience: 'female', cursor: 'c1' });
  calls[1].resolve(page(['c'], { has_more: false, next_cursor: null }));
  await more;
  assert.deepEqual(ids(state), ['a', 'b', 'c']);
  assert.deepEqual([state.hasMore, state.nextCursor, state.loading], [false, '', false]);
});

t('a card that shows up again on the next page (a script came back in between) is kept once', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const first = Catalog.load(state, fetchPage);
  calls[0].resolve(page(['a', 'b'], { has_more: true, next_cursor: 'c1' }));
  await first;
  const more = Catalog.load(state, fetchPage);
  calls[1].resolve(page(['b', 'c']));
  await more;
  assert.deepEqual(ids(state), ['a', 'b', 'c']);
});

t('pressing load more while a page is loading sends nothing; afterwards it works again', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const first = Catalog.load(state, fetchPage);
  assert.equal(await Catalog.load(state, fetchPage), false);
  assert.equal(calls.length, 1);
  calls[0].resolve(page(['a'], { has_more: true, next_cursor: 'c1' }));
  await first;
  const second = Catalog.load(state, fetchPage);
  assert.equal(calls.length, 2);
  calls[1].resolve(page([]));
  await second;
});

t('has_more alone is not enough: it needs a non-empty cursor and at least one row, so a bad response cannot loop forever', async () => {
  for (const body of [
    page(['a'], { has_more: true, next_cursor: null }),
    page(['a'], { has_more: true, next_cursor: '' }),
    page(['a'], { has_more: true, next_cursor: 7 }),
    page([], { has_more: true, next_cursor: 'c1' }),
    page(['a'], { has_more: 'yes', next_cursor: 'c1' }),
  ]) {
    const { calls, fetchPage } = controlled();
    const state = Catalog.blank();
    const loading = Catalog.load(state, fetchPage);
    calls[0].resolve(body);
    await loading;
    assert.equal(state.hasMore, false, JSON.stringify(body));
    assert.equal(state.nextCursor, '', JSON.stringify(body));
  }
});

t('a malformed success body shows an empty loaded list instead of throwing', async () => {
  for (const body of [null, undefined, 'text', {}, { scripts: 'no' }, { scripts: [null, 5, { title: '沒有編號' }, { id: 7 }] }]) {
    const { calls, fetchPage } = controlled();
    const state = Catalog.blank();
    const loading = Catalog.load(state, fetchPage);
    calls[0].resolve(body);
    assert.equal(await loading, true);
    assert.deepEqual(state.items, []);
    assert.equal(state.loaded, true);
    assert.equal(state.loading, false);
  }
});

// ---- 條件改變與請求序號 -------------------------------------------------------------------------------

t('changing a condition clears the list, the cursor and the error, and starts again from the first page', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const first = Catalog.load(state, fetchPage);
  calls[0].resolve(page(['a', 'b'], { has_more: true, next_cursor: 'c1' }));
  await first;
  state.error = '舊的錯誤';
  const seq = state.seq;
  assert.equal(Catalog.reset(state, { tag: '仙俠' }), seq + 1);
  assert.deepEqual([ids(state), state.hasMore, state.nextCursor, state.error, state.loaded, state.loading], [[], false, '', '', false, false]);
  const again = Catalog.load(state, fetchPage);
  assert.deepEqual(calls[1].options, { tag: '仙俠' });                                    // 第一頁：沒有游標
  calls[1].resolve(page(['z']));
  await again;
  assert.deepEqual(ids(state), ['z']);
});

t('a slow answer to the first search never overwrites the newer one (the older answer arrives last)', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  Catalog.reset(state, { q: '甲' });
  const older = Catalog.load(state, fetchPage);
  Catalog.reset(state, { q: '乙' });
  const newer = Catalog.load(state, fetchPage);
  assert.equal(calls.length, 2);                                                         // 條件一改，新請求立刻能送出
  calls[1].resolve(page(['new']));
  assert.equal(await newer, true);
  calls[0].resolve(page(['old1', 'old2'], { has_more: true, next_cursor: 'stale' }));
  assert.equal(await older, false);
  assert.deepEqual(ids(state), ['new']);
  assert.deepEqual([state.hasMore, state.nextCursor, state.loaded, state.loading, state.q], [false, '', true, false, '乙']);
});

t('an old answer that arrives before the newer one is dropped too, and it does not release the loading flag of the newer request', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  Catalog.reset(state, { q: '甲' });
  const older = Catalog.load(state, fetchPage);
  Catalog.reset(state, { q: '乙' });
  const newer = Catalog.load(state, fetchPage);
  calls[0].resolve(page(['old']));
  assert.equal(await older, false);
  assert.deepEqual(ids(state), []);
  assert.equal(state.loading, true);                                                     // 新請求還在路上，旗標不能被舊請求放開
  assert.equal(await Catalog.load(state, fetchPage), false);                             // 所以載入更多仍然不會重複送
  assert.equal(calls.length, 2);
  calls[1].resolve(page(['new']));
  await newer;
  assert.deepEqual([ids(state), state.loading], [['new'], false]);
});

t('a failure of an outdated request neither shows an error nor stops the newer request', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  Catalog.reset(state, { q: '甲' });
  const older = Catalog.load(state, fetchPage);
  Catalog.reset(state, { q: '乙' });
  const newer = Catalog.load(state, fetchPage);
  calls[0].reject({ kind: 'network', message: '連線失敗' });
  assert.equal(await older, false);
  assert.equal(state.error, '');
  assert.equal(state.loading, true);
  calls[1].resolve(page(['new']));
  await newer;
  assert.deepEqual([ids(state), state.error], [['new'], '']);
});

t('three quick changes in a row leave only the last one on the screen, whatever the order of the answers', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const loads = [];
  for (const q of ['一', '二', '三']) { Catalog.reset(state, { q }); loads.push(Catalog.load(state, fetchPage)); }
  calls[2].resolve(page(['three']));
  calls[0].resolve(page(['one']));
  calls[1].resolve(page(['two']));
  await Promise.all(loads);
  assert.deepEqual(ids(state), ['three']);
  assert.equal(state.q, '三');
});

// ---- 失敗 --------------------------------------------------------------------------------------------

t('a failed load more keeps the cards already loaded, shows the error, and retrying fetches the very same page', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  Catalog.reset(state, { q: '青雲' });
  const first = Catalog.load(state, fetchPage);
  calls[0].resolve(page(['a', 'b'], { has_more: true, next_cursor: 'c1' }));
  await first;
  const failed = Catalog.load(state, fetchPage);
  calls[1].reject({ kind: 'network', message: '網路不穩，請稍後再試。' });
  assert.equal(await failed, false);
  assert.deepEqual(ids(state), ['a', 'b']);
  assert.deepEqual([state.error, state.nextCursor, state.hasMore, state.loading, state.loaded], ['網路不穩，請稍後再試。', 'c1', true, false, true]);
  const retry = Catalog.load(state, fetchPage);
  assert.deepEqual(calls[2].options, calls[1].options);                                   // 同一頁：同樣的條件與游標
  assert.equal(state.error, '');                                                         // 重試一開始就清掉舊的錯誤
  calls[2].resolve(page(['c']));
  await retry;
  assert.deepEqual([ids(state), state.error], [['a', 'b', 'c'], '']);
});

t('a first page that fails leaves the list unloaded, so the page shows the error with a retry and no "empty" message', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const loading = Catalog.load(state, fetchPage);
  calls[0].reject({ kind: 'server', status: 500, message: '' });
  await loading;
  assert.deepEqual([state.loaded, state.error, state.loading], [false, '無法載入，請稍後再試。', false]);
  assert.equal(Catalog.emptyKind(state), '');
});

t('a canceled request shows no error message', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const loading = Catalog.load(state, fetchPage);
  calls[0].reject({ kind: 'canceled', message: '已取消' });
  await loading;
  assert.equal(state.error, '');
  assert.equal(state.loading, false);
});

t('the hooks see a current page once, and every failure (a session that ran out affects whichever search was running)', async () => {
  const { calls, fetchPage } = controlled();
  const state = Catalog.blank();
  const seen = { pages: [], failures: [] };
  const hooks = { onPage: (data) => seen.pages.push(data.content_mode.effective), onFailure: (info) => seen.failures.push(info.status) };
  Catalog.reset(state, { q: '甲' });
  const older = Catalog.load(state, fetchPage, hooks);
  Catalog.reset(state, { q: '乙' });
  const newer = Catalog.load(state, fetchPage, hooks);
  calls[0].reject({ kind: 'server', status: 401, message: '' });
  await older;
  calls[1].resolve(page(['n'], { content_mode: { effective: 'adult', forced: false } }));
  await newer;
  assert.deepEqual(seen, { pages: ['adult'], failures: [401] });
  const stalePage = controlled();
  Catalog.reset(state, { q: '丙' });
  const third = Catalog.load(state, stalePage.fetchPage, hooks);
  Catalog.reset(state, { q: '丁' });
  stalePage.calls[0].resolve(page(['x']));
  await third;
  assert.deepEqual(seen.pages, ['adult']);                                               // 過時的成功回應不觸發 onPage
});

// ---- 空清單 ------------------------------------------------------------------------------------------

t('the empty message depends on whether a filter is on, and never shows while loading, failed or non-empty', async () => {
  const state = Catalog.blank();
  assert.equal(Catalog.emptyKind(state), '');                                            // 還沒載完
  state.loaded = true;
  assert.equal(Catalog.emptyKind(state), 'none');
  Catalog.reset(state, { q: '找不到的字' });
  assert.equal(Catalog.emptyKind(state), '');                                            // reset 之後又回到「還沒載完」
  state.loaded = true;
  assert.equal(Catalog.emptyKind(state), 'filtered');
  state.error = '失敗';
  assert.equal(Catalog.emptyKind(state), '');
  state.error = '';
  state.items.push(card('a'));
  assert.equal(Catalog.emptyKind(state), '');
});

// ---- 常用標籤 ----------------------------------------------------------------------------------------

t('popular tags are tidied: strings only, no repeats, at most 30, bad counts become 0', () => {
  const rows = [{ tag: '仙俠', count: 5 }, { tag: '仙俠', count: 1 }, { tag: '', count: 2 }, { tag: 7, count: 1 }, null, 'x', { tag: '科幻', count: 'many' }, { tag: '懸疑' }];
  assert.deepEqual(Catalog.parseTags({ tags: rows }), [{ tag: '仙俠', count: 5 }, { tag: '科幻', count: 0 }, { tag: '懸疑', count: 0 }]);
  const many = Array.from({ length: 45 }, (_, index) => ({ tag: `標籤${index}`, count: 45 - index }));
  assert.equal(Catalog.parseTags({ tags: many }).length, 30);
  assert.equal(Catalog.parseTags({ tags: many }).length, Catalog.TAGS_MAX);
  for (const body of [null, undefined, {}, { tags: 'no' }, 'text']) { assert.deepEqual(Catalog.parseTags(body), []); }
  assert.deepEqual(Catalog.parseTags({ tags: [{ tag: '__proto__', count: 1 }, { tag: 'constructor', count: 1 }] }).map((row) => row.tag), ['__proto__', 'constructor']);
});

t('popular tags load once; a failure is silent (no chips, nothing else affected) and can be tried again', async () => {
  const state = Catalog.blank();
  let requests = 0;
  const ok = () => { requests += 1; return Promise.resolve({ ok: true, tags: [{ tag: '仙俠', count: 3 }] }); };
  assert.equal(await Catalog.loadTags(state, ok), true);
  assert.deepEqual(state.tags, [{ tag: '仙俠', count: 3 }]);
  assert.equal(await Catalog.loadTags(state, ok), false);                                // 載過就不再送
  assert.equal(requests, 1);

  const failing = Catalog.blank();
  assert.equal(await Catalog.loadTags(failing, () => Promise.reject({ kind: 'network' })), false);
  assert.deepEqual([failing.tags, failing.tagsLoaded, failing.tagsLoading, failing.error], [[], false, false, '']);
  assert.equal(await Catalog.loadTags(failing, ok), true);                               // 失敗過可以再試
  assert.equal(failing.tags.length, 1);
});

t('popular tags do not start a second request while one is on its way', async () => {
  const state = Catalog.blank();
  let release;
  let requests = 0;
  const slow = () => { requests += 1; return new Promise((resolve) => { release = resolve; }); };
  const first = Catalog.loadTags(state, slow);
  assert.equal(await Catalog.loadTags(state, slow), false);
  assert.equal(requests, 1);
  release({ tags: [{ tag: 'a', count: 1 }] });
  await first;
  await tick();
  assert.equal(state.tags.length, 1);
});

// ---- 不碰瀏覽器 --------------------------------------------------------------------------------------

t('the module keeps nothing in the browser: no storage, no window, no network of its own', () => {
  const source = fs.readFileSync(FILE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.ok(!/\b(window|document|location|localStorage|sessionStorage|indexedDB|axios|fetch|XMLHttpRequest)\b/.test(source));
  const bare = vm.createContext({ Promise, console });
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), bare, { filename: 'catalog-state.js' });
  assert.equal(typeof bare.Game.catalogState.load, 'function');                          // 瀏覽器的載入方式：掛在全域的 Game.catalogState
});
