'use strict';
/* rpg-entry-pages 規格的入口頁（entry.js）：以真的 Vue、真的 Auth、真的 game-labels.js 與 game-api.js 在 `vm` 沙盒執行 `setup()`，
 * 只把 axios、location 與 mount 換成替身。驗證預設分頁、載入更多只送一次且 finally 恢復、has_more 為假時沒有下一頁、失敗保留已載入的卡片與重試、
 * 閘門非空時「開始」禁用、未成年顯示原因、401 導向登入；最後檢查樣板使用的每個名稱都由 setup() 回傳。
 * 沒有瀏覽器可以掛載；樣板的文字與結構由 tests/test_game_pages.py 檢查。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 每個測試都有逾時：被破壞的程式若讓 Promise 永遠不完成，測試要失敗，不能掛住
const t = (name, fn) => test(name, { timeout: 5000 }, fn);

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const STATE = { user: { member_tier: 'free', story_preference: 'all' }, gate: null };
const ID_A = '11111111-1111-4111-8111-111111111111';
const ID_B = '22222222-2222-4222-8222-222222222222';
const ID_C = '33333333-3333-4333-8333-333333333333';

function serverError(status, code, message, fields) {
  return { response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } } };
}

const card = (id, title = `劇本${id.slice(0, 2)}`) => ({
  id, title, summary: '簡介', world_name: '世界', tags: ['仙俠'], author_name: '官方', author_code: null, content_tier: 'safe', audience: 'all',
});
const save = (id, title = `存檔${id.slice(0, 2)}`) => ({
  id, title, content_tier: 'safe', status: 'active', player_name: '林初', completed_turns: 3, last_active_at: '2026-10-27T12:00:00Z',
});
const catalogPage = (scripts, extra = {}) => ({ data: { ok: true, scripts, has_more: false, content_mode: { effective: 'standard', is_adult: true }, ...extra } });
const savesPage = (sessions, extra = {}) => ({ data: { ok: true, sessions, has_more: false, ...extra } });

const CATALOG_URL = '/api/sessions/catalog';
const SAVES_URL = '/api/sessions';
const emptySaves = () => Promise.resolve(savesPage([]));

/* 載入入口頁需要的腳本並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, params }，回傳 Promise。
 * 掛載時會**並行載入「選劇本」與「我的存檔」兩個清單的第一頁**（預設分頁依玩家有沒有存檔決定）。只關心「選劇本」的測試傳 `saves`（存檔清單請求的回應函式，
 * 通常是 `emptySaves`），這樣依序消耗的回應佇列與請求次數不會被存檔清單的請求打亂，並以 `catalogRequests()` 取得「選劇本」的請求；沒給 `saves` 時，所有請求都交給 `respond`。 */
function setupEntry({ state = STATE, respond = () => Promise.reject(new Error('unexpected request')), saves = null } = {}) {
  const requests = [];
  const navigations = [];
  const mounted = [];
  const sandbox = { console, Promise, setTimeout, clearTimeout };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/game', search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state' ? { textContent: typeof state === 'string' ? state : JSON.stringify(state) } : null),
  };
  const axios = (request) => {
    requests.push(JSON.parse(JSON.stringify(request)));
    return saves && request.url === SAVES_URL ? saves(request) : respond(request);
  };
  axios.defaults = { headers: { common: {} } };
  sandbox.axios = axios;
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });

  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, {
    createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; },
    onMounted: (hook) => { mounted.push(hook); },
  });
  ['js/auth/auth-common.js', 'js/ui/restriction.js', 'js/game/game-labels.js', 'js/game/game-api.js'].forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: () => {} });
  run('app/static/js/game/entry.js');
  assert.ok(options && typeof options.setup === 'function', 'entry.js 沒有呼叫 createApp');
  const page = options.setup();
  return {
    page, requests, navigations, mount: () => Promise.all(mounted.map((hook) => hook())),
    catalogRequests: () => requests.filter((request) => request.url === CATALOG_URL),
  };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));     // 沙盒物件的原型與測試行程不同，比對前先 JSON 往返
