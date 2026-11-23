'use strict';
/* onboarding-wizard 規格的 Vue 接線（age.js）：以真的 Vue、真的 Auth 與真的 onboarding.js 在 `vm` 沙盒執行 `setup()`
 * （做法見 tests/js/support/age-sandbox.js），只把 axios、location、儲存物件與 mount 換成替身。
 * 涵蓋：步驟與進度、各步可前進的條件與說明、前進與後退保留欄位、使用守則的確認（只有確認動作才算）、一次送出與送出順序、
 * 偏好只在非預設時存、各種錯誤回到對應步驟（暱稱 → 第 1 步、生日與區間 → 第 2 步、不一致清空）、結束與導向、重複點擊只送一次、
 * finally 恢復；最後以沙盒的儲存物件與主控台斷言整個流程沒有寫入任何儲存、沒有輸出任何玩家文字。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setupAge, serverError, STATE } = require('./support/age-sandbox.js');

// 每個測試都有逾時：被破壞的程式若讓 Promise 永遠不完成，測試要失敗，不能掛住
const t = (name, fn) => test(name, { timeout: 5000 }, fn);
const plain = (value) => JSON.parse(JSON.stringify(value));       // 沙盒物件的原型與測試行程不同，比對前先 JSON 往返
const tick = () => new Promise((resolve) => setImmediate(resolve));

const step = (a) => a.page.progress.value.step;
const label = (a) => a.page.progress.value.label;
const gate = (a) => plain(a.page.gate.value);

/* 填完第 1 步（暱稱與確認守則）並前進。 */
async function passWelcome(a, nickname = '天天') {
  a.page.form.values.nickname = nickname;
  a.page.confirmGuide();
  await a.page.onPrimary();
}

async function passAge(a, birthDate = '1996-05-05', ageRange = '25_34') {
  a.page.form.values.birthDate = birthDate;
  a.page.form.values.ageRange = ageRange;
  await a.page.onPrimary();
}

// ---- 步驟與條件 ----------------------------------------------------------------------------------

t('the page starts on step 1 with the progress label, no back and the default preference', () => {
  const a = setupAge();
  assert.equal(step(a), 'welcome');
  assert.equal(label(a), '第 1 步，共 3 步');
  assert.equal(a.page.progress.value.isFirst, true);
  assert.equal(a.page.form.values.storyPreference, 'all');
  assert.equal(a.page.acknowledged.value, false);
  assert.equal(a.page.guideOpen.value, false);
  assert.deepEqual(plain(a.page.preferenceOptions.map((o) => o.value)), ['all', 'male', 'female']);
  assert.equal(a.page.ageRules.minRegisterAge, 18);
  assert.deepEqual(a.mounts, ['#app']);
});

t('the next button is blocked with a reason until the nickname and the guideline are done, and a blocked press does nothing', async () => {
  const a = setupAge();
  assert.deepEqual(gate(a), { ok: false, reason: '請輸入暱稱。' });
  await a.page.onPrimary();
  assert.equal(step(a), 'welcome');
  a.page.form.values.nickname = '天天';
  assert.deepEqual(gate(a), { ok: false, reason: '請先閱讀並確認使用守則。' });
  await a.page.onPrimary();
  assert.equal(step(a), 'welcome');
  a.page.confirmGuide();
  assert.deepEqual(gate(a), { ok: true, reason: '' });
  await a.page.onPrimary();
  assert.equal(step(a), 'age');
  assert.equal(label(a), '第 2 步，共 3 步');
});

t('the guideline is acknowledged only by the confirm action: opening it does not count', () => {
  const a = setupAge();
  a.page.openGuide();
  assert.equal(a.page.guideOpen.value, true);
  assert.equal(a.page.acknowledged.value, false);
  a.page.guideOpen.value = false;                                // 用 Esc 或「關閉」關掉
  assert.equal(a.page.acknowledged.value, false);
  a.page.confirmGuide();
  assert.equal(a.page.acknowledged.value, true);
});

