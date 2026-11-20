'use strict';
/* rpg-entry-pages 規格的入口頁（entry.js）：以真的 Vue、真的 Auth、真的 game-labels.js 與 game-api.js 在 `vm` 沙盒執行 `setup()`，
 * 只把 axios、location 與 mount 換成替身。驗證預設分頁、載入更多只送一次且 finally 恢復、has_more 為假時沒有下一頁、失敗保留已載入的卡片與重試、
 * 閘門非空時「開始」禁用、兩種安全模式與緊急煞車的說明、點選未綁定劇本時的詢問流程（前往設定、維持安全模式、Esc 視同取消）、401 導向登入；
 * 最後檢查樣板使用的每個名稱都由 setup() 回傳。
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
const catalogPage = (scripts, extra = {}) => ({ data: { ok: true, scripts, has_more: false, content_mode: { effective: 'adult', forced: false }, ...extra } });
const savesPage = (sessions, extra = {}) => ({ data: { ok: true, sessions, has_more: false, ...extra } });

const CATALOG_URL = '/api/sessions/catalog';
const TAGS_URL = '/api/sessions/catalog/tags';
const SAVES_URL = '/api/sessions';
const noTags = () => Promise.resolve({ data: { ok: true, tags: [] } });
const emptySaves = () => Promise.resolve(savesPage([]));

/* 載入入口頁需要的腳本並回傳 setup() 的結果。`respond(request)` 是 axios 的替身：收到 { method, url, params }，回傳 Promise。
 * 掛載時會**並行載入「選劇本」與「我的存檔」兩個清單的第一頁與常用標籤**（預設分頁依玩家有沒有存檔決定）；常用標籤的請求由 `tags`（預設空清單）回應，不經 `respond` 與 `saves`。只關心「選劇本」的測試傳 `saves`（存檔清單請求的回應函式，
 * 通常是 `emptySaves`），這樣依序消耗的回應佇列與請求次數不會被存檔清單的請求打亂，並以 `catalogRequests()` 取得「選劇本」的請求；沒給 `saves` 時，所有請求都交給 `respond`。 */
function setupEntry({ state = STATE, respond = () => Promise.reject(new Error('unexpected request')), saves = null, tags = noTags } = {}) {
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
    if (request.url === TAGS_URL) { return tags(request); }                   // 掛載時也載入常用標籤；沒有特別要測它的測試一律給空清單
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
  ['js/auth/auth-common.js', 'js/ui/restriction.js', 'js/game/game-labels.js', 'js/game/mode-prompt.js', 'js/game/catalog-state.js', 'js/game/game-api.js'].forEach((file) => run('app/static/' + file));
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
    { method: 'get', url: CATALOG_URL, params: {} },                  // 目錄第一頁：沒有條件、沒有游標（也不再送 offset）
    { method: 'get', url: TAGS_URL },                                 // 常用標籤
    { method: 'get', url: SAVES_URL, params: { offset: 0 } },
  ]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);
  assert.equal(page.catalog.loaded, true);
  assert.equal(page.catalog.loading, false);
  assert.equal(page.saves.loaded, true);                            // 存檔清單提前載入（空的）
  assert.equal(page.saves.items.length, 0);
  assert.equal(page.tab.value, 'scripts');                          // 沒有存檔：維持「選劇本」
});

t('the first page also carries the content mode summary shown as the safe mode state', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', forced: false } })) });
  await mount();
  assert.deepEqual(JSON.parse(JSON.stringify(page.contentMode.value)), { effective: 'safe', forced: false });
  assert.ok(page.modeNotice.value.text.includes('安全模式開') && page.modeNotice.value.link === '/account');
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
  assert.equal(requests.length, 3);                                 // 切換不再多送請求：兩個清單與常用標籤掛載時就載好了
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
  assert.deepEqual(requests.map((request) => request.url), [CATALOG_URL, TAGS_URL, SAVES_URL]);   // 請求都已送出，沒有誰等誰
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

t('load more appends the next page with the cursor the server gave, and the button state is restored', async () => {
  const pages = [catalogPage([card(ID_A), card(ID_B)], { has_more: true, next_cursor: 'c-1' }), catalogPage([card(ID_C)])];
  const { page, catalogRequests, mount } = setupEntry({ respond: () => Promise.resolve(pages.shift()), saves: emptySaves });
  await mount();
  assert.equal(page.catalog.hasMore, true);
  assert.equal(await page.loadCatalog(), true);
  assert.deepEqual(catalogRequests().map((r) => r.params), [{}, { cursor: 'c-1' }]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B, ID_C]);
  assert.equal(page.catalog.hasMore, false);                        // has_more 為假：不再顯示「載入更多」
  assert.equal(page.catalog.loading, false);
});