const ids = (list) => plain(list.items.map((item) => item.id));

// ---- 掛載與預設分頁 -------------------------------------------------------------------------------

// 這一項是**有意識地改寫**（rpg-game-ui 的預設分頁）：以前掛載只載入「選劇本」，「我的存檔」要等玩家第一次切換才載入；現在掛載就並行載入兩個清單的第一頁，
// 好判斷玩家有沒有存檔來決定預設分頁。沒有存檔時預設分頁仍是「選劇本」。
t('with no saves the default tab is the script catalog; mounting loads the first page of both lists, and only those', async () => {
  const { page, requests, mount } = setupEntry({ respond: (request) => Promise.resolve(request.url === CATALOG_URL ? catalogPage([card(ID_A), card(ID_B)]) : savesPage([])) });
  assert.equal(page.tab.value, 'scripts');
  assert.equal(requests.length, 0);                                 // setup() 本身不送請求，掛載才載入
  await mount();
  assert.deepEqual(requests, [
    { method: 'get', url: CATALOG_URL, params: { offset: 0 } },
    { method: 'get', url: SAVES_URL, params: { offset: 0 } },
  ]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);
  assert.equal(page.catalog.loaded, true);
  assert.equal(page.catalog.loading, false);
  assert.equal(page.saves.loaded, true);                            // 存檔清單提前載入（空的）
  assert.equal(page.saves.items.length, 0);
  assert.equal(page.tab.value, 'scripts');                          // 沒有存檔：維持「選劇本」
});

t('the first page also carries the content mode summary used to explain missing scripts', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', is_adult: false } })) });
  await mount();
  assert.deepEqual(JSON.parse(JSON.stringify(page.contentMode.value)), { effective: 'safe', is_adult: false });
  assert.ok(page.modeNotice.value.text.includes('18'));
});

t('the initial state supplies the member tier text and the story preference text', () => {
  const { page } = setupEntry({ state: { user: { member_tier: 'vip', story_preference: 'female' }, gate: null } });
  assert.equal(page.memberText.value, '尊榮');
  assert.equal(page.preferenceText.value, '女頻');
  assert.equal(setupEntry({ state: STATE }).page.preferenceText.value, '');             // 全讀者不另外說明
});

t('a damaged initial state does not break the page and leaves nothing disabled by guesswork', () => {
  for (const state of ['not json', '[]', 'null', '{"gate": 5, "user": null}', '{"gate": ""}']) {
    const { page } = setupEntry({ state });
    assert.equal(page.gate.value, null, state);
    assert.equal(page.canStart.value, true, state);
    assert.equal(page.memberText.value, '一般', state);
  }
});

// ---- 預設分頁：有存檔時是「我的存檔」（rpg-entry-pages 規格）--------------------------------------------------

const gate = () => {
  let resolve; let reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
};
const routed = (catalog, saves) => (request) => (request.url === CATALOG_URL ? catalog(request) : saves(request));
const okCatalog = () => Promise.resolve(catalogPage([card(ID_A)]));

t('a player who already has saves lands on the saves tab with the most recent save first, and the catalog is one click away', async () => {
  const { page, requests, mount } = setupEntry({ respond: routed(okCatalog, () => Promise.resolve(savesPage([save(ID_C), save(ID_B)]))) });
  assert.equal(page.tab.value, 'scripts');                          // 載入完成之前仍是「選劇本」
  await mount();
  assert.equal(page.tab.value, 'saves');
  assert.deepEqual(ids(page.saves), [ID_C, ID_B]);                  // 伺服器給的順序：最近活動在前
  assert.deepEqual(ids(page.catalog), [ID_A]);                      // 「選劇本」也載好了
  page.selectTab('scripts');
  assert.equal(page.tab.value, 'scripts');
  page.selectTab('saves');
  await tick();
  assert.equal(requests.length, 2);                                 // 切換不再多送請求：兩個清單掛載時就載好了
});

