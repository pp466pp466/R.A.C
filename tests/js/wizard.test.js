'use strict';
/* rpg-creation-wizard 規格的精靈（wizard.js）：以真的 Vue、真的 Auth、真的 game-labels.js／wizard-state.js／game-api.js 在 `vm` 沙盒執行 `setup()`
 * （做法見 tests/js/support/wizard-sandbox.js），只把 axios、location、儲存物件與 mount 換成替身。
 * 涵蓋規格的每個情境：載入成功與三種失敗、聯繫選擇與略過與「選了沒寫」、示範氣泡不送請求、欄位超過上限禁用、生成成功只填表不建存檔、覆蓋確認、
 * 次數用完禁用、503／429／422 的處理與已填內容保留、離開頁面時兩種請求都被中止、開始冒險成功導向、400 跳回對應步驟、422 只顯示類別、
 * 重複點擊只送一次、finally 恢復；最後以沙盒的儲存物件與主控台斷言整個流程沒有寫入任何儲存、沒有輸出任何玩家文字。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setupWizard, serverError, canceled, VERSION_ID, SESSION_ID, STATE } = require('./support/wizard-sandbox.js');

// 每個測試都有逾時：被破壞的程式若讓 Promise 永遠不完成，測試要失敗，不能掛住
const t = (name, fn) => test(name, { timeout: 5000 }, fn);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));       // 沙盒物件的原型與測試行程不同，比對前先 JSON 往返

const SETUP = {
  ok: true,
  title: '雲嵐宗',
  background: '雲嵐山上的宗門，每二十年舉行一次大測。',
  characters: [
    { name: '蘇晚', occupation: '外門師姐', age: 21, hidden_trait: '其實怕水', personality: '溫和' },
    { name: '周遠', occupation: '', age: 40, hidden_trait: '暗中收買人心' },
  ],
  locations: ['雲嵐山', '藏經閣'],
  milestones: [{ id: 'm1', title: '宗門大測', trigger: 'age', value: 20 }],
  limits: { name: 10, personality: 20, age_max: 200, appearance: 40, occupation: 10, speech_style: 30, hidden_trait: 20, intro: 30, anchor_text: 20 },
  origin_options: ['native', 'transmigrator'],
  suggestions_remaining: 3,
  suggestions_available: true,
};
const PREVIEW = {
  ok: true,
  protagonist: { name: '林初', personality: '沉穩', age: 18, appearance: '黑髮黑眼', occupation: '學徒', speech_style: '簡短', hidden_trait: '怕黑' },
  anchor: { kind: 'character', ref: '蘇晚', text: '同門的師姐' },
  remaining: 2,
};
const STARTED = { ok: true, session: { id: SESSION_ID, title: '雲嵐宗', content_tier: 'safe', protagonist: { name: '林初' } } };
const MARKER = '標記QZ7';                                           // 放進玩家文字的標記：任何訊息、日誌、網址都不該出現它

/* 依網址分流的 axios 替身：setup（GET）、suggest（POST …/protagonist/suggest）、start（POST /api/sessions）。 */
const route = (handlers) => (request) => {
  const name = request.url.includes('/setup/') ? 'setup' : request.url.endsWith('/protagonist/suggest') ? 'suggest' : request.method === 'post' ? 'start' : 'other';
  const handler = handlers[name];
  if (!handler) { return Promise.reject(new Error('unexpected request: ' + name)); }
  return handler(request);
};
const ok = (data) => () => Promise.resolve({ data });
const fail = (error) => () => Promise.reject(typeof error === 'function' ? error() : error);
/* 永遠不完成的請求，只在被中止時以取消結束（模擬進行中的請求）。 */
const hang = (request) => new Promise((_resolve, reject) => {
  if (request.signal) { request.signal.addEventListener('abort', () => reject(canceled())); }
});

async function ready(handlers = {}, options = {}) {
  const context = setupWizard({ ...options, respond: route({ setup: ok(SETUP), ...handlers }) });
  await context.mount();
  return context;
}

const fillCard = (page, extra = {}) => Object.assign(page.draft, { name: '林初', personality: '沉穩', age: '18', appearance: '黑髮黑眼', ...extra });
const completeAnchor = (page, text = '同門的師姐') => { page.chooseTarget('character', '蘇晚'); page.anchor.text = text; };
/* 走到創角步驟（聯繫已完成）。 */
function reachCreate(page) {
  page.next();
  completeAnchor(page);
  assert.equal(page.next(), true);
  assert.equal(page.step.value, 'create');
}
const posts = (context, suffix = '') => context.requests.filter((r) => r.method === 'post' && r.url === '/api/sessions' + suffix);

// ---- 載入創角資料 ---------------------------------------------------------------------------------