t('while a page is loading, pressing load more again sends nothing; afterwards it is enabled again', async () => {
  let release;
  const { page, catalogRequests, mount } = setupEntry({
    respond: () => new Promise((resolve) => { release = () => resolve(catalogPage([card(ID_A)], { has_more: true, next_cursor: 'c-1' })); }),
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
  const pages = [catalogPage([card(ID_A), card(ID_B)], { has_more: true, next_cursor: 'c-1' }), catalogPage([card(ID_B), card(ID_C)])];
  const { page, catalogRequests, mount } = setupEntry({ respond: () => Promise.resolve(pages.shift()), saves: emptySaves });
  await mount();
  await page.loadCatalog();
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B, ID_C]);
  assert.equal(page.catalog.nextCursor, '');                        // 游標由伺服器決定，這一頁沒有下一頁就清空
  assert.deepEqual(catalogRequests().map((r) => r.params.cursor), [undefined, 'c-1']);
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
      if (call === 1) { return Promise.resolve(catalogPage([card(ID_A)], { has_more: true, next_cursor: 'c-1' })); }
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
  assert.deepEqual(catalogRequests().map((r) => r.params.cursor), [undefined, 'c-1', 'c-1']);  // 失敗時游標沒有前進，重試的是同一頁
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
    respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', forced: false } })),
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

t('the three mode states are explained, every script stays visible, and nothing on the page can raise the mode by itself', async () => {
  const states = [
    [{ effective: 'safe', forced: false }, '安全模式開', '/account'],
    [{ effective: 'adult', forced: false }, '安全模式關', null],
    [{ effective: 'safe', forced: true }, '目前系統暫時只提供安全模式', null],
  ];
  for (const [mode, expected, link] of states) {
    const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A), card(ID_B)], { content_mode: mode })) });
    await mount();
    assert.ok(page.modeNotice.value.text.includes(expected), expected);
    assert.equal(page.modeNotice.value.link, link, expected);
    assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);                      // 所有劇本都看得到，沒有因模式被隱藏
    for (const name of ['setMode', 'changeMode', 'raiseMode', 'saveMode']) { assert.ok(!(name in page), name); }
  }
});

t('no age-based explanation and no old mode names reach the page text', async () => {
  const { page, mount } = setupEntry({ respond: () => Promise.resolve(catalogPage([card(ID_A)], { content_mode: { effective: 'safe', forced: false } })) });
  await mount();
  const text = JSON.stringify([page.modeNotice.value, page.modePrompt]);
  for (const forbidden of ['未滿 18', '部分劇本不會顯示', '一般模式', '成人模式']) { assert.ok(!text.includes(forbidden), forbidden); }
  assert.equal(page.tierLabel('adult'), '');                                 // 未綁定的卡片沒有任何標籤
  assert.equal(page.tierLabel('safe'), '綁定安全模式');
  assert.equal(page.modeLabel('adult'), '安全模式關');
});

// ---- 點選未綁定劇本時的詢問 -----------------------------------------------------------------------------

const unbound = (id) => ({ ...card(id), content_tier: 'adult' });
const safeOn = { effective: 'safe', forced: false };

async function mountWith(cards, mode) {
  const setup = setupEntry({ respond: () => Promise.resolve(catalogPage(cards, mode === undefined ? {} : { content_mode: mode })), saves: emptySaves });
  await setup.mount();
  return setup;
}

t('safe mode on and an unbound script: start asks first and goes nowhere yet', async () => {
  const { page, navigations } = await mountWith([unbound(ID_A)], safeOn);
  assert.equal(page.start(page.catalog.items[0]), false);
  assert.deepEqual(navigations, []);
  assert.equal(page.askOpen.value, true);
  assert.equal(page.askCard.value.id, ID_A);
});