t('when the saves cannot be read the page stays on the script catalog, the saves tab shows its own error and retry, and a later retry does not pull the player away', async () => {
  let savesCalls = 0;
  const { page, mount } = setupEntry({
    respond: routed(okCatalog, () => {
      savesCalls += 1;
      return savesCalls === 1 ? Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')) : Promise.resolve(savesPage([save(ID_B)]));
    }),
  });
  await mount();
  assert.equal(page.tab.value, 'scripts');                          // 退回「選劇本」
  assert.deepEqual(ids(page.catalog), [ID_A]);
  assert.equal(page.catalog.error, '');                             // 沒有整頁錯誤
  assert.equal(page.saves.error, '系統發生錯誤，請稍後再試。');       // 「我的存檔」分頁顯示錯誤與重試
  assert.equal(page.saves.loaded, false);
  page.selectTab('saves');
  await tick();
  assert.equal(savesCalls, 1);                                      // 開分頁不會自動重試（由重試按鈕做）
  assert.equal(await page.loadSaves(), true);
  assert.deepEqual(ids(page.saves), [ID_B]);
  assert.equal(page.tab.value, 'saves');
});

t('a failed save request that comes after the player is on the script catalog leaves the catalog alone', async () => {
  const { page, mount } = setupEntry({ respond: routed(okCatalog, () => Promise.reject(new Error('Network Error'))) });
  await mount();
  assert.equal(page.tab.value, 'scripts');
  assert.equal(page.catalog.loaded, true);
  assert.ok(page.saves.error);
  assert.equal(page.catalog.error, '');
});

t('a manual switch before the saves arrive is never overridden, whichever tab the player picked', async () => {
  for (const picked of ['scripts', 'saves']) {
    const slow = gate();
    const { page, mount } = setupEntry({ respond: routed(okCatalog, () => slow.promise) });
    const mounting = mount();
    await tick();
    page.selectTab(picked);                                         // 存檔清單載入完成之前，玩家自己切換
    slow.resolve(savesPage([save(ID_B)]));
    await mounting;
    assert.equal(page.tab.value, picked, picked);
    assert.deepEqual(ids(page.saves), [ID_B]);
  }
});

t('picking the script tab that is already shown also counts as the player\'s choice', async () => {
  const slow = gate();
  const { page, mount } = setupEntry({ respond: routed(okCatalog, () => slow.promise) });
  const mounting = mount();
  await tick();
  page.selectTab('scripts');
  slow.resolve(savesPage([save(ID_B), save(ID_C)]));
  await mounting;
  assert.equal(page.tab.value, 'scripts');
});

t('the two first pages load in parallel and neither waits for the other', async () => {
  const slowCatalog = gate();
  const { page, requests, mount } = setupEntry({ respond: routed(() => slowCatalog.promise, () => Promise.resolve(savesPage([save(ID_B)]))) });
  const mounting = mount();
  await tick();
  assert.deepEqual(requests.map((request) => request.url), [CATALOG_URL, SAVES_URL]);   // 兩個請求都已送出，沒有誰等誰
  assert.equal(page.catalog.loading, true);
  assert.equal(page.tab.value, 'saves');                            // 存檔先到就先切換，不等「選劇本」
  slowCatalog.resolve(catalogPage([card(ID_A)]));
  await mounting;
  assert.deepEqual(ids(page.catalog), [ID_A]);

  const slowSaves = gate();
  const second = setupEntry({ respond: routed(() => Promise.resolve(catalogPage([card(ID_A)])), () => slowSaves.promise) });
  const secondMounting = second.mount();
  await tick();
  assert.deepEqual(ids(second.page.catalog), [ID_A]);               // 「選劇本」不等存檔
  assert.equal(second.page.tab.value, 'scripts');
  slowSaves.resolve(savesPage([save(ID_B)]));
  await secondMounting;
  assert.equal(second.page.tab.value, 'saves');
});

