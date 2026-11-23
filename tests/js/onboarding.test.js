'use strict';
/* onboarding-wizard 規格的前端純函式與流程控制器：三個步驟與進度、各步可前進的條件、錯誤回到對應的步驟、
 * 一次送出且成功之後才存故事偏好、離開與重新整理不留下資料。依賴（http、導向、計時器）都是替身，不需要瀏覽器。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Auth = require('../../app/static/js/auth/auth-common.js');
const Flows = require('../../app/static/js/auth/auth-flows.js');
const Onboarding = require('../../app/static/js/auth/onboarding.js');

const SOURCE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'auth', 'onboarding.js');

const server = (status, code, message, fields) => Auth.describeError({
  response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } }
});

/* 原始碼去掉區塊註解與整行註解之後的程式碼（註解裡可以提到這些字眼，程式碼不行）。 */
function codeOnly() {
  const source = fs.readFileSync(SOURCE, 'utf8');
  return source.replace(/\/\*[\s\S]*?\*\//g, '').split('\n').filter((line) => !line.trim().startsWith('//')).join('\n');
}

const VALUES = { nickname: '  天天 ', birthDate: '1995-05-05', ageRange: '25-34', storyPreference: 'all' };

/* 替身：記錄每個呼叫的順序；`script` 讓個別請求失敗或永不完成。 */
function rig(script = {}) {
  const calls = [];
  const http = {
    post: (url, body) => { calls.push(['post', url, body]); return script.post ? script.post(url, body) : Promise.resolve({ data: {} }); },
    put: (url, body) => { calls.push(['put', url, body]); return script.put ? script.put(url, body) : Promise.resolve({ data: {} }); }
  };
  const navigate = (to) => calls.push(['navigate', to]);
  const timers = [];
  const delay = (ms) => new Promise((resolve) => { timers.push({ ms, resolve }); });
  const flow = Onboarding.createOnboarding({ http, navigate, destination: '/game', delay });
  return { calls, flow, timers };
}

// ---- 三個步驟與進度 ------------------------------------------------------------------------------

test('there are three steps: welcome, age confirmation and story preference', () => {
  assert.deepEqual(Onboarding.STEPS.map((s) => s.id), ['welcome', 'age', 'preference']);
  assert.deepEqual(Onboarding.STEPS.map((s) => s.title), ['歡迎', '年齡確認', '故事偏好']);
});

test('the first step has three bar segments with only the first filled, no back button and a next button', () => {
  const p = Onboarding.progress(0);
  assert.deepEqual(p.segments, [true, false, false]);
  assert.equal(p.label, '第 1 步，共 3 步');
  assert.equal(p.isFirst, true);
  assert.equal(p.isLast, false);
  assert.equal(p.primaryLabel, '下一步');
  assert.equal(p.step, 'welcome');
});

test('later steps fill every segment up to the current one and the last step offers 開始', () => {
  assert.deepEqual(Onboarding.progress(1).segments, [true, true, false]);
  assert.equal(Onboarding.progress(1).label, '第 2 步，共 3 步');
  assert.equal(Onboarding.progress(1).isFirst, false);
  const last = Onboarding.progress(2);
  assert.deepEqual(last.segments, [true, true, true]);
  assert.equal(last.label, '第 3 步，共 3 步');
  assert.equal(last.isLast, true);
  assert.equal(last.primaryLabel, '開始');
  assert.equal(last.step, 'preference');
});

test('moving forward and back stays inside the list', () => {
  assert.equal(Onboarding.nextIndex(0), 1);
  assert.equal(Onboarding.nextIndex(1), 2);
  assert.equal(Onboarding.nextIndex(2), 2);
  assert.equal(Onboarding.previousIndex(2), 1);
  assert.equal(Onboarding.previousIndex(1), 0);
  assert.equal(Onboarding.previousIndex(0), 0);
  for (const bad of [-5, 99, NaN, undefined, null, '1']) {
    const p = Onboarding.progress(bad);
    assert.ok(p.index >= 0 && p.index <= 2, String(bad));
  }
});

test('adding a fourth step needs no change to the progress, forward or back logic', () => {
  const steps = [...Onboarding.STEPS, { id: 'genres', title: '喜歡的類型' }];
  assert.equal(Onboarding.progress(0, steps).segments.length, 4);
  assert.equal(Onboarding.progress(0, steps).label, '第 1 步，共 4 步');
  assert.equal(Onboarding.progress(2, steps).isLast, false);
  assert.equal(Onboarding.progress(2, steps).primaryLabel, '下一步');
  assert.equal(Onboarding.progress(3, steps).isLast, true);
  assert.equal(Onboarding.progress(3, steps).primaryLabel, '開始');
  assert.deepEqual(Onboarding.progress(3, steps).segments, [true, true, true, true]);
  assert.equal(Onboarding.nextIndex(2, steps), 3);
  assert.equal(Onboarding.nextIndex(3, steps), 3);
  assert.equal(Onboarding.previousIndex(3, steps), 2);
  assert.deepEqual(Onboarding.canAdvance('genres', {}, false), { ok: true, reason: '' });         // 沒有定義條件的步驟一律可前進
});

// ---- 各步驟的可前進條件 --------------------------------------------------------------------------

test('step 1 needs a nickname and an acknowledged guideline, and says which one is missing', () => {
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: '' }, false), { ok: false, reason: '請輸入暱稱。' });
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: '   ' }, true), { ok: false, reason: '請輸入暱稱。' });
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: '天天' }, false), { ok: false, reason: '請先閱讀並確認使用守則。' });
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: '天天' }, undefined), { ok: false, reason: '請先閱讀並確認使用守則。' });
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: '天天' }, 'yes'), { ok: false, reason: '請先閱讀並確認使用守則。' });   // 只有明確的 true 才算
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: ' 天 ' }, true), { ok: true, reason: '' });
  assert.deepEqual(Onboarding.canAdvance('welcome', undefined, true), { ok: false, reason: '請輸入暱稱。' });
  assert.deepEqual(Onboarding.canAdvance('welcome', { nickname: 5 }, true), { ok: false, reason: '請輸入暱稱。' });
});