t('go to settings leads to the account page and returns to this script wizard', async () => {
  const { page, navigations } = await mountWith([unbound(ID_A)], safeOn);
  page.start(page.catalog.items[0]);
  assert.equal(page.goToSettings(), true);
  assert.deepEqual(navigations, [`/account?return=${encodeURIComponent('/game/start/' + ID_A)}`]);
  assert.equal(page.askOpen.value, false);
});

t('keeping the safe mode continues into the wizard and changes nothing else', async () => {
  const { page, navigations, requests } = await mountWith([unbound(ID_A)], safeOn);
  const before = requests.length;
  page.start(page.catalog.items[0]);
  assert.equal(page.keepSafe(), true);
  assert.deepEqual(navigations, [`/game/start/${ID_A}`]);
  assert.equal(page.askOpen.value, false);
  assert.equal(requests.length, before);                                      // 沒有送出任何請求（模式設定不變）
});

t('cancel, Esc and the overlay (the modal sets its model to false) stay on the catalog without going anywhere', async () => {
  const { page, navigations } = await mountWith([unbound(ID_A)], safeOn);
  page.start(page.catalog.items[0]);
  page.cancelAsk();
  assert.equal(page.askOpen.value, false);
  page.start(page.catalog.items[0]);
  page.askOpen.value = false;                                                 // ui-modal 在 Esc 與點遮罩時送出 update:modelValue(false)
  assert.equal(page.askCard.value, null);
  assert.deepEqual(navigations, []);
  assert.equal(page.goToSettings(), false);                                   // 沒有等待中的詢問：不導向
  assert.equal(page.keepSafe(), false);
  assert.deepEqual(navigations, []);
});

t('a script bound to the safe mode starts at once, with no question', async () => {
  const { page, navigations } = await mountWith([card(ID_A)], safeOn);
  assert.equal(page.start(page.catalog.items[0]), true);
  assert.deepEqual(navigations, [`/game/start/${ID_A}`]);
  assert.equal(page.askOpen.value, false);
});

t('a player who already turned the safe mode off starts at once, and so does everyone while the emergency brake is on', async () => {
  for (const mode of [{ effective: 'adult', forced: false }, { effective: 'safe', forced: true }, { effective: 'adult', forced: true }]) {
    const { page, navigations } = await mountWith([unbound(ID_A)], mode);
    assert.equal(page.start(page.catalog.items[0]), true, JSON.stringify(mode));
    assert.deepEqual(navigations, [`/game/start/${ID_A}`]);
    assert.equal(page.askOpen.value, false);
  }
});

t('without a mode summary or with an odd card value nothing is asked (the server decides the real mode)', async () => {
  const missing = await mountWith([unbound(ID_A)], null);
  assert.equal(missing.page.start(missing.page.catalog.items[0]), true);
  for (const tier of ['standard', 'mystery', undefined, 7]) {
    const odd = await mountWith([{ ...card(ID_A), content_tier: tier }], safeOn);
    assert.equal(odd.page.start(odd.page.catalog.items[0]), true, String(tier));
    assert.equal(odd.page.askOpen.value, false);
  }
});

t('with a non-empty gate the question never opens either', async () => {
  const { page, navigations, mount } = setupEntry({
    state: { user: { member_tier: 'free', story_preference: 'all' }, gate: 'age_confirmation_required' },
    respond: () => Promise.resolve(catalogPage([unbound(ID_A)], { content_mode: safeOn })), saves: emptySaves,
  });
  await mount();
  assert.equal(page.start(page.catalog.items[0]), false);
  assert.equal(page.askOpen.value, false);
  assert.deepEqual(navigations, []);
});

t('a second start while the question is open replaces the card instead of stacking questions', async () => {
  const { page } = await mountWith([unbound(ID_A), unbound(ID_B)], safeOn);
  page.start(page.catalog.items[0]);
  page.start(page.catalog.items[1]);
  assert.equal(page.askCard.value.id, ID_B);
});

