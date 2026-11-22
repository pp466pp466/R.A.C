'use strict';
/* 劇本詳情頁（script-detail.js；見 rpg-script-catalog 規格「詳情頁」與 rpg-entry-pages 規格「點選未綁定劇本時詢問是否修改安全模式」）：
 * 以真的 Vue、真的 Auth、真的 game-labels.js、mode-prompt.js 與 game-api.js 在 `vm` 沙盒執行 `setup()`，只把 axios、location 與 mount 換成替身。
 * 驗證識別碼只取自網址、載入與 404／失敗／重試、401 與受限狀態、整理過的詳情（只有白名單欄位與正確的型別）、「開始冒險」與共用的詢問流程，
 * 檢舉（rpg-script-reports）：入口狀態、Modal 的開關與驗證、送出的請求與每種失敗的處理、送出中的保護。
 * 最後檢查樣板使用的每個名稱都由 setup() 回傳。沒有瀏覽器可以掛載；樣板的文字與結構由 tests/test_game_pages.py 檢查。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const ID = '11111111-1111-4111-8111-111111111111';
const STATE = { user: { member_tier: 'free' }, gate: null };
const safeOn = { effective: 'safe', forced: false };

function serverError(status, code, message) {
  return { response: { status, data: { error: { code, message } } } };
}

const detailBody = (extra = {}) => ({
  ok: true, id: ID, title: '青雲問道', summary: '一個關於修行的故事', world_name: '青雲界', tags: ['仙俠', '成長'], author_name: '小說家', author_code: 'AB12',
  content_tier: 'adult', audience: 'female', version_no: 2, published_at: '2026-11-18T04:30:00Z', background: '青雲山下，少年初入宗門。',
  characters: [{ name: '林初', occupation: '外門弟子' }, { name: '白師姐', occupation: null }], locations: ['青雲山', '藏經閣'],
  has_custom_ending: true, content_mode: safeOn, ...extra,
});
/* 詳情端點的回應是 { ok: true, script: { …詳情… } }（不是把欄位放在最外層）。 */
const envelope = (script) => ({ ok: true, script });
const ok = (script = detailBody()) => () => Promise.resolve({ data: envelope(script) });

/* 載入詳情頁需要的腳本並回傳 setup() 的結果。`respond(request)` 是 axios 的替身；`pathname` 是頁面的網址路徑。 */
function setupDetail({ state = STATE, respond = ok(), pathname = `/game/script/${ID}` } = {}) {
  const requests = [];
  const navigations = [];
  const mounted = [];
  const toasts = [];
  const sandbox = { console, Promise, setTimeout, clearTimeout };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname, search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state' ? { textContent: typeof state === 'string' ? state : JSON.stringify(state) } : null),
  };
  const axios = (request) => {
    requests.push(JSON.parse(JSON.stringify(request)));
    return respond(request);
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
  ['js/auth/auth-common.js', 'js/ui/restriction.js', 'js/game/game-labels.js', 'js/game/mode-prompt.js', 'js/game/script-report.js', 'js/game/game-api.js'].forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, { install: () => {}, toast: (message) => { toasts.push(message); } });
  run('app/static/js/game/script-detail.js');
  assert.ok(options && typeof options.setup === 'function', 'script-detail.js 沒有呼叫 createApp');
  const page = options.setup();
  return { page, requests, navigations, toasts, mount: () => Promise.all(mounted.map((hook) => hook())) };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));

// ---- 識別碼與載入 -----------------------------------------------------------------------------------

t('mounting reads the version id from the address (never from the shell) and loads the detail once, with nothing else', async () => {
  const { page, requests, mount } = setupDetail();
  assert.equal(requests.length, 0);                                                         // setup() 本身不送請求
  assert.equal(page.phase.value, 'loading');
  await mount();
  assert.deepEqual(requests, [{ method: 'get', url: `/api/sessions/catalog/${ID}`, params: undefined }].map((r) => ({ method: r.method, url: r.url })));
  assert.equal(page.phase.value, 'ready');
  assert.equal(page.detail.value.title, '青雲問道');
});