test('step 1 does not judge the nickname format: the server alone decides', () => {
  for (const odd of ['a', 'https://x.example', '0912345678', '官方客服', 'x'.repeat(200)]) {
    assert.equal(Onboarding.canAdvance('welcome', { nickname: odd }, true).ok, true, odd);
  }
});

test('step 2 needs both the birth date and the age range and names the missing one', () => {
  assert.deepEqual(Onboarding.canAdvance('age', { birthDate: '', ageRange: '' }), { ok: false, reason: '請輸入生日。' });
  assert.deepEqual(Onboarding.canAdvance('age', { birthDate: '1995-05-05', ageRange: '' }), { ok: false, reason: '請選擇年齡區間。' });
  assert.deepEqual(Onboarding.canAdvance('age', { birthDate: '', ageRange: '25-34' }), { ok: false, reason: '請輸入生日。' });
  assert.deepEqual(Onboarding.canAdvance('age', { birthDate: '1995-05-05', ageRange: '25-34' }), { ok: true, reason: '' });
  assert.equal(Onboarding.canAdvance('age', undefined).ok, false);
});

test('step 3 can always advance, whatever is chosen', () => {
  assert.deepEqual(Onboarding.canAdvance('preference', {}, false), { ok: true, reason: '' });
  assert.deepEqual(Onboarding.canAdvance('preference', undefined, false), { ok: true, reason: '' });
});

test('the reasons are the same texts the form uses elsewhere', () => {
  assert.equal(Onboarding.TEXT.needNickname, Flows.TEXT.needNickname);
  assert.equal(Onboarding.TEXT.needBirthDate, Flows.TEXT.needBirthDate);
  assert.equal(Onboarding.TEXT.needAgeRange, Flows.TEXT.needAgeRange);
});

// ---- 錯誤回到對應的步驟 --------------------------------------------------------------------------

test('a nickname field error goes back to step 1', () => {
  const outcome = Flows.ageOutcome(server(400, 'validation_error', '輸入的內容不正確', { nickname: '暱稱不可包含網址、電子郵件或電話。' }));
  assert.equal(Onboarding.stepForFailure(outcome), 'welcome');
});

test('birth date and age range field errors go back to step 2', () => {
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(400, 'validation_error', 'x', { birth_date: '請輸入有效的生日。' }))), 'age');
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(400, 'validation_error', 'x', { age_range: '請選擇年齡區間。' }))), 'age');
});