t('the ask modal is driven by Vue state only: no Bootstrap script, no v-html, three answers in the actions slot', () => {
  const html = read('app/templates/game/index.html');
  const partial = read('app/templates/game/_mode_prompt.html');                 // Modal 已抽成與詳情頁共用的片段，入口頁以 include 引入
  assert.ok(html.includes('{% include "game/_mode_prompt.html" %}'));
  const modal = partial.slice(partial.indexOf('<ui-modal v-model="askOpen"'), partial.indexOf('</ui-modal>'));
  assert.ok(modal.length > 200, '找不到詢問 Modal');
  for (const needle of ['@click="cancelAsk"', '@click="keepSafe"', '@click="goToSettings"', 'modePrompt.goSettings', 'modePrompt.keepSafe', 'modePrompt.cancel', ':status-code="modePrompt.code"']) {
    assert.ok(modal.includes(needle), needle);
  }
  assert.ok(!/data-bs-|v-html/.test(html + partial));
  assert.match(html, /<span v-if="tierLabel\(card\.content_tier\)" class="ge-chip">/);        // 沒有綁定的卡片不顯示標籤
});

// ---- 目錄的搜尋與篩選（rpg-script-catalog 規格「目錄畫面的搜尋與篩選」）-----------------------------------------

/* 依序消耗 `answers` 的目錄替身（沒有了就回空頁）。 */
const answering = (answers) => () => Promise.resolve(answers.length ? answers.shift() : catalogPage([]));

t('submitting a search clears the list and loads the first page with the words, and the box shows the tidied text', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([catalogPage([card(ID_A)], { has_more: true, next_cursor: 'c-1' }), catalogPage([card(ID_B)])]), saves: emptySaves });
  await mount();
  page.searchText.value = '  青雲   仙俠 ';
  const loading = page.submitSearch();
  assert.deepEqual(ids(page.catalog), []);                                  // 條件一改就清空（回應還沒到）
  assert.equal(page.catalog.nextCursor, '');
  await loading;
  assert.deepEqual(catalogRequests().map((r) => r.params), [{}, { q: '青雲 仙俠' }]);   // 第一頁：沒有游標
  assert.equal(page.searchText.value, '青雲 仙俠');
  assert.equal(page.catalog.q, '青雲 仙俠');
  assert.deepEqual(ids(page.catalog), [ID_B]);
  assert.equal(page.hasFilters.value, true);
});

t('typing alone changes nothing: the words only count once they are submitted', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([catalogPage([card(ID_A)])]), saves: emptySaves });
  await mount();
  page.searchText.value = '還在打字';
  await tick();
  assert.equal(catalogRequests().length, 1);
  assert.equal(page.catalog.q, '');
  assert.equal(page.hasFilters.value, false);
  assert.deepEqual(ids(page.catalog), [ID_A]);
});

t('submitting an empty box after a search takes the words off and reloads from the first page', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([catalogPage([card(ID_A)]), catalogPage([]), catalogPage([card(ID_A)])]), saves: emptySaves });
  await mount();
  page.searchText.value = '青雲';
  await page.submitSearch();
  page.searchText.value = '   ';
  await page.submitSearch();
  assert.deepEqual(catalogRequests().map((r) => r.params), [{}, { q: '青雲' }, {}]);
  assert.equal(page.catalog.q, '');
  assert.equal(page.hasFilters.value, false);
});

t('a tag chip applies the tag, shows as selected, and a second press takes it off; a tag on a card works the same way', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([]), saves: emptySaves });
  await mount();
  await page.chooseTag('仙俠');
  assert.equal(page.isTagOn('仙俠'), true);
  assert.equal(page.isTagOn('SCIFI'), false);
  assert.equal(page.isTagOn('科幻'), false);
  await page.chooseTag('仙俠');
  assert.equal(page.isTagOn('仙俠'), false);
  await page.chooseCardTag('SciFi');                                        // 卡片上的標籤：保留作者寫的大小寫送給伺服器（伺服器不分大小寫）
  assert.equal(page.isTagOn('scifi'), true);
  assert.deepEqual(catalogRequests().map((r) => r.params), [{}, { tag: '仙俠' }, {}, { tag: 'SciFi' }]);
});

t('the channel select applies one of the three channels, and "all channels" takes the filter off', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([]), saves: emptySaves });
  await mount();
  assert.deepEqual(plain(page.audienceOptions), [
    { value: '', label: '全部頻道' }, { value: 'male', label: '男頻' }, { value: 'female', label: '女頻' }, { value: 'all', label: '全讀者' },
  ]);
  await page.chooseAudience('female');
  await page.chooseAudience('all');                                         // 「全讀者」是一個頻道篩選，不是沒有篩選
  await page.chooseAudience('');
  await page.chooseAudience('other');                                       // 不認得的值當成沒有篩選，不會送給伺服器
  assert.deepEqual(catalogRequests().map((r) => r.params), [{}, { audience: 'female' }, { audience: 'all' }, {}, {}]);
});