t('the id is the segment after "script" in the address, decoded; an address with no id is "not found" without any request', async () => {
  const encoded = setupDetail({ pathname: '/game/script/a%2Fb' });
  await encoded.mount();
  assert.equal(encoded.requests[0].url, '/api/sessions/catalog/a%2Fb');                      // 解碼後再編碼：路徑段落不會被竄改
  for (const pathname of ['/game/script/', '/', '', '/game/script/%E0%A4%A']) {
    const none = setupDetail({ pathname });
    await none.mount();
    assert.equal(none.page.phase.value, 'notfound', JSON.stringify(pathname));
    assert.equal(none.requests.length, 0, JSON.stringify(pathname));
  }
});

t('the loaded detail is tidied: only the public fields, the right types, characters with a name and an occupation and nothing else', async () => {
  const hostile = detailBody({
    summary: 7, world_name: null, tags: ['仙俠', 3, '', null, '成長'], author_code: undefined, version_no: 'two', background: { x: 1 },
    characters: [{ name: '林初', occupation: '弟子', hidden_trait: '不該出現', personality: '不該出現', age: 17 }, null, 'text', { name: '', occupation: '' }, { name: 7 }],
    locations: ['青雲山', 5, '', null], has_custom_ending: 'yes', hidden_trait: '不該出現', milestones: [{ name: '不該出現' }], ending_text: '不該出現', rules: '不該出現',
  });
  const { page, mount } = setupDetail({ respond: ok(hostile) });
  await mount();
  assert.deepEqual(plain(page.detail.value), {
    id: ID, title: '青雲問道', summary: '', world_name: '', tags: ['仙俠', '成長'], author_name: '小說家', author_code: '', content_tier: 'adult', audience: 'female',
    version_no: 0, published_at: '2026-11-18T04:30:00Z', background: '', characters: [{ name: '林初', occupation: '弟子' }], locations: ['青雲山'], has_custom_ending: false,
    report_state: 'unavailable',                                                              // 回應沒有（或不認得）的值一律不顯示入口
  });
  assert.ok(!JSON.stringify(page.detail.value).includes('不該出現'));
});

t('the author text stays a plain string, so the page shows markup as text and never parses it', async () => {
  const { page, mount } = setupDetail({ respond: ok(detailBody({ title: '<b>粗體</b>', background: '<script>alert(1)</script>', characters: [{ name: '<i>林初</i>', occupation: '<u>弟子</u>' }] })) });
  await mount();
  assert.equal(page.detail.value.title, '<b>粗體</b>');
  assert.equal(page.detail.value.background, '<script>alert(1)</script>');
  assert.equal(page.detail.value.characters[0].name, '<i>林初</i>');
});

t('the member text, the published time (Taiwan time) and the labels come from the shared tables', async () => {
  const { page, mount } = setupDetail({ state: { user: { member_tier: 'vip' }, gate: null } });
  await mount();
  assert.equal(page.memberText.value, '尊榮');
  assert.equal(page.publishedText.value, '2026-11-18 12:30');
  assert.equal(page.tierLabel('safe'), '綁定安全模式');
  assert.equal(page.tierLabel('adult'), '');
  assert.equal(page.audienceLabel('female'), '女頻');
});

// ---- 找不到、失敗與重試 -----------------------------------------------------------------------------

t('a 404 (taken down, someone else\'s draft, or never existed) shows "not found" with no error text and no way to start', async () => {
  const { page, mount, navigations } = setupDetail({ respond: () => Promise.reject(serverError(404, 'not_found', '找不到。')) });
  await mount();
  assert.equal(page.phase.value, 'notfound');
  assert.equal(page.detail.value, null);
  assert.equal(page.failureText.value, '');
  assert.equal(page.canStart.value, false);
  assert.equal(page.start(), false);
  assert.deepEqual(navigations, []);
});

t('any other failure says so in Traditional Chinese and retry loads again; success replaces the failure', async () => {
  let call = 0;
  const { page, requests, mount } = setupDetail({
    respond: () => {
      call += 1;
      return call === 1 ? Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')) : Promise.resolve({ data: envelope(detailBody()) });
    },
  });
  await mount();
  assert.equal(page.phase.value, 'failed');
  assert.equal(page.failureText.value, '系統發生錯誤，請稍後再試。');
  assert.equal(page.canStart.value, false);
  assert.equal(await page.loadDetail(), true);
  assert.equal(page.phase.value, 'ready');
  assert.equal(page.failureText.value, '');
  assert.equal(requests.length, 2);
});