test('age_mismatch goes back to step 2 where the cleared fields are', () => {
  const outcome = Flows.ageOutcome(server(400, 'age_mismatch', '資料有誤，請重新填寫。'));
  assert.deepEqual(outcome.clear, ['birthDate', 'ageRange']);
  assert.equal(Onboarding.stepForFailure(outcome), 'age');
});

test('errors in several steps go back to the earliest one', () => {
  const outcome = { action: 'fields', fields: { birth_date: 'x', nickname: 'y' } };
  assert.equal(Onboarding.stepForFailure(outcome), 'welcome');
});

test('the other failures stay where they are', () => {
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(400, 'age_not_eligible', 'x'))), null);          // 結束精靈
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(401, 'unauthorized', 'x'))), null);              // 導向登入頁
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(403, 'account_locked', 'x'))), null);            // 導向申訴頁
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome(server(500, 'internal_error', 'x'))), null);            // 留在最後一步
  assert.equal(Onboarding.stepForFailure(Flows.ageOutcome({ code: 'network', status: 0, message: 'x', fields: {} })), null);
  for (const odd of [undefined, null, 'x', 5, {}, { action: 'fields' }, { action: 'fields', fields: { unknown: 'x' } }, { clear: ['other'] }]) {
    assert.equal(Onboarding.stepForFailure(odd), null, JSON.stringify(odd));
  }
});

test('an error for a step the list no longer has changes nothing', () => {
  const steps = [{ id: 'welcome' }, { id: 'preference' }];
  assert.equal(Onboarding.stepForFailure({ action: 'fields', fields: { birth_date: 'x' } }, steps), null);
  assert.equal(Onboarding.stepForFailure({ action: 'fields', fields: { nickname: 'x' } }, steps), 'welcome');
});

// ---- 一次送出，成功之後才存故事偏好 --------------------------------------------------------------

test('a male preference is saved after the age confirmation and before the redirect', async () => {
  const { calls, flow } = rig();
  assert.equal(await flow.submit({ ...VALUES, storyPreference: 'male' }), 'done');
  assert.deepEqual(calls, [
    ['post', '/api/auth/verify-age', { birth_date: '1995-05-05', age_range: '25-34', nickname: '天天' }],
    ['put', '/api/account/story-preference', { story_preference: 'male' }],
    ['navigate', '/game']
  ]);
});

test('a female preference is saved the same way', async () => {
  const { calls, flow } = rig();
  await flow.submit({ ...VALUES, storyPreference: 'female' });
  assert.deepEqual(calls[1], ['put', '/api/account/story-preference', { story_preference: 'female' }]);
});

test('the default preference sends no preference request at all', async () => {
  const { calls, flow } = rig();
  assert.equal(await flow.submit(VALUES), 'done');
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'navigate']);
});

test('an unknown or missing preference value is never sent', async () => {
  for (const odd of [undefined, null, '', 'ALL', 'Male', 'both', 5, {}]) {
    const { calls, flow } = rig();
    await flow.submit({ ...VALUES, storyPreference: odd });
    assert.deepEqual(calls.map((c) => c[0]), ['post', 'navigate'], String(odd));
  }
});

test('the age confirmation request has exactly the old contract: three fields, nothing else', async () => {
  const { calls, flow } = rig();
  await flow.submit({ ...VALUES, storyPreference: 'male', acknowledged: true });
  assert.deepEqual(Object.keys(calls[0][2]).sort(), ['age_range', 'birth_date', 'nickname']);          // 沒有偏好、沒有「已確認使用守則」
  assert.deepEqual(calls[0][2], Flows.ageBody(VALUES));
});

test('a failed preference request does not stop the redirect or the result', async () => {
  const { calls, flow } = rig({ put: () => Promise.reject(server(500, 'internal_error', 'x')) });
  assert.equal(await flow.submit({ ...VALUES, storyPreference: 'male' }), 'done');
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'put', 'navigate']);
});

test('a preference request that throws synchronously is swallowed too', async () => {
  const { calls, flow } = rig({ put: () => { throw new Error('boom'); } });
  assert.equal(await flow.submit({ ...VALUES, storyPreference: 'female' }), 'done');
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'put', 'navigate']);
});