t('step 2 names the missing field and moves on only when both are filled', async () => {
  const a = setupAge();
  await passWelcome(a);
  assert.deepEqual(gate(a), { ok: false, reason: '請輸入生日。' });
  a.page.form.values.birthDate = '1996-05-05';
  assert.deepEqual(gate(a), { ok: false, reason: '請選擇年齡區間。' });
  await a.page.onPrimary();
  assert.equal(step(a), 'age');
  a.page.form.values.ageRange = '25_34';
  await a.page.onPrimary();
  assert.equal(step(a), 'preference');
  assert.equal(label(a), '第 3 步，共 3 步');
  assert.equal(a.page.progress.value.primaryLabel, '開始');
  assert.deepEqual(gate(a), { ok: true, reason: '' });
});

t('going back keeps every field and the acknowledgement, and the first step has nowhere to go back to', async () => {
  const a = setupAge();
  await passWelcome(a);
  await passAge(a);
  a.page.goBack();
  assert.equal(step(a), 'age');
  assert.equal(a.page.form.values.birthDate, '1996-05-05');
  assert.equal(a.page.form.values.ageRange, '25_34');
  a.page.goBack();
  assert.equal(step(a), 'welcome');
  assert.equal(a.page.form.values.nickname, '天天');
  assert.equal(a.page.acknowledged.value, true);
  a.page.goBack();
  assert.equal(step(a), 'welcome');
  assert.equal(a.requests.length, 0);                              // 走到最後一步為止沒有任何請求
});

// ---- 一次送出 ------------------------------------------------------------------------------------

t('the last step sends the age confirmation once, then the male preference, then goes to the destination', async () => {
  const a = setupAge({ state: { ...STATE, next: '/cs' } });
  await passWelcome(a, '  天天 ');
  await passAge(a);
  a.page.form.values.storyPreference = 'male';
  assert.equal(await a.page.onPrimary(), true);
  assert.deepEqual(a.requests, [
    { method: 'POST', url: '/api/auth/verify-age', data: { birth_date: '1996-05-05', age_range: '25_34', nickname: '天天' } },
    { method: 'PUT', url: '/api/account/story-preference', data: { story_preference: 'male' } },
  ]);
  assert.deepEqual(a.navigations, ['/cs']);
});

t('the default preference sends no preference request', async () => {
  const a = setupAge();
  await passWelcome(a);
  await passAge(a);
  await a.page.onPrimary();
  assert.deepEqual(a.requests.map((r) => r.method + ' ' + r.url), ['POST /api/auth/verify-age']);
  assert.deepEqual(a.navigations, ['/game']);
});

t('an unsafe destination from the page state falls back to the default page', async () => {
  for (const bad of ['https://evil.example/', '//evil.example', 'javascript:alert(1)']) {
    const a = setupAge({ state: { ...STATE, next: bad } });
    await passWelcome(a);
    await passAge(a);
    await a.page.onPrimary();
    assert.deepEqual(a.navigations, ['/game'], bad);
  }
});

t('a failed preference request does not stop the redirect', async () => {
  const a = setupAge({ respond: (r) => (r.method === 'PUT' ? Promise.reject(serverError(500, 'internal_error', 'x')) : Promise.resolve({ data: {} })) });
  await passWelcome(a);
  await passAge(a);
  a.page.form.values.storyPreference = 'female';
  await a.page.onPrimary();
  assert.deepEqual(a.requests.map((r) => r.method), ['POST', 'PUT']);
  assert.deepEqual(a.navigations, ['/game']);
  assert.equal(a.page.form.state.submitting, false);
});