t('a network failure and a timeout each give a message, and a failure without a message falls back to a fixed one', async () => {
  for (const failure of [new Error('Network Error'), { code: 'ECONNABORTED', message: 'timeout of 1000ms exceeded' }, { response: { status: 503, data: {} } }]) {
    const { page, mount } = setupDetail({ respond: () => Promise.reject(failure) });
    await mount();
    assert.equal(page.phase.value, 'failed');
    assert.ok(/[一-鿿]/.test(page.failureText.value), JSON.stringify(failure));
  }
});

t('while the detail is loading, pressing retry again sends nothing; afterwards it works again', async () => {
  let release;
  let calls = 0;
  const { page, mount } = setupDetail({ respond: () => { calls += 1; return new Promise((resolve) => { release = () => resolve({ data: envelope(detailBody()) }); }); } });
  const first = mount();
  await tick();
  assert.equal(await page.loadDetail(), false);
  assert.equal(calls, 1);
  release();
  await first;
  assert.equal(page.phase.value, 'ready');
});

t('a success body that makes no sense is a retryable failure, never a blank page', async () => {
  for (const body of [null, undefined, {}, { ok: true }, { ok: true, script: null }, { ok: true, script: {} }, { ok: true, script: { id: ID } }, { ok: true, script: { title: '沒有編號' } },
    { ok: true, script: { id: 7, title: 'x' } }, { ok: true, script: { id: '', title: 'x' } }, { ok: true, script: [] }, 'text',
    detailBody()]) {                                                                           // 最後一個：欄位放在最外層（沒有 script 包裝）也看不懂
    const { page, mount } = setupDetail({ respond: () => Promise.resolve({ data: body }) });      // 不經 ok()：它對 undefined 會套用預設的正常詳情
    await mount();
    assert.equal(page.phase.value, 'failed', JSON.stringify(body));
    assert.ok(page.failureText.value.length > 0);
    assert.equal(page.detail.value, null);
  }
});

t('an expired session sends the player to the login page and keeps the position, once', async () => {
  const { navigations, mount, page } = setupDetail({ respond: () => Promise.reject(serverError(401, 'unauthorized', '請先登入。')) });
  await mount();
  await page.loadDetail();
  assert.deepEqual(navigations, [`/login?next=%2Fgame%2Fscript%2F${ID}`]);
});

t('a locked account gets the appeal shortcut instead of a blank page', async () => {
  const { page, mount } = setupDetail({ respond: () => Promise.reject(serverError(403, 'account_locked', '您的帳號已被鎖定。')) });
  await mount();
  assert.equal(page.restriction.value, 'locked');
  assert.equal(page.phase.value, 'failed');
});

// ---- 開始冒險與安全模式詢問 ---------------------------------------------------------------------------

const mountDetail = async (body, state = STATE) => {
  const setup = setupDetail({ state, respond: ok(body) });
  await setup.mount();
  return setup;
};
const boundBody = (mode = safeOn) => detailBody({ content_tier: 'safe', content_mode: mode });
const unboundBody = (mode = safeOn) => detailBody({ content_tier: 'adult', content_mode: mode });

t('a player who turned the safe mode off starts at once, and the address comes from the detail the server sent', async () => {
  const { page, navigations } = await mountDetail(unboundBody({ effective: 'adult', forced: false }));
  assert.equal(page.start(), true);
  assert.deepEqual(navigations, [`/game/start/${ID}`]);
  assert.equal(page.askOpen.value, false);
});

t('safe mode on and an unbound script: start asks first and goes nowhere yet', async () => {
  const { page, navigations } = await mountDetail(unboundBody());
  assert.equal(page.start(), false);
  assert.deepEqual(navigations, []);
  assert.equal(page.askOpen.value, true);
  assert.equal(page.askCard.value.id, ID);
});

t('go to settings leads to the account page and comes back to this script wizard', async () => {
  const { page, navigations } = await mountDetail(unboundBody());
  page.start();
  assert.equal(page.goToSettings(), true);
  assert.deepEqual(navigations, [`/account?return=${encodeURIComponent('/game/start/' + ID)}`]);
  assert.equal(page.askOpen.value, false);
});

