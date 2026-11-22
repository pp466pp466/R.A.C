'use strict';
/* account-safe-mode-setting 規格的「帳號頁接線」：以真的 Vue 與真的 Auth.useForm 執行 account.js 的 setup()，
 * 只把 axios、Toast 與 mount 換成替身，驗證安全模式區塊的初始狀態、開關與同意 Modal 的流程、緊急煞車、失敗還原，
 * 以及樣板用到的每個名稱都由 setup() 回傳。沒有瀏覽器可以掛載；真實的點擊流程在瀏覽器驗收。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

const USER = { role: 'user', member_tier: 'free', age_confirmed: true, display_name: '玩家', email: 'a@example.com', story_preference: 'all' };
const RETURN = '/game/start/0b6f3a52-1c8e-4d57-9a41-7f2e6c1d8b90';
const ON = { effective: 'safe', confirmed: false, forced: false, returnPath: null };

function serverError(status, code, message, fields) {
  return { response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } } };
}

const answer = (effective, confirmed, forced = false) => ({ data: { ok: true, content_mode: { preference: effective, effective, confirmed, forced } } });

function setupAccount({ contentMode = ON, put, user = USER } = {}) {
  const toasts = [];
  const navigations = [];
  const timers = [];
  const sandbox = { console, Promise, clearTimeout };
  sandbox.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.location = { assign: (target) => navigations.push(target), hash: '', pathname: '/account', search: '' };
  sandbox.document = {
    createElement: () => ({}),
    querySelector: () => null,
    getElementById: (id) => (id === 'initial-state'
      ? { textContent: JSON.stringify({ user, ageRules: {}, deletion: { confirmText: '刪除我的帳號' }, contentMode }) } : null),
  };
  // 帳號頁啟動時會讀「匿名保留」偏好（account-script-retention.js）；這個檔案不測它，所以讀取一律失敗（區塊不顯示），其餘行為不受影響
  sandbox.axios = { get: () => Promise.reject(new Error('retention is not under test')), put, post: () => Promise.reject(new Error('unexpected post')), defaults: { headers: { common: {} } } };
  vm.createContext(sandbox);
  const run = (file) => vm.runInContext(read(file), sandbox, { filename: file });

  run('app/static/vendor/vue/vue.global.prod.js');
  let options = null;
  sandbox.Vue = Object.assign({}, sandbox.Vue, { createApp: (given) => { options = given; return { use() {}, component() {}, mount() {} }; } });
  ['js/auth/auth-common.js', 'js/auth/auth-form.js', 'js/auth/auth-flows.js', 'js/auth/account-lifecycle.js', 'js/auth/account-safe-mode.js', 'js/auth/account-script-retention.js', 'js/ui/restriction.js']
    .forEach((file) => run('app/static/' + file));
  sandbox.UI = Object.assign(sandbox.UI || {}, {
    toast: (message, settings) => toasts.push({ message, type: settings && settings.type ? settings.type : 'info' }),
    install: () => {},
  });
  run('app/static/js/auth/account.js');
  assert.ok(options && typeof options.setup === 'function', 'account.js 沒有呼叫 createApp');
  return { vm: options.setup(), toasts, navigations, timers };
}

const noRequest = () => Promise.reject(new Error('no request expected'));

// ---- 初始狀態 ---------------------------------------------------------------------------------------

test('the block is available for a confirmed member and shows the effective mode from the server state', () => {
  const { vm: page } = setupAccount({ put: noRequest });
  assert.equal(page.safeMode.available, true);
  assert.equal(page.safeMode.safe, true);
  assert.equal(page.safeMode.modalOpen, false);
  const off = setupAccount({ contentMode: { effective: 'adult', confirmed: true, forced: false, returnPath: null }, put: noRequest });
  assert.equal(off.vm.safeMode.safe, false);
});

test('without a state from the server (an admin who has not confirmed the age) the block is not available', () => {
  for (const contentMode of [null, {}, 'safe']) {
    const { vm: page } = setupAccount({ contentMode, put: noRequest });
    assert.equal(page.safeMode.available, false);
  }
});

test('the emergency brake shows the switch on and refuses to do anything', async () => {
  const { vm: page } = setupAccount({ contentMode: { effective: 'adult', confirmed: true, forced: true, returnPath: null }, put: noRequest });
  assert.equal(page.safeMode.safe, true);
  assert.equal(page.safeMode.forced, true);
  assert.equal(await page.toggleSafeMode(), false);
  assert.equal(page.safeMode.modalOpen, false);
});

// ---- 流程 -------------------------------------------------------------------------------------------

test('the first time off: modal, no request until the rules box is ticked, then one PUT with confirm_rules', async () => {
  const calls = [];
  const { vm: page, toasts } = setupAccount({ put: (url, body) => { calls.push([url, JSON.parse(JSON.stringify(body))]); return Promise.resolve(answer('adult', true)); } });
  await page.toggleSafeMode();
  assert.equal(page.safeMode.modalOpen, true);
  assert.equal(calls.length, 0);
  assert.equal(await page.confirmSafeMode(), false);                    // 沒勾選：確認鈕不送出
  assert.equal(calls.length, 0);
  page.safeMode.agreed = true;
  assert.equal(await page.confirmSafeMode(), true);
  assert.deepEqual(calls, [['/api/account/content-mode', { mode: 'adult', confirm_rules: true }]]);
  assert.equal(page.safeMode.safe, false);
  assert.equal(page.safeMode.modalOpen, false);
  assert.deepEqual(toasts, [{ message: '安全模式已關閉。', type: 'info' }]);
});

test('cancelling the modal sends nothing and leaves the switch on', async () => {
  const { vm: page } = setupAccount({ put: noRequest });
  await page.toggleSafeMode();
  page.safeMode.agreed = true;
  page.cancelSafeModeModal();
  assert.equal(page.safeMode.modalOpen, false);
  assert.equal(page.safeMode.agreed, false);
  assert.equal(page.safeMode.safe, true);
});

test('an agreed member turns it off in one click, and back on in one click', async () => {
  const bodies = [];
  const replies = [answer('adult', true), answer('safe', true)];
  const { vm: page } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: null },
    put: (url, body) => { bodies.push(JSON.parse(JSON.stringify(body))); return Promise.resolve(replies.shift()); },
  });
  assert.equal(await page.toggleSafeMode(), true);
  assert.equal(page.safeMode.safe, false);
  assert.equal(await page.toggleSafeMode(), true);
  assert.equal(page.safeMode.safe, true);
  assert.deepEqual(bodies, [{ mode: 'adult' }, { mode: 'safe' }]);
  assert.equal(page.safeMode.modalOpen, false);
});

test('while saving the switch is busy and a failure restores it', async () => {
  let release;
  const { vm: page } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: null },
    put: () => new Promise((resolve, reject) => { release = () => reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')); }),
  });
  const pending = page.toggleSafeMode();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(page.safeMode.busy, true);
  release();
  assert.equal(await pending, false);
  assert.equal(page.safeMode.busy, false);
  assert.equal(page.safeMode.safe, true);
  assert.equal(page.safeMode.error, '系統發生錯誤，請稍後再試。');
});

test('a valid return position sends the player back to the script after the toast', async () => {
  const { vm: page, navigations, timers } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: RETURN },
    put: () => Promise.resolve(answer('adult', true)),
  });
  assert.equal(page.safeMode.returnPath, RETURN);
  await page.toggleSafeMode();
  assert.deepEqual(navigations, []);
  assert.equal(timers.length, 1);
  timers[0].fn();
  assert.deepEqual(navigations, [RETURN]);
});

test('a malicious return position in the state is ignored by the page script', async () => {
  const { vm: page, navigations, timers } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: 'https://example.com/' },
    put: () => Promise.resolve(answer('adult', true)),
  });
  assert.equal(page.safeMode.returnPath, null);
  await page.toggleSafeMode();
  assert.equal(timers.length, 0);
  assert.deepEqual(navigations, []);
});

// ---- 失敗的共同處理 ---------------------------------------------------------------------------------

test('an expired session goes to the login page and keeps the position', async () => {
  const { vm: page, navigations } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: null },
    put: () => Promise.reject(serverError(401, 'unauthenticated', '請先登入。')),
  });
  await page.toggleSafeMode();
  assert.deepEqual(navigations, ['/login?next=%2Faccount']);
  assert.equal(page.safeMode.busy, false);
});

test('a locked account gets the appeal shortcut', async () => {
  const { vm: page } = setupAccount({
    contentMode: { effective: 'safe', confirmed: true, forced: false, returnPath: null },
    put: () => Promise.reject(serverError(403, 'account_locked', '您的帳號已被鎖定。')),
  });
  assert.equal(page.restrictionKind.value, null);
  await page.toggleSafeMode();
  assert.equal(page.restrictionKind.value, 'locked');
});

// ---- 樣板與腳本的對應 -------------------------------------------------------------------------------

test('every name the safe mode block and modal use is returned by setup(), and they never read the other forms', () => {
  const html = read('app/templates/auth/account.html');
  const start = html.indexOf('aria-labelledby="safe-mode-title"');
  const block = html.slice(start, html.indexOf('</section>', start));
  const modalStart = html.indexOf('v-model="safeMode.modalOpen"');
  const modal = html.slice(modalStart, html.indexOf('</ui-modal>', modalStart));
  assert.ok(block.length > 300 && modal.length > 300, '找不到安全模式區塊或 Modal');
  const { vm: page } = setupAccount({ put: noRequest });
  for (const name of ['safeMode', 'safeModeCopy', 'toggleSafeMode', 'confirmSafeMode', 'cancelSafeModeModal']) {
    assert.ok((block + modal).includes(name), `樣板沒有用到 ${name}`);
    assert.ok(name in page, `setup() 沒有回傳 ${name}`);
  }
  for (const other of ['nicknameForm', 'ageForm', 'passwordForm', 'exportForm', 'deleteForm', 'storyForm']) {
    assert.ok(!(block + modal).includes(other), `安全模式區塊不應該用到 ${other}`);
  }
  // 樣板讀的每個 safeMode.xxx 與 safeModeCopy.xxx 都存在
  for (const [, field] of (block + modal).matchAll(/safeMode\.(\w+)/g)) {
    assert.ok(field in page.safeMode, `safeMode.${field} 不存在`);
  }
  for (const [, field] of (block + modal).matchAll(/safeModeCopy\.(\w+)/g)) {
    assert.ok(field in page.safeModeCopy, `safeModeCopy.${field} 不存在`);
  }
});
