'use strict';
/* story-preference 規格的「帳號頁的故事偏好設定」：以真的 Vue 與真的 Auth.useForm 執行 account.js 的 setup()，
 * 只把 axios、Toast 與 mount 換成替身，驗證初始選取、儲存的請求、成功與失敗的處理，以及一律在 finally 恢復按鈕。
 * 沒有瀏覽器可以掛載；樣板是否真的引用這些名稱，由下方的樣板測試檢查。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const BASE_USER = { role: 'user', member_tier: 'free', age_confirmed: true, display_name: '玩家', email: 'a@example.com', story_preference: 'all' };

function serverError(status, code, message, fields) {
  return { response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } } };
}

/* 載入帳號頁需要的腳本並回傳 setup() 的結果；`put` 是 axios.put 的替身。 */
function setupAccount({ user = BASE_USER, put } = {}) {
  const toasts = [];
  const navigations = [];
  const sandbox = { console, Promise, setTimeout, clearTimeout };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/account', search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,                                       // auth-common.js 載入時會找 CSRF 的 meta
    getElementById: (id) => (id === 'initial-state'
      ? { textContent: JSON.stringify({ user, ageRules: {}, deletion: { confirmText: '刪除我的帳號' } }) } : null),
  };
  sandbox.axios = { put, post: () => Promise.reject(new Error('unexpected post')), defaults: { headers: { common: {} } } };
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });

  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, { createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; } });
  ['js/auth/auth-common.js', 'js/auth/auth-form.js', 'js/auth/auth-flows.js', 'js/auth/account-lifecycle.js', 'js/ui/restriction.js']
    .forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, {
    toast: (message, settings) => toasts.push({ message, type: settings && settings.type ? settings.type : 'info' }),
    install: () => {},
  });
  run('app/static/js/auth/account.js');
  assert.ok(options && typeof options.setup === 'function', 'account.js 沒有呼叫 createApp');
  return { vm: options.setup(), toasts, navigations };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ---- 初始選取 ---------------------------------------------------------------------------------------

test('the radios start on the preference the server sent', () => {
  for (const value of ['all', 'male', 'female']) {
    const { vm: page } = setupAccount({ user: { ...BASE_USER, story_preference: value }, put: () => Promise.reject(new Error('no request on load')) });
    assert.equal(page.storyForm.values.story_preference, value);
  }
});

test('a missing or unknown preference starts as all readers (nothing is guessed from other fields)', () => {
  for (const bad of [undefined, null, '', 'Female', 'boys', 1, ['male']]) {
    const { vm: page } = setupAccount({ user: { ...BASE_USER, story_preference: bad }, put: () => Promise.reject(new Error('no request on load')) });
    assert.equal(page.storyForm.values.story_preference, 'all', String(bad));
  }
});

// ---- 儲存成功 ---------------------------------------------------------------------------------------

test('saving sends exactly the chosen value with PUT, shows a toast and keeps the selection', async () => {
  const calls = [];
  const { vm: page, toasts } = setupAccount({ put: (url, body) => { calls.push([url, body]); return Promise.resolve({ data: { ok: true, story_preference: 'male' } }); } });
  page.storyForm.values.story_preference = 'male';
  const ok = await page.saveStoryPreference();
  assert.equal(ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['/api/account/story-preference', { story_preference: 'male' }]]);   // 請求本文來自沙盒，先轉成本地物件再比較
  assert.deepEqual(toasts, [{ message: '故事偏好已儲存。', type: 'info' }]);
  assert.equal(page.storyForm.values.story_preference, 'male');
  assert.equal(page.storyForm.state.submitting, false);
});

test('the value the server confirms is what stays selected', async () => {
  const { vm: page } = setupAccount({ put: () => Promise.resolve({ data: { ok: true, story_preference: 'female' } }) });
  page.storyForm.values.story_preference = 'male';
  await page.saveStoryPreference();
  assert.equal(page.storyForm.values.story_preference, 'female');
});

test('a malformed success body does not wipe the selection', async () => {
  for (const data of [undefined, null, {}, { story_preference: 'Male' }, { story_preference: 7 }]) {
    const { vm: page } = setupAccount({ put: () => Promise.resolve({ data }) });
    page.storyForm.values.story_preference = 'male';
    await page.saveStoryPreference();
    assert.equal(page.storyForm.values.story_preference, 'male', JSON.stringify(data));
  }
});

// ---- 儲存失敗：一律恢復按鈕、保留選取、不丟失玩家的選擇 -----------------------------------------------------------