t('keeping the safe mode continues into the wizard and sends no request', async () => {
  const { page, navigations, requests } = await mountDetail(unboundBody());
  const before = requests.length;
  page.start();
  assert.equal(page.keepSafe(), true);
  assert.deepEqual(navigations, [`/game/start/${ID}`]);
  assert.equal(requests.length, before);
});

t('cancel, Esc and the overlay stay on the page without going anywhere', async () => {
  const { page, navigations } = await mountDetail(unboundBody());
  page.start();
  page.cancelAsk();
  assert.equal(page.askOpen.value, false);
  page.start();
  page.askOpen.value = false;                                                               // ui-modal 在 Esc 與點遮罩時送出 update:modelValue(false)
  assert.equal(page.askCard.value, null);
  assert.equal(page.goToSettings(), false);
  assert.equal(page.keepSafe(), false);
  assert.deepEqual(navigations, []);
});

t('a script bound to the safe mode, the emergency brake, and a missing mode summary never ask', async () => {
  for (const body of [boundBody(), unboundBody({ effective: 'safe', forced: true }), unboundBody(null), detailBody({ content_tier: 'adult', content_mode: undefined })]) {
    const { page, navigations } = await mountDetail(body);
    assert.equal(page.start(), true, JSON.stringify(body.content_mode));
    assert.deepEqual(navigations, [`/game/start/${ID}`]);
    assert.equal(page.askOpen.value, false);
  }
});

t('with a non-empty gate the button is disabled, start goes nowhere, and no question opens', async () => {
  const { page, navigations } = await mountDetail(unboundBody(), { user: { member_tier: 'free' }, gate: 'age_confirmation_required' });
  assert.equal(page.canStart.value, false);
  assert.equal(page.start(), false);
  assert.equal(page.askOpen.value, false);
  assert.deepEqual(navigations, []);
  assert.ok(page.gateNotice.value.text.includes('年齡'));
});

t('before the detail has loaded there is nothing to start', () => {
  const { page, navigations } = setupDetail();
  assert.equal(page.canStart.value, false);
  assert.equal(page.start(), false);
  assert.deepEqual(navigations, []);
});

t('a damaged initial state does not break the page and leaves nothing disabled by guesswork', () => {
  for (const state of ['not json', '[]', 'null', '{"gate": 5, "user": null}', '{"gate": ""}']) {
    const { page } = setupDetail({ state });
    assert.equal(page.gate.value, null, state);
    assert.equal(page.memberText.value, '一般', state);
  }
});

t('opening the page sends one GET for the detail and nothing else, and the shared modal copy names no old mode', async () => {
  const { page, requests, mount } = setupDetail();
  await mount();
  page.start();
  assert.deepEqual(requests.map((r) => `${r.method} ${r.url}`), [`get /api/sessions/catalog/${ID}`]);
  const text = JSON.stringify(page.modePrompt);
  for (const forbidden of ['一般模式', '成人模式', '未滿 18']) { assert.ok(!text.includes(forbidden), forbidden); }
});

// ---- 檢舉（rpg-script-reports；script-reports 規格「劇本詳情頁提供檢舉入口」）-------------------------------------------------

const REPORT_URL = `/api/sessions/catalog/${ID}/report`;
const reportedBody = (state = 'available') => detailBody({ report_state: state });
/* GET 回詳情；POST（檢舉）交給 `post(request)`。 */
const withReport = (post, state = 'available') => (request) => (request.method === 'get' ? Promise.resolve({ data: envelope(reportedBody(state)) }) : post(request));
const created = () => Promise.resolve({ status: 201, data: { ok: true } });
const reportPage = async (post = created, state = 'available') => {
  const setup = setupDetail({ respond: withReport(post, state) });
  await setup.mount();
  return setup;
};
const posts = (requests) => requests.filter((r) => r.method !== 'get');
const fail = (status, code, message, fields) => Promise.reject({ response: { status, data: { error: { code, message, ...(fields ? { fields } : {}) } } } });