t('mounting loads the setup of the version in the path, shows the prologue, and does nothing before that', async () => {
  const context = setupWizard({ respond: route({ setup: ok(SETUP) }) });
  const { page } = context;
  assert.equal(page.phase.value, 'loading');
  assert.equal(context.requests.length, 0);                          // setup() 本身不送請求，掛載才載入
  await context.mount();
  assert.equal(context.requests.length, 1);
  const [request] = context.requests;
  assert.equal(request.method, 'get');
  assert.equal(request.url, `/api/sessions/setup/${VERSION_ID}`);
  assert.ok(request.signal && request.signal.aborted === false);   // 載入也可以在離開頁面時中止
  assert.equal(page.phase.value, 'ready');
  assert.equal(page.step.value, 'prologue');
  assert.equal(page.setup.value.title, '雲嵐宗');
  assert.equal(page.suggestionsRemaining.value, 3);
  assert.deepEqual(plain(page.originOptions.value), ['native', 'transmigrator']);
  assert.equal(page.loadFailure.value, null);
});

t('a version that is not found shows the not-found text with a link home, and no script content', async () => {
  const context = await ready({ setup: fail(serverError(404, 'not_found', '找不到。')) });
  const { page } = context;
  assert.equal(page.phase.value, 'failed');
  assert.equal(page.setup.value, null);
  assert.match(page.loadFailure.value.message, /找不到這個劇本/);
  assert.deepEqual(plain(page.loadFailure.value.link), { href: '/game', text: '回到冒險' });
  assert.equal(page.loadFailure.value.retry, false);
});

t('a script above the player mode explains that and links to the account settings, without entering the creation', async () => {
  const { page } = await ready({ setup: fail(serverError(403, 'content_tier_restricted', '內容模式高於目前模式。')) });
  assert.equal(page.phase.value, 'failed');
  assert.match(page.loadFailure.value.message, /內容模式/);
  assert.equal(page.loadFailure.value.link.href, '/account');
  assert.equal(page.setup.value, null);
});

t('an unconfirmed age and a locked account each get their own explanation', async () => {
  const age = await ready({ setup: fail(serverError(403, 'age_confirmation_required', '請先確認年齡。')) });
  assert.match(age.page.loadFailure.value.message, /年齡確認/);
  assert.equal(age.page.restriction.value, null);
  const locked = await ready({ setup: fail(serverError(403, 'account_locked', '已被鎖定。')) });
  assert.equal(locked.page.restriction.value, 'locked');
  assert.equal(locked.page.phase.value, 'failed');
});

t('a failed load can be retried, and the retry loads again', async () => {
  let call = 0;
  const context = await ready({ setup: () => { call += 1; return call === 1 ? Promise.reject(new Error('Network Error')) : Promise.resolve({ data: SETUP }); } });
  const { page } = context;
  assert.equal(page.phase.value, 'failed');
  assert.equal(page.loadFailure.value.retry, true);
  assert.equal(await page.loadSetup(), true);
  assert.equal(page.phase.value, 'ready');
  assert.equal(page.loadFailure.value, null);
  assert.equal(context.requests.length, 2);
});

t('loading shows the loading state, and pressing retry while a load is in flight sends nothing more', async () => {
  let release;
  const context = setupWizard({ respond: route({ setup: () => new Promise((resolve) => { release = () => resolve({ data: SETUP }); }) }) });
  const first = context.mount();
  await tick();
  assert.equal(context.page.phase.value, 'loading');
  assert.equal(await context.page.loadSetup(), false);
  assert.equal(context.requests.length, 1);
  release();
  await first;
  assert.equal(context.page.phase.value, 'ready');
});

t('a success body that is not a script (no title or background) is a retryable failure, not a blank wizard', async () => {
  for (const data of [{}, null, { ok: true, title: 5, background: 'x' }, { title: 'x' }]) {
    const { page } = await ready({ setup: ok(data) });
    assert.equal(page.phase.value, 'failed', JSON.stringify(data));
    assert.equal(page.loadFailure.value.retry, true);
  }
});

t('an expired session on load goes to the login page and keeps the wizard path', async () => {
  const context = await ready({ setup: fail(serverError(401, 'unauthorized', '請先登入。')) });
  assert.deepEqual(context.navigations, [`/login?next=${encodeURIComponent('/game/start/' + VERSION_ID)}`]);
  assert.equal(context.page.phase.value, 'failed');
});

t('a path without a readable version id is a not-found state with no request', async () => {
  for (const pathname of ['/', '', '/game/start/%E0%A4%A']) {
    const context = setupWizard({ pathname, respond: route({ setup: ok(SETUP) }) });
    await context.mount();
    assert.equal(context.requests.length, 0, pathname);
    assert.equal(context.page.phase.value, 'failed', pathname);
    assert.match(context.page.loadFailure.value.message, /找不到這個劇本/);
  }
});

t('the version id is read from the last path segment and sent encoded', async () => {
  const context = setupWizard({ pathname: '/game/start/a%2Fb', respond: route({ setup: ok(SETUP) }) });
  await context.mount();
  assert.equal(context.requests[0].url, '/api/sessions/setup/a%2Fb');
});

// ---- 序章與聯繫 -----------------------------------------------------------------------------------

t('the prologue is read-only and only moves forward', async () => {
  const { page } = await ready();
  assert.equal(page.step.value, 'prologue');
  assert.equal(page.stepIndex.value, 0);
  assert.equal(page.back(), false);
  assert.equal(page.next(), true);
  assert.equal(page.step.value, 'contact');
  assert.equal(page.stepIndex.value, 1);
});