t('both requests failing with an expired session send the player to the login page only once', async () => {
  const expired = () => Promise.reject(serverError(401, 'unauthorized', '請先登入。'));
  const { navigations, mount } = setupEntry({ respond: routed(expired, expired) });
  await mount();
  assert.deepEqual(navigations, ['/login?next=%2Fgame']);
});

t('a save list whose body is malformed counts as no saves: the catalog stays and nothing throws', async () => {
  for (const body of [{ data: null }, { data: {} }, { data: { sessions: 'x' } }, { data: { sessions: [] , has_more: true } }]) {
    const { page, mount } = setupEntry({ respond: routed(okCatalog, () => Promise.resolve(body)) });
    await mount();
    assert.equal(page.tab.value, 'scripts', JSON.stringify(body));
    assert.equal(page.saves.hasMore, false);
  }
});

// ---- 載入更多 -------------------------------------------------------------------------------------

t('load more appends the next page with the offset the server consumed, and the button state is restored', async () => {
  const pages = [catalogPage([card(ID_A), card(ID_B)], { has_more: true }), catalogPage([card(ID_C)])];
  const { page, catalogRequests, mount } = setupEntry({ respond: () => Promise.resolve(pages.shift()), saves: emptySaves });
  await mount();
  assert.equal(page.catalog.hasMore, true);
  assert.equal(await page.loadCatalog(), true);
  assert.deepEqual(catalogRequests().map((r) => r.params), [{ offset: 0 }, { offset: 2 }]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B, ID_C]);
  assert.equal(page.catalog.hasMore, false);                        // has_more 為假：不再顯示「載入更多」
  assert.equal(page.catalog.loading, false);
});

t('while a page is loading, pressing load more again sends nothing; afterwards it is enabled again', async () => {
  let release;
  const { page, catalogRequests, mount } = setupEntry({
    respond: () => new Promise((resolve) => { release = () => resolve(catalogPage([card(ID_A)], { has_more: true })); }),
    saves: emptySaves,
  });
  const first = mount();
  await tick();
  assert.equal(page.catalog.loading, true);
  assert.equal(await page.loadCatalog(), false);                    // 重複點擊被忽略
  assert.equal(catalogRequests().length, 1);
  release();
  await first;
  assert.equal(page.catalog.loading, false);                        // 成功後恢復
  assert.equal(page.catalog.hasMore, true);
});

t('a card that comes back on the next page (a script was published in between) is shown once', async () => {
  const pages = [catalogPage([card(ID_A), card(ID_B)], { has_more: true }), catalogPage([card(ID_B), card(ID_C)])];
  const { page, catalogRequests, mount } = setupEntry({ respond: () => Promise.resolve(pages.shift()), saves: emptySaves });
  await mount();
  await page.loadCatalog();
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B, ID_C]);
  assert.equal(page.catalog.offset, 4);                             // 位移依伺服器實際回的筆數前進，不是去重後的筆數
  assert.deepEqual(catalogRequests().map((r) => r.params.offset), [0, 2]);
});

t('has_more with an empty page cannot loop forever', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([], { has_more: true })) });
  await mount();
  assert.equal(page.catalog.hasMore, false);
  assert.equal(page.catalog.loaded, true);
});

t('a malformed success body shows an empty list instead of throwing', async () => {
  for (const data of [undefined, null, {}, { scripts: 'x' }, { scripts: [null, 5, {}, { title: '沒有 id' }, { id: 7 }] }]) {
    const { page, mount } = setupEntry({ respond: () => Promise.resolve({ data }) });
    await mount();
    assert.deepEqual(plain(page.catalog.items), [], JSON.stringify(data));
    assert.equal(page.catalog.loading, false);
    assert.equal(page.catalog.hasMore, false);
  }
});

// ---- 失敗與重試 -----------------------------------------------------------------------------------