t('the entry follows report_state: available, reported, and anything else (built-in, own script, unknown) shows nothing', async () => {
  for (const [given, expected] of [['available', 'available'], ['reported', 'reported'], ['unavailable', 'unavailable'], ['AVAILABLE', 'unavailable'], [null, 'unavailable'], [7, 'unavailable'], ['', 'unavailable']]) {
    const { page } = await reportPage(created, given);
    assert.equal(page.reportState.value, expected, String(given));
  }
});

t('opening is only possible when the entry is available, and every opening starts with a clean form', async () => {
  for (const state of ['reported', 'unavailable']) {
    const closed = await reportPage(created, state);
    assert.equal(closed.page.openReport(), false, state);
    assert.equal(closed.page.reportOpen.value, false, state);
  }
  const { page } = await reportPage();
  assert.equal(page.openReport(), true);
  assert.equal(page.reportOpen.value, true);
  page.reportPicked.value = ['plagiarism'];
  page.reportText.value = '舊的文字';
  page.reportError.value = '舊的錯誤';
  assert.equal(page.closeReport(), true);
  assert.equal(page.reportOpen.value, false);
  page.openReport();
  assert.deepEqual(plain(page.reportPicked.value), []);
  assert.equal(page.reportText.value, '');
  assert.equal(page.reportError.value, '');
});

t('the confirm button needs at least one reason, and the text counts Unicode characters up to 200', async () => {
  const { page } = await reportPage();
  page.openReport();
  assert.equal(page.canSubmitReport.value, false);                                            // 沒有任何理由
  page.reportPicked.value = ['other'];
  assert.equal(page.canSubmitReport.value, true);
  page.reportText.value = '😀'.repeat(200);                                                   // 200 個字元（UTF-16 單元 400 個）
  assert.equal(page.reportCheck.value.length, 200);
  assert.equal(page.canSubmitReport.value, true);
  page.reportText.value = '😀'.repeat(201);
  assert.equal(page.reportCheck.value.length, 201);
  assert.equal(page.canSubmitReport.value, false);
  page.reportText.value = '字'.repeat(200) + ' \n\t ';                                         // 空白與控制字元整理掉之後仍是 200 字
  assert.equal(page.canSubmitReport.value, true);
});

t('submitting without a reason sends nothing and says why', async () => {
  const { page, requests } = await reportPage();
  page.openReport();
  assert.equal(await page.submitReport(), false);
  assert.equal(posts(requests).length, 0);
  assert.equal(page.reportError.value, '請至少選擇一個檢舉原因。');
});

t('a successful report posts the reasons in the fixed order with the cleaned text, closes, toasts and turns the entry into "reported"', async () => {
  const { page, requests, toasts } = await reportPage();
  page.openReport();
  page.reportPicked.value = ['plagiarism', 'minor_sexual', 'other'];                        // 勾選的先後不影響順序
  page.reportText.value = '  抄襲\n某部作品  ';
  const result = page.submitReport();
  assert.equal(page.submitting.value, true);
  assert.equal(await result, true);
  assert.deepEqual(posts(requests).map((r) => ({ method: r.method, url: r.url, data: r.data })), [
    { method: 'post', url: REPORT_URL, data: { reasons: ['minor_sexual', 'plagiarism', 'other'], text: '抄襲某部作品' } },
  ]);
  assert.equal(page.submitting.value, false);
  assert.equal(page.reportOpen.value, false);
  assert.equal(page.reportState.value, 'reported');
  assert.deepEqual(toasts, ['已送出檢舉，謝謝您的回報。']);
});

t('an empty text is not sent at all', async () => {
  const { page, requests } = await reportPage();
  page.openReport();
  page.reportPicked.value = ['tier_mismatch'];
  page.reportText.value = '   ';
  await page.submitReport();
  assert.deepEqual(posts(requests)[0].data, { reasons: ['tier_mismatch'] });
});

t('the id sent is the one the server returned in the detail, not one rebuilt from the address', async () => {
  const other = '22222222-2222-4222-8222-222222222222';
  const setup = setupDetail({ respond: (request) => (request.method === 'get' ? Promise.resolve({ data: envelope(detailBody({ id: other, report_state: 'available' })) }) : created()) });
  await setup.mount();
  setup.page.openReport();
  setup.page.reportPicked.value = ['other'];
  await setup.page.submitReport();
  assert.equal(posts(setup.requests)[0].url, `/api/sessions/catalog/${other}/report`);
});