t('the contact options only carry names and occupations, never ages or hidden traits', async () => {
  const { page } = await ready();
  const groups = plain(page.targetGroups.value);
  assert.deepEqual(groups.map((g) => g.kind), ['character', 'location', 'milestone']);
  assert.deepEqual(groups[0].options.map((o) => [o.label, o.detail]), [['蘇晚', '外門師姐'], ['周遠', '']]);
  const text = JSON.stringify(groups);
  assert.ok(!text.includes('其實怕水') && !text.includes('暗中收買人心') && !/"age"/.test(text));
});

t('choosing a target and writing one sentence completes the contact step, and the summary shows both', async () => {
  const { page } = await ready();
  page.next();
  assert.equal(page.isChosen('character', '蘇晚'), false);
  completeAnchor(page);
  assert.equal(page.isChosen('character', '蘇晚'), true);
  assert.deepEqual(plain(page.chosenTarget.value), { kindLabel: '人物', label: '蘇晚' });
  assert.equal(page.next(), true);
  assert.equal(page.step.value, 'create');
  assert.match(page.anchorSummary.value, /人物「蘇晚」：同門的師姐/);
});

t('skipping the contact needs the confirmation, and the start request then carries skip_anchor and no anchor', async () => {
  const context = await ready({ start: ok(STARTED) });
  const { page } = context;
  page.next();
  page.askSkip();
  assert.equal(page.modal.skip, true);
  assert.equal(page.anchor.skipped, false);                          // 還沒確認就還沒略過
  assert.equal(page.confirmSkip(), true);
  assert.equal(page.modal.skip, false);
  assert.equal(page.anchor.skipped, true);
  assert.equal(page.isChosen('character', '蘇晚'), false);
  assert.equal(page.next(), true);
  assert.match(page.anchorSummary.value, /略過/);
  fillCard(page);
  await page.start();
  const [request] = posts(context);
  assert.equal(request.data.skip_anchor, true);
  assert.ok(!('anchor' in request.data));
});

t('a skipped contact can be turned back into a normal one', async () => {
  const { page } = await ready();
  page.next();
  page.confirmSkip();
  assert.equal(page.unskip(), true);
  assert.equal(page.anchor.skipped, false);
  assert.equal(page.anchor.kind, '');
});

t('a chosen target with no sentence stays on the contact step and says to write one or skip', async () => {
  const { page } = await ready();
  page.next();
  page.chooseTarget('character', '蘇晚');
  assert.equal(page.next(), false);
  assert.equal(page.step.value, 'contact');
  assert.equal(page.anchorErrors.value.length, 1);
  assert.match(page.anchorErrors.value[0], /略過/);
  page.anchor.text = '同門';                                          // 玩家改了聯繫，舊的提示就不再成立
  assert.equal(page.anchorErrors.value.length, 0);
});

t('nothing chosen at all also stays on the contact step', async () => {
  const { page } = await ready();
  page.next();
  assert.equal(page.next(), false);
  assert.equal(page.step.value, 'contact');
  assert.ok(page.anchorErrors.value.length > 0);
  assert.match(page.notice.value.text, /聯繫/);
});

t('an over-long relation sentence is stopped on the contact step and the counter shows it', async () => {
  const { page } = await ready();
  page.next();
  completeAnchor(page, '長'.repeat(21));
  assert.equal(page.anchorCounts.value.over, true);
  assert.equal(page.next(), false);
  page.anchor.text = '長'.repeat(20);
  assert.equal(page.next(), true);
});

t('going back and forth: back always works, going to a later step with goTo does not', async () => {
  const { page } = await ready();
  page.next();
  assert.equal(page.goTo('create'), false);                          // 不能跳過聯繫的前進條件
  assert.equal(page.step.value, 'contact');
  completeAnchor(page);
  page.next();
  assert.equal(page.goTo('prologue'), true);
  assert.equal(page.step.value, 'prologue');
  assert.equal(page.goTo('nowhere'), false);
  assert.equal(page.goTo(undefined), false);
  page.next();
  assert.equal(page.back(), true);
  assert.equal(page.step.value, 'prologue');
});

t('the contact choice ignores clicks while skipped', async () => {
  const { page } = await ready();
  page.next();
  page.confirmSkip();
  assert.equal(page.chooseTarget('character', '蘇晚'), false);
  assert.equal(page.anchor.kind, '');
});

// ---- 創角：欄位、字數、示範氣泡、來歷 -------------------------------------------------------------

t('the demo bubbles follow the name and the speech style, and rendering them sends no request', async () => {
  const context = await ready();
  const { page } = context;
  reachCreate(page);
  const before = context.requests.length;
  page.draft.name = '林初';
  page.draft.speech_style = '話少，語氣平淡';
  const bubbles = plain(page.bubbles.value);
  assert.ok(bubbles.some((b) => b.text.includes('林初') || b.speaker === '林初'));
  assert.ok(bubbles.some((b) => b.note.includes('話少，語氣平淡')));
  assert.equal(context.requests.length, before);
});