test('a preference request that never answers is cut off by the timeout and the redirect happens', async () => {
  const { calls, flow, timers } = rig({ put: () => new Promise(() => {}) });
  const pending = flow.submit({ ...VALUES, storyPreference: 'male' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'put']);                  // 還在等偏好，沒有導向
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, Onboarding.PREFERENCE_TIMEOUT_MS);
  timers[0].resolve();
  assert.equal(await pending, 'done');
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'put', 'navigate']);
});

test('no timer is started when there is no preference request', async () => {
  const { flow, timers } = rig();
  await flow.submit(VALUES);
  assert.equal(timers.length, 0);
});

test('a failed age confirmation rejects with the original error and sends no preference and no redirect', async () => {
  const failure = server(400, 'age_mismatch', '資料有誤，請重新填寫。');
  const { calls, flow } = rig({ post: () => Promise.reject(failure) });
  await assert.rejects(flow.submit({ ...VALUES, storyPreference: 'male' }), (error) => error === failure);
  assert.deepEqual(calls.map((c) => c[0]), ['post']);
});

test('every kind of age confirmation failure rejects without a preference or a redirect', async () => {
  for (const failure of [server(400, 'age_not_eligible', 'x'), server(401, 'unauthorized', 'x'), server(403, 'account_locked', 'x'), server(400, 'validation_error', 'x', { nickname: 'y' }), new Error('network')]) {
    const { calls, flow } = rig({ post: () => Promise.reject(failure) });
    await assert.rejects(flow.submit({ ...VALUES, storyPreference: 'female' }));
    assert.deepEqual(calls.map((c) => c[0]), ['post']);
  }
});

test('a second click while the first is still running sends nothing', async () => {
  let release;
  const { calls, flow } = rig({ post: () => new Promise((resolve) => { release = resolve; }) });
  const first = flow.submit(VALUES);
  assert.equal(flow.isSubmitting(), true);
  assert.equal(await flow.submit(VALUES), 'busy');
  assert.equal(await flow.submit({ ...VALUES, storyPreference: 'male' }), 'busy');
  assert.deepEqual(calls.map((c) => c[0]), ['post']);                            // 只有一個請求
  release({ data: {} });
  assert.equal(await first, 'done');
  assert.equal(flow.isSubmitting(), false);
});

test('the busy flag is released after a failure so the player can try again', async () => {
  let attempt = 0;
  const { calls, flow } = rig({ post: () => (++attempt === 1 ? Promise.reject(server(400, 'age_mismatch', 'x')) : Promise.resolve({ data: {} })) });
  await assert.rejects(flow.submit(VALUES));
  assert.equal(flow.isSubmitting(), false);
  assert.equal(await flow.submit(VALUES), 'done');
  assert.deepEqual(calls.map((c) => c[0]), ['post', 'post', 'navigate']);
});

test('the destination is exactly the one that was injected', async () => {
  const calls = [];
  const flow = Onboarding.createOnboarding({
    http: { post: () => Promise.resolve({}), put: () => Promise.resolve({}) }, navigate: (to) => calls.push(to), destination: '/cs', delay: () => new Promise(() => {}),
  });
  await flow.submit(VALUES);
  assert.deepEqual(calls, ['/cs']);
});

// ---- 離開與重新整理不留下資料 --------------------------------------------------------------------

test('the module never touches any browser storage or cookie, so nothing of the form can be left behind', () => {
  const code = codeOnly();
  for (const word of ['localStorage', 'sessionStorage', 'indexedDB', 'IndexedDB', 'document.cookie', 'document.', 'window.']) {
    assert.equal(code.includes(word), false, word);
  }
});

test('the module runs with no global browser objects at all', async () => {
  for (const name of ['window', 'document', 'localStorage', 'sessionStorage', 'indexedDB']) {
    assert.equal(typeof globalThis[name], 'undefined', name);
  }
  const { flow } = rig();
  assert.equal(await flow.submit({ ...VALUES, storyPreference: 'male' }), 'done');
});

test('the file has no CDN, v-html or Bootstrap script usage', () => {
  const code = codeOnly();
  for (const word of ['v-html', 'data-bs-', 'http://', 'https://', 'bootstrap.']) {
    assert.equal(code.includes(word), false, word);
  }
});