t('a second press while the request is in flight sends nothing, and the modal cannot be closed until it ends', async () => {
  let release = null;
  const { page, requests } = await reportPage(() => new Promise((resolve) => { release = () => resolve({ status: 201, data: { ok: true } }); }));
  page.openReport();
  page.reportPicked.value = ['other'];
  const first = page.submitReport();
  assert.equal(page.canSubmitReport.value, false);
  assert.equal(await page.submitReport(), false);
  assert.equal(posts(requests).length, 1);
  assert.equal(page.closeReport(), false);
  page.reportOpen.value = false;                                                              // Esc 與遮罩
  assert.equal(page.reportOpen.value, true);
  release();
  assert.equal(await first, true);
  assert.equal(page.submitting.value, false);
  assert.equal(page.reportOpen.value, false);
});

t('Esc and the overlay close an idle modal without any request', async () => {
  const { page, requests } = await reportPage();
  page.openReport();
  page.reportOpen.value = false;
  assert.equal(page.reportOpen.value, false);
  assert.equal(posts(requests).length, 0);
  assert.equal(page.reportState.value, 'available');
});

t('409 already_reported closes the modal and shows "reported" with a toast saying so', async () => {
  const { page, toasts } = await reportPage(() => fail(409, 'already_reported', '您已經檢舉過這個劇本版本了。'));
  page.openReport();
  page.reportPicked.value = ['other'];
  assert.equal(await page.submitReport(), false);
  assert.equal(page.reportState.value, 'reported');
  assert.equal(page.reportOpen.value, false);
  assert.equal(page.submitting.value, false);
  assert.deepEqual(toasts, ['您已經檢舉過這個版本了。']);
});

t('429 keeps the modal open with the daily-limit explanation, and the button is usable again', async () => {
  const { page } = await reportPage(() => fail(429, 'report_limit', '您今日的檢舉次數已用完，將於明日 00:00（台灣時間）重置。'));
  page.openReport();
  page.reportPicked.value = ['other'];
  assert.equal(await page.submitReport(), false);
  assert.equal(page.reportOpen.value, true);
  assert.equal(page.reportState.value, 'available');
  assert.equal(page.submitting.value, false);
  assert.match(page.reportError.value, /今天的檢舉次數已用完/);
  assert.equal(page.canSubmitReport.value, true);
});

t('404 says it was not found and hides the entry; so does 403 report_own_script', async () => {
  for (const [status, code, expected] of [[404, 'not_found', /找不到這個劇本/], [403, 'report_own_script', /不能檢舉自己的劇本/]]) {
    const { page } = await reportPage(() => fail(status, code, '伺服器的訊息不會被直接顯示'));
    page.openReport();
    page.reportPicked.value = ['other'];
    assert.equal(await page.submitReport(), false);
    assert.match(page.reportError.value, expected, code);
    assert.ok(!page.reportError.value.includes('伺服器的訊息'), code);
    assert.equal(page.reportState.value, 'unavailable', code);
    assert.equal(page.canSubmitReport.value, false, code);                                    // 這個版本不可再檢舉，不能再送
    assert.equal(page.submitting.value, false, code);
  }
});

t('a 400 shows the field message the server named and stays open', async () => {
  const { page } = await reportPage(() => fail(400, 'validation_error', '輸入有誤。', { text: '檢舉說明至多 200 字。' }));
  page.openReport();
  page.reportPicked.value = ['other'];
  await page.submitReport();
  assert.equal(page.reportError.value, '檢舉說明至多 200 字。');
  assert.equal(page.reportOpen.value, true);
  assert.equal(page.reportState.value, 'available');
});

t('a network failure says so, restores the button and keeps what the player typed', async () => {
  const { page } = await reportPage(() => Promise.reject({ code: 'ERR_NETWORK' }));
  page.openReport();
  page.reportPicked.value = ['other'];
  page.reportText.value = '我寫的說明';
  assert.equal(await page.submitReport(), false);
  assert.equal(page.reportError.value, '連線不穩，這次沒有送出，請稍後再試。');
  assert.equal(page.submitting.value, false);
  assert.equal(page.reportText.value, '我寫的說明');
  assert.equal(page.canSubmitReport.value, true);
});