t('a field over its limit shows the overflow and disables start, and fixing it enables start again', async () => {
  const { page } = await ready();
  reachCreate(page);
  fillCard(page);
  assert.equal(page.startDisabled.value, false);
  page.draft.personality = '性'.repeat(21);
  assert.equal(page.counts.value.personality.over, true);
  assert.equal(page.counts.value.personality.max, 20);
  assert.equal(page.startDisabled.value, true);
  assert.equal(await page.start(), false);
  page.draft.personality = '性'.repeat(20);
  assert.equal(page.startDisabled.value, false);
});

t('the origin defaults to a local and the chosen origin is sent; there is no avatar or gender field', async () => {
  const context = await ready({ start: ok(STARTED) });
  const { page } = context;
  reachCreate(page);
  assert.equal(page.draft.origin, 'native');
  assert.deepEqual(Object.keys(plain(page.draft)).filter((key) => /avatar|gender|sex|image|photo/i.test(key)), []);
  fillCard(page);
  page.draft.origin = 'transmigrator';
  await page.start();
  assert.equal(posts(context)[0].data.protagonist.origin, 'transmigrator');
  assert.equal(page.originLabel('native'), '本地人');
  assert.equal(page.originLabel('transmigrator'), '穿越者');
});

// ---- AI 生成預覽 ----------------------------------------------------------------------------------

t('a successful suggestion fills the card and the contact, keeps origin and intro, lowers the count, and creates no save', async () => {
  const context = await ready({ suggest: ok(PREVIEW) });
  const { page } = context;
  page.step.value = 'create';                                        // 聯繫與角色卡都還沒填：生成不需要先確認覆蓋
  page.draft.origin = 'transmigrator';
  page.draft.intro = '我想當個劍客';
  assert.equal(page.canSuggest.value, true);
  assert.equal(await page.requestSuggestion(), true);
  const suggest = context.requests.filter((r) => r.url.endsWith('/protagonist/suggest'));
  assert.equal(suggest.length, 1);
  assert.equal(suggest[0].method, 'post');
  assert.deepEqual(suggest[0].data, { version_id: VERSION_ID, intro: '我想當個劍客' });
  assert.equal(suggest[0].timeout, 35000);                           // 逾時取自頁面初始狀態
  assert.ok(suggest[0].signal);
  assert.equal(page.draft.name, '林初');
  assert.equal(page.draft.age, '18');
  assert.equal(page.draft.origin, 'transmigrator');                  // 預覽不決定來歷
  assert.equal(page.draft.intro, '我想當個劍客');                      // 自我介紹不被覆蓋
  assert.deepEqual(plain({ kind: page.anchor.kind, ref: page.anchor.ref, text: page.anchor.text }), PREVIEW.anchor);
  assert.equal(page.suggestionsRemaining.value, 2);
  assert.equal(page.suggesting.value, false);
  assert.equal(context.toasts.length, 1);
  assert.equal(posts(context).length, 0);                            // 只是預覽：沒有開存檔
});

t('the suggestion asks for confirmation first when something was already written, and only then sends', async () => {
  const context = await ready({ suggest: ok(PREVIEW) });
  const { page } = context;
  reachCreate(page);
  fillCard(page, { name: '舊名字' });
  assert.equal(await page.requestSuggestion(), false);
  assert.equal(page.modal.overwrite, true);
  assert.equal(context.requests.filter((r) => r.url.endsWith('/protagonist/suggest')).length, 0);
  assert.equal(page.draft.name, '舊名字');
  assert.equal(await page.confirmSuggestion(), true);
  assert.equal(page.modal.overwrite, false);
  assert.equal(context.requests.filter((r) => r.url.endsWith('/protagonist/suggest')).length, 1);
  assert.equal(page.draft.name, '林初');
});

t('with an untouched card the suggestion goes straight out, with an empty intro sent as null', async () => {
  const context = await ready({ suggest: ok(PREVIEW) });
  const { page } = context;
  page.step.value = 'create';
  await page.requestSuggestion();
  assert.equal(page.modal.overwrite, false);
  assert.deepEqual(context.requests.find((r) => r.url.endsWith('/protagonist/suggest')).data, { version_id: VERSION_ID, intro: null });
});

t('no suggestions left disables the button, sends nothing and explains the self-made option', async () => {
  const context = await ready();
  const exhausted = setupWizard({ respond: route({ setup: ok({ ...SETUP, suggestions_remaining: 0 }) }) });
  await exhausted.mount();
  assert.equal(exhausted.page.suggestionsRemaining.value, 0);
  assert.equal(exhausted.page.canSuggest.value, false);
  assert.equal(await exhausted.page.requestSuggestion(), false);
  assert.equal(exhausted.requests.length, 1);
  assert.match(exhausted.page.suggestNotice.value, /次數/);
  assert.match(exhausted.page.suggestNotice.value, /自己創角/);
  assert.equal(context.page.canSuggest.value, true);                  // 對照：還有次數時可以生成
});

t('a server without a model disables the button with an explanation', async () => {
  const { page } = await ready();
  const off = setupWizard({ respond: route({ setup: ok({ ...SETUP, suggestions_available: false }) }) });
  await off.mount();
  assert.equal(off.page.canSuggest.value, false);
  assert.match(off.page.suggestNotice.value, /自己創角/);
  assert.equal(page.canSuggest.value, true);
});