t('a second press while the request is running sends nothing and the busy flag is released afterwards', async () => {
  let release;
  const a = setupAge({ respond: (r) => (r.method === 'POST' ? new Promise((resolve) => { release = resolve; }) : Promise.resolve({ data: {} })) });
  await passWelcome(a);
  await passAge(a);
  const first = a.page.onPrimary();
  await tick();
  assert.equal(a.page.form.state.submitting, true);
  assert.equal(await a.page.onPrimary(), false);
  assert.equal(a.requests.filter((r) => r.method === 'POST').length, 1);
  release({ data: {} });
  assert.equal(await first, true);
  assert.equal(a.page.form.state.submitting, false);
});

t('submitting with an empty nickname never reaches the server and sends the player back to step 1', async () => {
  const a = setupAge();
  await passWelcome(a);
  await passAge(a);
  a.page.form.values.nickname = '   ';
  assert.equal(await a.page.onPrimary(), false);
  assert.equal(a.requests.length, 0);
  assert.equal(step(a), 'welcome');
  assert.equal(a.page.form.fieldErrors.nickname, '請輸入暱稱。');
});

t('submitting with a missing birth date sends the player back to step 2', async () => {
  const a = setupAge();
  await passWelcome(a);
  await passAge(a);
  a.page.form.values.birthDate = '';
  assert.equal(await a.page.onPrimary(), false);
  assert.equal(a.requests.length, 0);
  assert.equal(step(a), 'age');
  assert.equal(a.page.form.fieldErrors.birth_date, '請輸入生日。');
});

// ---- 錯誤回到對應的步驟 --------------------------------------------------------------------------

async function toLastStep(a) {
  await passWelcome(a);
  await passAge(a);
}

t('a nickname error goes back to step 1 with the message at the field and the other fields kept', async () => {
  const a = setupAge({ respond: () => Promise.reject(serverError(400, 'validation_error', '輸入的內容不正確', { nickname: '暱稱不可包含網址、電子郵件或電話。' })) });
  await toLastStep(a);
  assert.equal(await a.page.onPrimary(), false);
  assert.equal(step(a), 'welcome');
  assert.equal(a.page.form.fieldErrors.nickname, '暱稱不可包含網址、電子郵件或電話。');
  assert.equal(a.page.form.values.birthDate, '1996-05-05');
  assert.equal(a.page.form.values.ageRange, '25_34');
  assert.equal(a.page.acknowledged.value, true);
  assert.deepEqual(a.requests.map((r) => r.method), ['POST']);                  // 失敗後沒有偏好請求
  assert.deepEqual(a.navigations, []);
  assert.equal(a.page.form.state.submitting, false);
});

t('a birth date or age range field error goes back to step 2', async () => {
  for (const field of ['birth_date', 'age_range']) {
    const a = setupAge({ respond: () => Promise.reject(serverError(400, 'validation_error', 'x', { [field]: '請重新填寫。' })) });
    await toLastStep(a);
    await a.page.onPrimary();
    assert.equal(step(a), 'age', field);
    assert.equal(a.page.form.fieldErrors[field], '請重新填寫。');
    assert.equal(a.page.form.values.nickname, '天天');
  }
});

t('age_mismatch goes back to step 2, clears the birth date and range and shows the generic notice', async () => {
  const a = setupAge({ respond: () => Promise.reject(serverError(400, 'age_mismatch', '資料有誤，請重新填寫。')) });
  await toLastStep(a);
  await a.page.onPrimary();
  assert.equal(step(a), 'age');
  assert.equal(a.page.form.values.birthDate, '');
  assert.equal(a.page.form.values.ageRange, '');
  assert.equal(a.page.form.values.nickname, '天天');
  assert.equal(a.page.form.state.formError, '資料有誤，請重新填寫。');
  assert.deepEqual(gate(a), { ok: false, reason: '請輸入生日。' });            // 要重新填寫才能再往前
});