t('an expired session sends the player to the login page once, with no error text left in the modal', async () => {
  const { page, navigations } = await reportPage(() => fail(401, 'unauthorized', '請先登入。'));
  page.openReport();
  page.reportPicked.value = ['other'];
  await page.submitReport();
  assert.deepEqual(navigations, [`/login?next=%2Fgame%2Fscript%2F${ID}`]);
  assert.equal(page.reportError.value, '');
  assert.equal(page.submitting.value, false);
});

t('a locked account closes the modal and gets the appeal shortcut', async () => {
  const { page } = await reportPage(() => fail(403, 'account_locked', '您的帳號已被鎖定。'));
  page.openReport();
  page.reportPicked.value = ['other'];
  await page.submitReport();
  assert.equal(page.restriction.value, 'locked');
  assert.equal(page.reportOpen.value, false);
  assert.equal(page.reportState.value, 'available');
});

t('an age confirmation that is still missing is explained in the modal', async () => {
  const { page } = await reportPage(() => fail(403, 'age_confirmation_required', '請先完成年齡確認。'));
  page.openReport();
  page.reportPicked.value = ['other'];
  await page.submitReport();
  assert.equal(page.reportError.value, '請先完成年齡確認，才能檢舉劇本。');
  assert.equal(page.reportOpen.value, true);
});

t('the reasons and their labels are the seven fixed ones in the fixed order, and the copy names the modal code', async () => {
  const { page } = await reportPage();
  assert.deepEqual(plain(page.reportReasons).map((item) => item.value), ['minor_sexual', 'sexual_violence', 'real_person_sexual', 'tier_mismatch', 'plagiarism', 'privacy_harassment', 'other']);
  assert.equal(page.reportCopy.code, 'REPORT_SCRIPT');
  assert.equal(page.textMax, 200);
});

// ---- 樣板與腳本的對應 -----------------------------------------------------------------------------

const JS_WORDS = new Set(['true', 'false', 'null', 'undefined', 'in', 'of', 'typeof']);
const LOOP_VARS = new Set(['tag', 'index', 'character', 'place', 'line', 'item']);

/* 取出 Vue 樣板裡所有運算式（插值與 v-*、:、@ 屬性），回傳其中最外層的識別字（不含屬性存取與字串內容）。 */
function templateNames(html) {
  const body = html.slice(html.indexOf('{% raw %}'), html.indexOf('{% endraw %}'));
  const expressions = [];
  for (const match of body.matchAll(/\{\{([\s\S]*?)\}\}/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\s(?:v-if|v-else-if|v-show|v-model|:[\w-]+|@[\w.-]+)="([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of body.matchAll(/\sv-for="[^"]*?\sin\s([^"]*)"/g)) { expressions.push(match[1]); }
  const names = new Set();
  for (const expression of expressions) {
    const stripped = expression.replace(/'[^']*'/g, "''");
    for (const match of stripped.matchAll(/(?<![\w.$])([A-Za-z_$][\w$]*)/g)) { names.add(match[1]); }
  }
  return [...names].filter((name) => !JS_WORDS.has(name) && !LOOP_VARS.has(name)).sort();
}

t('every name the detail template and the shared modal fragment use is returned by setup()', () => {
  const names = [...new Set([...templateNames(read('app/templates/game/script_detail.html')), ...templateNames(read('app/templates/game/_mode_prompt.html'))])].sort();
  assert.ok(names.length >= 15, `只掃到 ${names.length} 個名稱，掃描可能是空洞的：${names}`);
  const { page } = setupDetail();
  const missing = names.filter((name) => !(name in page));
  assert.deepEqual(missing, [], `樣板用到但 setup() 沒有回傳：${missing}`);
  for (const must of ['phase', 'detail', 'failureText', 'gateNotice', 'canStart', 'loadDetail', 'start', 'askOpen', 'modePrompt', 'cancelAsk', 'keepSafe', 'goToSettings', 'restriction', 'reportState', 'reportOpen', 'reportPicked', 'reportText', 'reportCheck', 'canSubmitReport', 'openReport', 'closeReport', 'submitReport', 'submitting']) {
    assert.ok(names.includes(must), `樣板沒有用到 ${must}`);
  }
});