t('an over-long intro disables the suggestion and says why', async () => {
  const { page } = await ready({ suggest: ok(PREVIEW) });
  page.draft.intro = '介'.repeat(31);
  assert.equal(page.canSuggest.value, false);
  assert.match(page.suggestNotice.value, /自我介紹/);
});

t('a suggestion that is unavailable keeps everything the player wrote and leaves the button usable', async () => {
  const context = await ready({ suggest: fail(serverError(503, 'suggestion_unavailable', '暫時無法生成。')) });
  const { page } = context;
  reachCreate(page);
  fillCard(page, { personality: `沉穩${MARKER}` });
  const before = plain(page.draft);
  const anchorBefore = plain(page.anchor);
  await page.confirmSuggestion();
  assert.deepEqual(plain(page.draft), before);
  assert.deepEqual(plain(page.anchor), anchorBefore);
  assert.match(page.suggestNotice.value, /暫時無法生成/);
  assert.equal(page.suggesting.value, false);                        // finally 恢復
  assert.equal(page.canSuggest.value, true);
  assert.equal(page.suggestionsRemaining.value, 3);
  assert.equal(context.toasts.length, 0);
});

t('the daily limit answer (429) shows the used-up text and disables the button for the rest of the day', async () => {
  const { page } = await ready({ suggest: fail(serverError(429, 'suggestion_limit', '今日次數已用完。')) });
  await page.requestSuggestion();
  assert.equal(page.suggestionsRemaining.value, 0);
  assert.equal(page.canSuggest.value, false);
  assert.match(page.suggestNotice.value, /次數已經用完/);
  assert.equal(page.suggesting.value, false);
});

t('a suggestion rejected by the review shows only the category and never echoes the intro', async () => {
  const { page } = await ready({ suggest: fail(serverError(422, 'protagonist_rejected', `你的自我介紹${MARKER}不符`, { categories: ['sexual_violence', '<script>x</script>'] })) });
  page.draft.intro = `我的故事${MARKER}`;
  await page.requestSuggestion();
  assert.match(page.suggestNotice.value, /性暴力/);
  assert.ok(!page.suggestNotice.value.includes(MARKER) && !page.suggestNotice.value.includes('script'));
  assert.equal(page.draft.intro, `我的故事${MARKER}`);               // 自我介紹仍在輸入框，不被清掉
  assert.equal(page.suggestionsRemaining.value, 3);
});

t('a suggestion that times out or loses the connection keeps the draft and can be tried again', async () => {
  for (const error of [new Error('Network Error'), Object.assign(new Error('timeout'), { code: 'ECONNABORTED' })]) {
    const { page } = await ready({ suggest: fail(error) });
    fillCard(page);
    await page.confirmSuggestion();
    assert.equal(page.draft.name, '林初');
    assert.match(page.suggestNotice.value, /自己創角/);
    assert.equal(page.canSuggest.value, true);
  }
});

t('a suggestion body without a card changes nothing and says it could not generate', async () => {
  for (const data of [{}, null, { ok: true, protagonist: 'x' }]) {
    const { page } = await ready({ suggest: ok(data) });
    fillCard(page);
    const before = plain(page.draft);
    await page.confirmSuggestion();
    assert.deepEqual(plain(page.draft), before, JSON.stringify(data));
    assert.ok(page.suggestNotice.value.length > 0);
    assert.equal(page.suggestionsRemaining.value, 3);
  }
});

t('a half-formed anchor in the preview never wipes the contact the player had', async () => {
  const { page } = await ready({ suggest: ok({ ...PREVIEW, anchor: { kind: 'character', ref: '蘇晚' } }) });
  reachCreate(page);
  await page.confirmSuggestion();
  assert.equal(page.draft.name, '林初');
  assert.equal(page.anchor.text, '同門的師姐');                       // 沿用原本的聯繫
});

t('a suggestion in flight is aborted when the page is left, and the state is restored', async () => {
  const context = await ready({ suggest: hang });
  const { page } = context;
  reachCreate(page);
  const pending = page.confirmSuggestion();                          // 聯繫已填，所以走確認覆蓋之後的那一步
  assert.equal(page.suggesting.value, true);
  assert.equal(page.canSuggest.value, false);                        // 進行中不能再按
  const request = context.requests.find((r) => r.url.endsWith('/protagonist/suggest'));
  assert.equal(request.signal.aborted, false);
  context.fire('pagehide');
  assert.equal(request.signal.aborted, true);
  assert.equal(await pending, false);
  assert.equal(page.suggesting.value, false);
  assert.equal(page.suggestNotice.value, '');                        // 中止不顯示錯誤
});

t('a second press while a suggestion is in flight sends nothing', async () => {
  const context = await ready({ suggest: hang });
  const { page } = context;
  reachCreate(page);
  const first = page.confirmSuggestion();
  assert.equal(await page.requestSuggestion(), false);
  assert.equal(await page.confirmSuggestion(), false);
  assert.equal(context.requests.filter((r) => r.url.endsWith('/protagonist/suggest')).length, 1);
  context.fire('pagehide');
  await first;
});