t('a failed load keeps the cards already loaded, shows the error, and retry fetches the same page', async () => {
  let call = 0;
  const { page, catalogRequests, mount } = setupEntry({
    respond: () => {
      call += 1;
      if (call === 1) { return Promise.resolve(catalogPage([card(ID_A)], { has_more: true })); }
      if (call === 2) { return Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')); }
      return Promise.resolve(catalogPage([card(ID_B)]));
    },
    saves: emptySaves,
  });
  await mount();
  assert.equal(await page.loadCatalog(), false);
  assert.deepEqual(ids(page.catalog), [ID_A]);                      // 已載入的卡片沒有消失
  assert.equal(page.catalog.error, '系統發生錯誤，請稍後再試。');
  assert.equal(page.catalog.loading, false);                        // finally 恢復
  assert.equal(await page.loadCatalog(), true);                     // 重試
  assert.deepEqual(catalogRequests().map((r) => r.params.offset), [0, 1, 1]);  // 失敗時位移沒有前進，重試的是同一頁
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);
  assert.equal(page.catalog.error, '');
});

t('a first page that fails leaves the list unloaded (so no "empty" message) with a retryable error', async () => {
  let call = 0;
  const { page, mount } = setupEntry({
    respond: () => { call += 1; return call === 1 ? Promise.reject(new Error('Network Error')) : Promise.resolve(catalogPage([card(ID_A)])); },
  });
  await mount();
  assert.equal(page.catalog.loaded, false);
  assert.ok(page.catalog.error.length > 0);
  assert.equal(page.catalog.items.length, 0);
  await page.loadCatalog();
  assert.equal(page.catalog.loaded, true);
  assert.deepEqual(ids(page.catalog), [ID_A]);
});

t('a network failure, a timeout and a server error each give a Traditional Chinese message', async () => {
  const errors = [new Error('Network Error'), Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }), serverError(503, 'service_unavailable', '服務暫時無法使用，請稍後再試。')];
  for (const error of errors) {
    const { page, mount } = setupEntry({ respond: () => Promise.reject(error) });
    await mount();
    assert.match(page.catalog.error, /[一-鿿]/);
    assert.equal(page.catalog.loading, false);
  }
});

t('a canceled request shows no error message', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' })) });
  await mount();
  assert.equal(page.catalog.error, '');
  assert.equal(page.catalog.loading, false);
});

t('an expired session sends the player to the login page and keeps the position', async () => {
  const { page, navigations, mount } = setupEntry({ respond: () => Promise.reject(serverError(401, 'unauthorized', '請先登入。')) });
  await mount();
  assert.deepEqual(navigations, ['/login?next=%2Fgame']);
  assert.equal(page.catalog.loading, false);
});

t('a locked account gets the appeal shortcut', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.reject(serverError(403, 'account_locked', '您的帳號已被鎖定。')) });
  assert.equal(page.restriction.value, null);
  await mount();
  assert.equal(page.restriction.value, 'locked');
});

// ---- 開始 -----------------------------------------------------------------------------------------

t('start goes to the wizard path of that version', async () => {
  const { page, navigations, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A)])) });
  await mount();
  assert.equal(page.canStart.value, true);
  assert.equal(page.start(page.catalog.items[0]), true);
  assert.deepEqual(navigations, [`/game/start/${ID_A}`]);
});

t('with a non-empty gate start is disabled and goes nowhere, while the list can still be browsed', async () => {
  const { page, navigations, mount } = setupEntry({
    state: { user: { member_tier: 'free', story_preference: 'all' }, gate: 'age_confirmation_required' },
    respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', is_adult: true } })),
  });
  await mount();
  assert.deepEqual(ids(page.catalog), [ID_A]);                      // 清單仍可瀏覽
  assert.equal(page.canStart.value, false);
  assert.equal(page.start(page.catalog.items[0]), false);
  assert.deepEqual(navigations, []);
  assert.equal(page.gateNotice.value.link, '/account');
});

t('start ignores anything that is not a card with a string id', async () => {
  const { page, navigations } = setupEntry();
  for (const junk of [undefined, null, {}, { id: 5 }, 'x']) { assert.equal(page.start(junk), false); }
  assert.deepEqual(navigations, []);
});