t('the three conditions combine, and load more repeats all of them with the cursor', async () => {
  const { page, catalogRequests, mount } = setupEntry({
    respond: answering([catalogPage([]), catalogPage([]), catalogPage([]), catalogPage([card(ID_A)], { has_more: true, next_cursor: 'c-9' }), catalogPage([card(ID_B)])]),
    saves: emptySaves,
  });
  await mount();
  page.searchText.value = '青雲';
  await page.chooseAudience('male');
  await page.chooseTag('仙俠');
  await page.submitSearch();
  assert.equal(page.catalog.hasMore, true);
  await page.loadCatalog();
  const last = catalogRequests().slice(-2).map((r) => r.params);
  assert.deepEqual(last, [{ q: '青雲', tag: '仙俠', audience: 'male' }, { q: '青雲', tag: '仙俠', audience: 'male', cursor: 'c-9' }]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);
});

t('clear filters empties the box and every condition and loads the unfiltered first page', async () => {
  const { page, catalogRequests, mount } = setupEntry({ respond: answering([]), saves: emptySaves });
  await mount();
  page.searchText.value = '青雲';
  await page.submitSearch();
  await page.chooseTag('仙俠');
  await page.chooseAudience('female');
  assert.equal(page.hasFilters.value, true);
  await page.clearFilters();
  assert.equal(page.searchText.value, '');
  assert.deepEqual([page.catalog.q, page.catalog.tag, page.catalog.audience], ['', '', '']);
  assert.equal(page.hasFilters.value, false);
  assert.deepEqual(catalogRequests().pop().params, {});
});

t('a slow answer to the first of two quick searches never covers the newer result', async () => {
  const first = gate();
  let calls = 0;
  const { page, mount } = setupEntry({
    respond: () => {
      calls += 1;
      if (calls === 1) { return Promise.resolve(catalogPage([])); }                                   // 掛載時的第一頁
      return calls === 2 ? first.promise : Promise.resolve(catalogPage([card(ID_B)]));
    },
    saves: emptySaves,
  });
  await mount();
  page.searchText.value = '甲';
  const older = page.submitSearch();
  page.searchText.value = '乙';
  const newer = page.submitSearch();
  await newer;
  assert.deepEqual(ids(page.catalog), [ID_B]);
  first.resolve(catalogPage([card(ID_A), card(ID_C)], { has_more: true, next_cursor: 'stale' }));
  await older;
  assert.deepEqual(ids(page.catalog), [ID_B]);                              // 較晚抵達的舊結果被丟掉
  assert.equal(page.catalog.hasMore, false);
  assert.equal(page.catalog.q, '乙');
  assert.equal(page.catalog.loading, false);
});

t('with a filter on and nothing found the page says so and offers to clear; with no filter it says there is nothing to play; neither is an error', async () => {
  const none = setupEntry({ respond: answering([catalogPage([])]), saves: emptySaves });
  await none.mount();
  assert.equal(none.page.emptyKind.value, 'none');
  none.page.searchText.value = '找不到的字';
  await none.page.submitSearch();
  assert.equal(none.page.emptyKind.value, 'filtered');
  assert.equal(none.page.catalog.error, '');
  await none.page.clearFilters();
  assert.equal(none.page.emptyKind.value, 'none');
});