t('an expired session during a suggestion goes to the login page', async () => {
  const context = await ready({ suggest: fail(serverError(401, 'unauthorized', '請先登入。')) });
  await context.page.requestSuggestion();
  assert.equal(context.navigations.length, 1);
  assert.match(context.navigations[0], /^\/login\?next=/);
});

// ---- 開始冒險 -------------------------------------------------------------------------------------

t('start sends one request with the card, origin and contact, then goes to the new save', async () => {
  const context = await ready({ start: ok(STARTED) });
  const { page } = context;
  reachCreate(page);
  fillCard(page, { occupation: '學徒' });
  assert.equal(await page.start(), true);
  const sent = posts(context);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].timeout, 65000);                               // 逾時取自頁面初始狀態（涵蓋兩層審查的最壞時間）
  assert.ok(sent[0].signal);
  assert.deepEqual(sent[0].data, {
    version_id: VERSION_ID,
    protagonist: { name: '林初', personality: '沉穩', age: 18, appearance: '黑髮黑眼', occupation: '學徒', speech_style: null, hidden_trait: null, origin: 'native' },
    anchor: { kind: 'character', ref: '蘇晚', text: '同門的師姐' },
  });
  assert.deepEqual(context.navigations, [`/game/${SESSION_ID}`]);
  assert.equal(page.busy.value, false);                               // finally 恢復
  assert.equal(page.startDisabled.value, true);                       // 但在導向完成之前不能再送第二次
  assert.equal(await page.start(), false);
  assert.equal(posts(context).length, 1);
});

t('while start is in flight every button is disabled and a second press sends nothing', async () => {
  let release;
  const context = await ready({ start: () => new Promise((resolve) => { release = () => resolve({ data: STARTED }); }) });
  const { page } = context;
  reachCreate(page);
  fillCard(page);
  const first = page.start();
  assert.equal(page.busy.value, true);
  assert.equal(page.startDisabled.value, true);
  assert.equal(await page.start(), false);
  assert.equal(page.back(), false);                                   // 其他按鈕也被擋
  assert.equal(page.goTo('prologue'), false);
  assert.equal(await page.requestSuggestion(), false);
  assert.equal(posts(context).length, 1);
  release();
  assert.equal(await first, true);
  assert.equal(posts(context).length, 1);
  assert.equal(page.busy.value, false);
});

t('a field error from the server is shown by that field, the wizard stays on the step, and the draft is untouched', async () => {
  const context = await ready({ start: fail(serverError(400, 'validation_error', '資料有誤。', { fields: { 'protagonist.age': '年齡必須是 1 到 200 的整數。' } })) });
  const { page } = context;
  reachCreate(page);
  fillCard(page, { age: '999' });
  const before = plain(page.draft);
  assert.equal(await page.start(), false);
  assert.equal(page.step.value, 'create');
  assert.equal(page.fieldErrors.age, '年齡必須是 1 到 200 的整數。');
  assert.deepEqual(plain(page.draft), before);
  assert.equal(page.busy.value, false);
  assert.deepEqual(context.navigations, []);
  page.draft.age = '19';                                              // 玩家改了那一欄，它的舊錯誤就消失
  assert.equal(page.fieldErrors.age, undefined);
});

t('an error on the contact goes back to the contact step with the message there', async () => {
  const context = await ready({ start: fail(serverError(400, 'validation_error', '資料有誤。', { fields: { 'anchor.text': '關係的描述不可以是性的關係。' } })) });
  const { page } = context;
  reachCreate(page);
  fillCard(page);
  await page.start();
  assert.equal(page.step.value, 'contact');
  assert.deepEqual(plain(page.anchorErrors.value), ['關係的描述不可以是性的關係。']);
  assert.equal(page.anchor.text, '同門的師姐');
  assert.equal(page.draft.name, '林初');
});

t('errors on both steps go back to the earlier one', async () => {
  const fields = { 'protagonist.name': '姓名不合規定。', 'anchor.ref': '找不到這個對象。' };
  const context = await ready({ start: fail(serverError(400, 'validation_error', '資料有誤。', { fields })) });
  const { page } = context;
  reachCreate(page);
  fillCard(page);
  await page.start();
  assert.equal(page.step.value, 'contact');
  assert.equal(page.fieldErrors.name, '姓名不合規定。');
  assert.equal(page.anchorErrors.value.length, 1);
});

t('a review rejection (422) shows only the category and echoes no player text; the button is usable again', async () => {
  const context = await ready({ start: fail(serverError(422, 'protagonist_rejected', `主角${MARKER}不合規定`, { categories: ['minor_sexual'] })) });
  const { page } = context;
  reachCreate(page);
  fillCard(page, { personality: `沉穩${MARKER}` });
  assert.equal(await page.start(), false);
  const text = JSON.stringify(plain(page.notice.value)) + JSON.stringify(plain(page.fieldErrors));
  assert.match(text, /涉及未成年角色的性內容/);
  assert.ok(!text.includes(MARKER));
  assert.equal(page.busy.value, false);
  assert.equal(page.startDisabled.value, false);
  assert.deepEqual(context.navigations, []);
});