t('a minor is told why some scripts are missing and nothing on the page can raise the mode', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', is_adult: false } })) });
  await mount();
  const notice = page.modeNotice.value;
  assert.ok(notice.text.includes('安全模式') && notice.link === null);
  const forbidden = ['setMode', 'changeMode', 'raiseMode', 'saveMode'];
  for (const name of forbidden) { assert.ok(!(name in page), name); }
});

// ---- 我的存檔 -------------------------------------------------------------------------------------

t('opening the saves tab loads the first page once; opening it again does not reload', async () => {
  const { page, requests, mount } = setupEntry({
    respond: (request) => Promise.resolve(request.url === '/api/sessions/catalog' ? catalogPage([card(ID_A)]) : savesPage([save(ID_B)])),
  });
  await mount();
  page.selectTab('saves');
  await tick();
  assert.equal(page.tab.value, 'saves');
  assert.deepEqual(ids(page.saves), [ID_B]);
  page.selectTab('scripts');
  page.selectTab('saves');
  await tick();
  assert.deepEqual(requests.map((r) => r.url), ['/api/sessions/catalog', '/api/sessions']);
  assert.deepEqual(requests[1].params, { offset: 0 });
});

t('an empty save list is loaded and empty (the page shows guidance, not an error)', async () => {
  const { page } = setupEntry({ respond: () => Promise.resolve(savesPage([])) });
  page.selectTab('saves');
  await tick();
  assert.equal(page.saves.loaded, true);
  assert.equal(page.saves.items.length, 0);
  assert.equal(page.saves.error, '');
});

t('the saves tab keeps its cards when a later page fails, and retry works', async () => {
  let call = 0;
  const { page } = setupEntry({
    respond: () => {
      call += 1;
      if (call === 1) { return Promise.resolve(savesPage([save(ID_A)], { has_more: true })); }
      if (call === 2) { return Promise.reject(new Error('Network Error')); }
      return Promise.resolve(savesPage([save(ID_B)]));
    },
  });
  page.selectTab('saves');
  await tick();
  await page.loadSaves();
  assert.deepEqual(ids(page.saves), [ID_A]);
  assert.ok(page.saves.error);
  assert.equal(await page.loadSaves(), true);
  assert.deepEqual(ids(page.saves), [ID_A, ID_B]);
  assert.equal(page.saves.error, '');
});

t('a failed first load of the saves is not retried every time the tab is opened (the retry button does that)', async () => {
  let calls = 0;
  const { page } = setupEntry({ respond: () => { calls += 1; return Promise.reject(new Error('Network Error')); } });
  page.selectTab('saves');
  await tick();
  page.selectTab('scripts');
  page.selectTab('saves');
  await tick();
  assert.equal(calls, 1);
  assert.equal(page.saves.loaded, false);
  await page.loadSaves();
  assert.equal(calls, 2);
});

t('an unknown tab name is ignored', () => {
  const { page } = setupEntry();
  for (const name of ['', 'settings', undefined, null, '__proto__']) { page.selectTab(name); }
  assert.equal(page.tab.value, 'scripts');
});

t('save links are site-internal paths with the identifier encoded', () => {
  const { page } = setupEntry();
  assert.equal(page.sessionPath(ID_A), `/game/${ID_A}`);
  assert.equal(page.sessionPath('a/b'), '/game/a%2Fb');
});

// ---- 樣板與腳本的對應 -----------------------------------------------------------------------------

const JS_WORDS = new Set(['true', 'false', 'null', 'undefined', 'in', 'of', 'typeof']);
const LOOP_VARS = new Set(['card', 'save', 'tag', 'index']);