t('being under the minimum age ends the wizard with only the message and the link to the login page', async () => {
  const a = setupAge({ respond: () => Promise.reject(serverError(400, 'age_not_eligible', '本服務僅供 18 歲以上使用，您的帳號與相關資料已刪除。')) });
  await toLastStep(a);
  await a.page.onPrimary();
  assert.deepEqual(plain(a.page.finished.value), { message: '本服務僅供 18 歲以上使用，您的帳號與相關資料已刪除。', to: '/login' });
  assert.deepEqual(a.navigations, []);
  assert.deepEqual(a.requests.map((r) => r.method), ['POST']);
});

t('an expired login goes to the login page and a locked account to the appeal page', async () => {
  const expired = setupAge({ respond: () => Promise.reject(serverError(401, 'unauthorized', '請先登入。')) });
  await toLastStep(expired);
  await expired.page.onPrimary();
  assert.deepEqual(expired.navigations, ['/login?next=%2Fage']);
  const locked = setupAge({ respond: () => Promise.reject(serverError(403, 'account_locked', '您的帳號已被鎖定。')) });
  await toLastStep(locked);
  await locked.page.onPrimary();
  assert.deepEqual(locked.navigations, ['/appeal']);
});

t('any other failure stays on the last step with the message and lets the player try again', async () => {
  let attempt = 0;
  const a = setupAge({ respond: (r) => (r.method === 'POST' && ++attempt === 1 ? Promise.reject(serverError(500, 'internal_error', '發生錯誤，請稍後再試。')) : Promise.resolve({ data: {} })) });
  await toLastStep(a);
  assert.equal(await a.page.onPrimary(), false);
  assert.equal(step(a), 'preference');
  assert.equal(a.page.form.state.formError, '發生錯誤，請稍後再試。');
  assert.equal(await a.page.onPrimary(), true);
  assert.deepEqual(a.navigations, ['/game']);
});

t('fixing the nickname after the error lets the player go through the steps again and send', async () => {
  let attempt = 0;
  const a = setupAge({ respond: (r) => (r.method === 'POST' && ++attempt === 1 ? Promise.reject(serverError(400, 'validation_error', 'x', { nickname: '暱稱不合格。' })) : Promise.resolve({ data: {} })) });
  await toLastStep(a);
  await a.page.onPrimary();
  assert.equal(step(a), 'welcome');
  a.page.form.values.nickname = '新暱稱';
  await a.page.onPrimary();
  assert.equal(step(a), 'age');
  await a.page.onPrimary();
  assert.equal(step(a), 'preference');
  assert.equal(await a.page.onPrimary(), true);
  assert.deepEqual(a.requests.filter((r) => r.method === 'POST').map((r) => r.data.nickname), ['天天', '新暱稱']);
  assert.deepEqual(a.navigations, ['/game']);
});

// ---- 登出與零存取 --------------------------------------------------------------------------------

t('logging out posts to the logout endpoint and always ends on the login page', async () => {
  const a = setupAge({ respond: () => Promise.reject(new Error('down')) });
  await a.page.logout();
  assert.deepEqual(a.requests.map((r) => r.method + ' ' + r.url), ['POST /api/auth/logout']);
  assert.deepEqual(a.navigations, ['/login']);
});

t('the whole flow writes nothing to any browser storage or cookie and logs no player text', async () => {
  const a = setupAge();
  await passWelcome(a, '祕密暱稱');
  await passAge(a, '1996-05-05', '25_34');
  a.page.form.values.storyPreference = 'male';
  await a.page.onPrimary();
  assert.deepEqual(a.storageWrites, []);
  const everything = JSON.stringify([a.logs, a.toasts]);
  for (const secret of ['祕密暱稱', '1996-05-05']) {
    assert.equal(everything.includes(secret), false, secret);
  }
});

t('the acknowledgement is never part of a request', async () => {
  const a = setupAge();
  await passWelcome(a);
  await passAge(a);
  a.page.form.values.storyPreference = 'female';
  await a.page.onPrimary();
  const bodies = JSON.stringify(a.requests);
  assert.equal(bodies.includes('acknowledged'), false);
  assert.equal(bodies.includes('guideline'), false);
});