t('a rejected script (422 script_rejected) names the script as the problem, not the player', async () => {
  const { page } = await ready({ start: fail(serverError(422, 'script_rejected', '劇本不合規。', { categories: ['tier_mismatch'] })) });
  reachCreate(page);
  fillCard(page);
  await page.start();
  assert.match(page.notice.value.text, /劇本/);
  assert.match(page.notice.value.text, /內容模式不符/);
});

t('a review that is temporarily unavailable (503) asks to try again later and enables the button', async () => {
  const { page } = await ready({ start: fail(serverError(503, 'review_unavailable', '審查暫時無法使用。')) });
  reachCreate(page);
  fillCard(page);
  await page.start();
  assert.match(page.notice.value.text, /稍後再試/);
  assert.equal(page.startDisabled.value, false);
  assert.equal(page.draft.name, '林初');
});

t('the gate answers (403) each explain themselves with a link, and keep the draft', async () => {
  for (const [code, pattern, href] of [['content_tier_restricted', /內容模式/, '/account'], ['age_confirmation_required', /年齡確認/, '/account'], ['account_deletion_pending', /刪除/, '/account']]) {
    const { page } = await ready({ start: fail(serverError(403, code, '被擋。')) });
    reachCreate(page);
    fillCard(page);
    await page.start();
    assert.match(page.notice.value.text, pattern, code);
    assert.equal(page.notice.value.link.href, href, code);
    assert.equal(page.draft.name, '林初');
  }
});

t('a timeout or a lost connection keeps the draft, tells the player to check the saves first, and restores the buttons', async () => {
  for (const error of [new Error('Network Error'), Object.assign(new Error('timeout'), { code: 'ECONNABORTED' })]) {
    const context = await ready({ start: fail(error) });
    const { page } = context;
    reachCreate(page);
    fillCard(page);
    assert.equal(await page.start(), false);
    assert.match(page.notice.value.text, /我的存檔/);
    assert.equal(page.busy.value, false);
    assert.equal(page.startDisabled.value, false);
    assert.equal(page.draft.name, '林初');
    assert.deepEqual(context.navigations, []);
  }
});

t('a request aborted by leaving the page restores the state and shows no message', async () => {
  const context = await ready({ start: hang });
  const { page } = context;
  reachCreate(page);
  fillCard(page);
  const pending = page.start();
  const request = posts(context)[0];
  assert.equal(request.signal.aborted, false);
  context.fire('pagehide');
  assert.equal(request.signal.aborted, true);
  assert.equal(await pending, false);
  assert.equal(page.busy.value, false);
  assert.equal(page.notice.value, null);
  assert.equal(page.draft.name, '林初');
});

t('an expired session on start goes to the login page', async () => {
  const context = await ready({ start: fail(serverError(401, 'unauthorized', '請先登入。')) });
  reachCreate(context.page);
  fillCard(context.page);
  await context.page.start();
  assert.equal(context.navigations.length, 1);
  assert.match(context.navigations[0], /^\/login\?next=/);
  assert.equal(context.page.busy.value, false);
});

t('missing required fields are pointed out on the fields and nothing is sent', async () => {
  const context = await ready({ start: ok(STARTED) });
  const { page } = context;
  reachCreate(page);
  assert.equal(page.startDisabled.value, false);                      // 沒填不是禁用，按下時指出是哪一欄
  assert.equal(await page.start(), false);
  assert.equal(posts(context).length, 0);
  for (const key of ['name', 'personality', 'age', 'appearance']) { assert.ok(page.fieldErrors[key], key); }
  assert.equal(page.fieldErrors.occupation, undefined);               // 選填欄位不擋
  assert.match(page.notice.value.text, /標示的欄位/);
  assert.equal(page.step.value, 'create');
});

t('an unset contact sends the player back to the contact step instead of sending', async () => {
  const context = await ready({ start: ok(STARTED) });
  const { page } = context;
  reachCreate(page);
  fillCard(page);
  Object.assign(page.anchor, { skipped: false, kind: '', ref: '', text: '' });
  assert.equal(await page.start(), false);
  assert.equal(page.step.value, 'contact');
  assert.equal(posts(context).length, 0);
  assert.ok(page.anchorErrors.value.length > 0);
});

t('a success answer without a save id is not retried blindly: it points to the saves and stays here', async () => {
  for (const data of [{ ok: true }, { ok: true, session: {} }, { ok: true, session: { id: 5 } }, null]) {
    const context = await ready({ start: ok(data) });
    const { page } = context;
    reachCreate(page);
    fillCard(page);
    assert.equal(await page.start(), false, JSON.stringify(data));
    assert.match(page.notice.value.text, /我的存檔/);
    assert.equal(page.notice.value.link.href, '/game');
    assert.deepEqual(context.navigations, []);
    assert.equal(page.busy.value, false);
  }
});

t('a non-empty gate disables the suggestion and the start, and the notice links to the account settings', async () => {
  const state = { ...STATE, gate: 'age_confirmation_required' };
  const context = await ready({}, { state });
  const { page } = context;
  assert.equal(page.gateNotice.value.link, '/account');
  assert.equal(page.canSuggest.value, false);
  assert.equal(page.startDisabled.value, true);
  assert.equal(await page.start(), false);
  assert.equal(await page.requestSuggestion(), false);
  assert.equal(context.requests.length, 1);                           // 只有載入
});