/* 取出 Vue 樣板裡所有運算式（插值與 v-*、:、@ 屬性），回傳其中最外層的識別字（不含屬性存取與字串內容）。 */
function templateNames(html) {
  const body = html.slice(html.indexOf('{% raw %}'), html.indexOf('{% endraw %}'));
  const expressions = [];
  for (const match of body.matchAll(/\{\{([\s\S]*?)\}\}/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\s(?:v-if|v-else-if|v-show|:[\w-]+|@[\w.-]+)="([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sv-for="[^"]*?\sin\s([^"]*)"/g)) { expressions.push(match[1]); }
  const names = new Set();
  for (const expression of expressions) {
    const stripped = expression.replace(/'[^']*'/g, "''");
    for (const match of stripped.matchAll(/(?<![\w.$])([A-Za-z_$][\w$]*)/g)) { names.add(match[1]); }
  }
  return [...names].filter((name) => !JS_WORDS.has(name) && !LOOP_VARS.has(name)).sort();
}

t('every name the index template uses is returned by setup()', () => {
  const html = read('app/templates/game/index.html');
  const names = templateNames(html);
  assert.ok(names.length >= 15, `只掃到 ${names.length} 個名稱，掃描可能是空洞的：${names}`);
  const { page } = setupEntry();
  const missing = names.filter((name) => !(name in page));
  assert.deepEqual(missing, [], `樣板用到但 setup() 沒有回傳：${missing}`);
  for (const must of ['catalog', 'saves', 'tab', 'gateNotice', 'modeNotice', 'canStart', 'loadCatalog', 'loadSaves', 'selectTab', 'start', 'sessionPath', 'restriction']) {
    assert.ok(names.includes(must), `樣板沒有用到 ${must}`);
  }
});

t('the template shows load more only while another page exists, and the retry button only while there is an error', () => {
  const html = read('app/templates/game/index.html');
  assert.match(html, /v-if="catalog\.error \|\| catalog\.hasMore"/);
  assert.match(html, /v-if="catalog\.error" type="button" class="ui-btn" :disabled="catalog\.loading" @click="loadCatalog">重試</);
  assert.match(html, /v-else type="button" class="ui-btn" :disabled="catalog\.loading"[^>]*@click="loadCatalog"/);
  assert.match(html, /<button type="button" class="ui-btn ui-btn--primary" :disabled="!canStart" @click="start\(card\)">開始<\/button>/);
});

// ---- 範本能編譯 -----------------------------------------------------------------------------------

/* 用專案自己離線引入的 Vue 編譯 `game/index.html` 的 raw 區塊（做法同 script-workshop-template.test.js）：
 * 沒有瀏覽器可以掛載，但運算式的語法錯誤（括號沒對、少了運算元）在編譯時就會拋出，這是最便宜也最容易漏掉的一類錯誤。
 * 正式版的編譯器對沒關閉的標籤不會拋錯，標籤是否成對由 tests/test_game_pages.py 檢查。 */
function loadVueWithCompiler() {
  const sandbox = { console };
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.window = sandbox;
  sandbox.document = {                          // 編譯器在瀏覽器裡用 DOM 解開 HTML 實體；本範本的屬性沒有使用實體，以原樣回傳的替身滿足它
    createElement: () => ({
      set innerHTML(value) { this.html = value; },
      get textContent() { return this.html; },
      get children() {
        const match = /^<div foo="([\s\S]*)">$/.exec(this.html || '');
        return [{ getAttribute: () => (match ? match[1].replace(/&quot;/g, '"') : null) }];
      },
    }),
  };
  vm.createContext(sandbox);
  vm.runInContext(read('app/static/vendor/vue/vue.global.prod.js') + '\n;this.__Vue = Vue;', sandbox);
  return sandbox.__Vue;
}

function rawTemplate(file) {
  const html = read(file);
  const start = html.indexOf('{% raw %}');
  const end = html.lastIndexOf('{% endraw %}');
  assert.ok(start >= 0 && end > start, '找不到 raw 區塊');
  return '<div>' + html.slice(start + '{% raw %}'.length, end) + '</div>';
}

t('the index template compiles with the offline Vue', () => {
  assert.equal(typeof loadVueWithCompiler().compile(rawTemplate('app/templates/game/index.html')), 'function');
});

t('a broken expression would be caught by the same check (self test)', () => {
  const Vue = loadVueWithCompiler();
  assert.throws(() => Vue.compile('<div :a="b +"></div>'));
  assert.throws(() => Vue.compile('<div @click="go("></div>'));
});