test('a rejected value keeps the selection, shows the field error and one error toast, and re-enables the button', async () => {
  const { vm: page, toasts } = setupAccount({
    put: () => Promise.reject(serverError(400, 'validation_failed', '故事偏好必須是全讀者、男頻或女頻。', { story_preference: '故事偏好必須是全讀者、男頻或女頻。' })),
  });
  page.storyForm.values.story_preference = 'male';
  const ok = await page.saveStoryPreference();
  assert.equal(ok, false);
  assert.equal(page.storyForm.values.story_preference, 'male');
  assert.equal(page.storyForm.fieldErrors.story_preference, '故事偏好必須是全讀者、男頻或女頻。');
  assert.deepEqual(toasts.map((t) => t.type), ['error']);
  assert.equal(page.storyForm.state.submitting, false);
});

test('a server failure shows an error toast and keeps the selection', async () => {
  const { vm: page, toasts } = setupAccount({ put: () => Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')) });
  page.storyForm.values.story_preference = 'female';
  assert.equal(await page.saveStoryPreference(), false);
  assert.equal(page.storyForm.values.story_preference, 'female');
  assert.deepEqual(toasts, [{ message: '系統發生錯誤，請稍後再試。', type: 'error' }]);
  assert.equal(page.storyForm.state.submitting, false);
});

test('a network failure or a timeout shows exactly one error toast (the form helper already shows it)', async () => {
  for (const error of [new Error('Network Error'), Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }), serverError(403, 'csrf_failed', '頁面已過期，請重新整理。')]) {
    const { vm: page, toasts } = setupAccount({ put: () => Promise.reject(error) });
    page.storyForm.values.story_preference = 'male';
    assert.equal(await page.saveStoryPreference(), false);
    assert.equal(toasts.length, 1, JSON.stringify(toasts));
    assert.equal(toasts[0].type, 'error');
    assert.equal(page.storyForm.values.story_preference, 'male');
    assert.equal(page.storyForm.state.submitting, false);
  }
});

test('a session that expired sends the player to the login page and keeps the position', async () => {
  const { vm: page, navigations } = setupAccount({ put: () => Promise.reject(serverError(401, 'unauthenticated', '請先登入。')) });
  await page.saveStoryPreference();
  assert.deepEqual(navigations, ['/login?next=%2Faccount']);
  assert.equal(page.storyForm.state.submitting, false);
});

test('a locked account gets the appeal shortcut instead of a silent failure', async () => {
  const { vm: page } = setupAccount({ put: () => Promise.reject(serverError(403, 'account_locked', '您的帳號已被鎖定。')) });
  assert.equal(page.restrictionKind.value, null);
  await page.saveStoryPreference();
  assert.equal(page.restrictionKind.value, 'locked');
  assert.equal(page.storyForm.state.submitting, false);
});

// ---- 忙碌狀態 ---------------------------------------------------------------------------------------

test('while a save is in flight the button is busy and a second click sends nothing', async () => {
  let release;
  let requests = 0;
  const { vm: page } = setupAccount({ put: () => { requests += 1; return new Promise((resolve) => { release = () => resolve({ data: { story_preference: 'male' } }); }); } });
  page.storyForm.values.story_preference = 'male';
  const first = page.saveStoryPreference();
  await tick();
  assert.equal(page.storyForm.state.submitting, true);
  assert.equal(await page.saveStoryPreference(), false);            // 重複點擊被忽略
  assert.equal(requests, 1);
  release();
  assert.equal(await first, true);
  assert.equal(page.storyForm.state.submitting, false);             // 成功後恢復
});

test('after a failure the player can save again with the same selection', async () => {
  let attempt = 0;
  const { vm: page, toasts } = setupAccount({
    put: () => { attempt += 1; return attempt === 1 ? Promise.reject(new Error('Network Error')) : Promise.resolve({ data: { story_preference: 'female' } }); },
  });
  page.storyForm.values.story_preference = 'female';
  assert.equal(await page.saveStoryPreference(), false);
  assert.equal(await page.saveStoryPreference(), true);
  assert.deepEqual(toasts.map((t) => t.type), ['error', 'info']);
  assert.equal(page.storyForm.values.story_preference, 'female');
});

// ---- 樣板與腳本的對應 -----------------------------------------------------------------------------------

test('every name the story preference block uses is returned by setup(), and the block never reads other forms', () => {
  const html = read('app/templates/auth/account.html');
  const block = html.slice(html.indexOf('aria-labelledby="story-title"'), html.indexOf('aria-labelledby="security-title"'));
  assert.ok(block.length > 200, '找不到故事偏好區塊');
  const { vm: page } = setupAccount({ put: () => Promise.resolve({ data: {} }) });
  for (const name of ['storyForm', 'saveStoryPreference']) {
    assert.ok(block.includes(name), `樣板沒有用到 ${name}`);
    assert.ok(name in page, `setup() 沒有回傳 ${name}`);
  }
  for (const other of ['nicknameForm', 'ageForm', 'passwordForm', 'exportForm', 'deleteForm']) {
    assert.ok(!block.includes(other), `故事偏好區塊不應該用到 ${other}`);
  }
});