t('the two timeouts come from the page state, and a damaged state sends no made-up number', async () => {
  const custom = await ready({ suggest: ok(PREVIEW), start: ok(STARTED) }, { state: { ...STATE, suggest_timeout_ms: 45000, start_timeout_ms: 85000 } });
  reachCreate(custom.page);
  await custom.page.confirmSuggestion();
  fillCard(custom.page);
  await custom.page.start();
  assert.equal(custom.requests.find((r) => r.url.endsWith('/protagonist/suggest')).timeout, 45000);
  assert.equal(posts(custom)[0].timeout, 85000);
  for (const state of ['not json', { member_tier: 'free', suggest_timeout_ms: 'x', start_timeout_ms: -1 }]) {
    const broken = await ready({ suggest: ok(PREVIEW), start: ok(STARTED) }, { state });
    reachCreate(broken.page);
    await broken.page.confirmSuggestion();
    fillCard(broken.page);
    await broken.page.start();
    assert.equal(broken.requests.find((r) => r.url.endsWith('/protagonist/suggest')).timeout, undefined);
    assert.equal(posts(broken)[0].timeout, undefined);
  }
});

// ---- 離開 -----------------------------------------------------------------------------------------

t('leaving with nothing written is a plain link; with something written it asks first', async () => {
  const { page } = await ready();
  let prevented = 0;
  const event = { preventDefault: () => { prevented += 1; } };
  assert.equal(page.leaveClick(event), false);
  assert.equal(prevented, 0);
  assert.equal(page.modal.leave, false);
  page.draft.intro = '想寫一點東西';                                   // 自我介紹也算
  assert.equal(page.leaveClick(event), true);
  assert.equal(prevented, 1);
  assert.equal(page.modal.leave, true);
});

t('confirming the leave goes to the adventure home and closes the modal', async () => {
  const context = await ready();
  fillCard(context.page);
  context.page.leaveClick({ preventDefault() {} });
  assert.equal(context.page.confirmLeave(), true);
  assert.equal(context.page.modal.leave, false);
  assert.deepEqual(context.navigations, ['/game']);
});

t('leaving works with a missing event, and a loaded-failure page needs no confirmation', async () => {
  const { page } = await ready({ setup: fail(serverError(404, 'not_found', '找不到。')) });
  assert.equal(page.leaveClick(), false);
  const filled = await ready();
  fillCard(filled.page);
  assert.equal(filled.page.leaveClick(undefined), true);
});

// ---- 沒有任何儲存、日誌與網址 ---------------------------------------------------------------------

t('a whole run writes to no storage, logs nothing, and puts no player text in any URL or path', async () => {
  const context = await ready({ suggest: ok(PREVIEW), start: ok(STARTED) });
  const { page } = context;
  page.next();
  completeAnchor(page, `關係${MARKER}`);
  page.next();
  fillCard(page, { name: `名${MARKER}`, personality: `個性${MARKER}` });
  page.draft.intro = `介紹${MARKER}`;
  page.draft.origin = 'transmigrator';
  await page.confirmSuggestion();
  fillCard(page, { name: `名${MARKER}` });
  await page.start();
  assert.deepEqual(context.storageWrites, []);                        // localStorage／sessionStorage／IndexedDB／cookie 全都沒有被寫
  assert.deepEqual(context.logs, []);                                 // 主控台沒有任何輸出
  for (const request of context.requests) { assert.ok(!request.url.includes(MARKER) && !request.url.includes(encodeURIComponent(MARKER)), request.url); }
  for (const target of context.navigations) { assert.ok(!target.includes(MARKER) && !target.includes(encodeURIComponent(MARKER)), target); }
  assert.deepEqual(context.navigations, [`/game/${SESSION_ID}`]);
});

t('failures write nothing and log nothing either', async () => {
  const errors = [serverError(400, 'validation_error', `錯${MARKER}`, { fields: { 'protagonist.name': `x${MARKER}` } }), serverError(422, 'protagonist_rejected', MARKER, { categories: ['minor_sexual'] }), serverError(503, 'review_unavailable', MARKER), new Error(MARKER)];
  for (const error of errors) {
    const context = await ready({ start: fail(error), suggest: fail(error) });
    reachCreate(context.page);
    fillCard(context.page, { name: `名${MARKER}` });
    await context.page.start();
    await context.page.confirmSuggestion();
    assert.deepEqual(context.storageWrites, []);
    assert.deepEqual(context.logs, []);
  }
});

t('reloading starts over: a new page has the prologue and an empty form', async () => {
  const first = await ready();
  reachCreate(first.page);
  fillCard(first.page);
  const second = await ready();                                       // 重新整理 = 新的頁面實例，沒有任何東西可以讀回來
  assert.equal(second.page.step.value, 'prologue');
  assert.deepEqual(plain(second.page.draft), { origin: 'native', intro: '', name: '', personality: '', age: '', appearance: '', occupation: '', speech_style: '', hidden_trait: '' });
  assert.equal(second.page.anchor.text, '');
});