t('a filtered page that fails keeps what was loaded, shows the error, and retry repeats the same conditions and cursor', async () => {
  let call = 0;
  const { page, catalogRequests, mount } = setupEntry({
    respond: () => {
      call += 1;
      if (call === 1) { return Promise.resolve(catalogPage([])); }
      if (call === 2) { return Promise.resolve(catalogPage([card(ID_A)], { has_more: true, next_cursor: 'c-2' })); }
      if (call === 3) { return Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')); }
      return Promise.resolve(catalogPage([card(ID_B)]));
    },
    saves: emptySaves,
  });
  await mount();
  await page.chooseTag('仙俠');
  assert.equal(await page.loadCatalog(), false);
  assert.deepEqual(ids(page.catalog), [ID_A]);
  assert.equal(page.catalog.error, '系統發生錯誤，請稍後再試。');
  assert.equal(page.emptyKind.value, '');
  assert.equal(await page.loadCatalog(), true);
  assert.deepEqual(catalogRequests().slice(-2).map((r) => r.params), [{ tag: '仙俠', cursor: 'c-2' }, { tag: '仙俠', cursor: 'c-2' }]);
  assert.deepEqual(ids(page.catalog), [ID_A, ID_B]);
});

t('an expired session during a search sends the player to the login page', async () => {
  let call = 0;
  const { page, navigations, mount } = setupEntry({
    respond: () => { call += 1; return call === 1 ? Promise.resolve(catalogPage([card(ID_A)])) : Promise.reject(serverError(401, 'unauthorized', '請先登入。')); },
    saves: emptySaves,
  });
  await mount();
  page.searchText.value = '青雲';
  await page.submitSearch();
  assert.deepEqual(navigations, ['/login?next=%2Fgame']);
});

// ---- 常用標籤晶片 ---------------------------------------------------------------------------------

t('the popular tags load with the page and become chips; the search box limit is the one the server enforces', async () => {
  const { page, requests, mount } = setupEntry({
    respond: answering([catalogPage([card(ID_A)])]), saves: emptySaves,
    tags: () => Promise.resolve({ data: { ok: true, tags: [{ tag: '仙俠', count: 3 }, { tag: '科幻', count: 1 }] } }),
  });
  await mount();
  assert.deepEqual(plain(page.tagChips.value), [{ tag: '仙俠' }, { tag: '科幻' }]);
  assert.equal(requests.filter((r) => r.url === TAGS_URL).length, 1);
  assert.equal(page.qMax, 50);
});

t('a tag picked on a card that is not among the popular ones still shows as a selected chip, so it can be taken off', async () => {
  const { page, mount } = setupEntry({
    respond: answering([]), saves: emptySaves,
    tags: () => Promise.resolve({ data: { ok: true, tags: [{ tag: '仙俠', count: 3 }] } }),
  });
  await mount();
  await page.chooseCardTag('冷門標籤');
  assert.deepEqual(plain(page.tagChips.value), [{ tag: '冷門標籤' }, { tag: '仙俠' }]);
  assert.equal(page.isTagOn('冷門標籤'), true);
  await page.chooseTag('冷門標籤');
  assert.deepEqual(plain(page.tagChips.value), [{ tag: '仙俠' }]);
  await page.chooseTag('仙俠');                                             // 已在常用標籤裡的不會重複出現
  assert.deepEqual(plain(page.tagChips.value), [{ tag: '仙俠' }]);
});

t('when the popular tags cannot be read there are simply no chips, and the catalog, the search and everything else still work', async () => {
  const { page, mount } = setupEntry({
    respond: answering([catalogPage([card(ID_A)]), catalogPage([card(ID_B)])]), saves: emptySaves,
    tags: () => Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')),
  });
  await mount();
  assert.deepEqual(plain(page.tagChips.value), []);
  assert.deepEqual(ids(page.catalog), [ID_A]);
  assert.equal(page.catalog.error, '');
  assert.equal(page.restriction.value, null);
  page.searchText.value = '青雲';
  await page.submitSearch();
  assert.deepEqual(ids(page.catalog), [ID_B]);
});

t('every card links its title to the detail page by its version id', () => {
  const { page } = setupEntry();
  assert.equal(page.detailPath(ID_A), `/game/script/${ID_A}`);
  assert.equal(page.detailPath('a/b'), '/game/script/a%2Fb');
});

t('searching and filtering write nothing to the browser: neither the page code nor the catalog state reaches for storage', () => {
  for (const file of ['app/static/js/game/entry.js', 'app/static/js/game/catalog-state.js']) {
    const code = read(file).replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
    assert.ok(!/\b(localStorage|sessionStorage|indexedDB|document\.cookie)\b/.test(code), file);
  }
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
  assert.deepEqual(requests.map((r) => r.url), ['/api/sessions/catalog', TAGS_URL, '/api/sessions']);
  assert.deepEqual(requests[2].params, { offset: 0 });
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
const LOOP_VARS = new Set(['card', 'save', 'tag', 'index', 'line', 'chip', 'option', '$event']);   // v-for 的迴圈變數與事件物件

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
