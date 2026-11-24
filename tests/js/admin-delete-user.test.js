'use strict';
/* 後台使用者面板的「刪除帳號」（admin.js、console.html；見 admin-delete-account 規格「後台使用者面板的刪除帳號操作」）：
 * 以真的 Vue、真的 Auth 與真的 admin-format.js 在 `vm` 沙盒執行 admin.js 的 setup()（見 support/admin-sandbox.js），只把 axios、Toast 與 mount 換成替身。
 * 驗證：按鈕只對角色為 user 的帳號出現（含停權與待刪除），管理員沒有；確認視窗寫明不可復原與連帶刪除的資料並要求原因與密碼；
 * 送出的請求只有密碼與原因；成功後清空面板（不落入預設分支）、帳號在鎖定／暫停發佈／申訴清單的列消失、其餘清單標為過期；
 * 錯誤顯示在對應欄位且視窗保持開啟；密碼在送出當下清空；樣板的名稱都由 setup() 回傳，且沒有 v-html 與 data-bs-*。
 * 沒有瀏覽器可以掛載；真實的點擊流程在瀏覽器驗收（tasks 3.5）。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setupAdmin, fail, read, plain } = require('./support/admin-sandbox.js');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);
const tick = () => new Promise((resolve) => setImmediate(resolve));

const TARGET = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const DELETE_URL = `/api/admin/users/${TARGET}/delete`;

const player = (extra = {}) => ({ public_id: TARGET, display_name: '小明', email: 'player@example.com', role: 'user', status: 'active', tier: 'free', ...extra });
const admin = (extra = {}) => ({ public_id: OTHER, display_name: '另一位管理員', email: 'admin2@example.com', role: 'admin', status: 'active', tier: 'free', ...extra });

/* 預設的路由：刪除成功、總覽可讀；`remove` 可換成失敗。 */
function routes(remove) {
  return (request) => {
    if (request.method === 'post' && request.url === DELETE_URL) { return remove ? remove(request) : { ok: true }; }
    if (request.method === 'get' && request.url === '/api/admin/overview') { return { ok: true, overview: { warnings: {}, usage: {} } }; }
    return undefined;
  };
}

function open(target, remove) {
  const setup = setupAdmin(routes(remove));
  setup.page.user.value = target;
  return setup;
}

const posts = (setup) => setup.requests.filter((r) => r.method === 'post');

/* 讓成功後會被清空的清單都有內容，才看得出誰被清掉、誰被保留。 */
function seedLists(page) {
  page.lists.locked.items = [{ public_id: TARGET, display_name: '小明' }, { public_id: OTHER, display_name: '別人' }];
  page.lists.suspended.items = [{ public_id: TARGET, display_name: '小明' }, { public_id: OTHER, display_name: '別人' }];
  page.lists.appeals.items = [{ id: 'a1', user_public_id: TARGET, content: '申訴一' }, { id: 'a2', user_public_id: OTHER, content: '申訴二' }];
  for (const id of ['flagged', 'feedback', 'audit']) {
    page.lists[id].items = [{ id: `${id}-1` }];
    page.lists[id].loaded = true;
  }
}

// ---- 按鈕 --------------------------------------------------------------------------------------------

t('the delete action is offered for a player in any state and never for an administrator or when nobody is shown', () => {
  const { page } = setupAdmin(routes());
  assert.equal(page.userActions.value.delete, false);                       // 沒有查詢任何人
  for (const status of ['active', 'suspended', 'deletion_pending']) {
    page.user.value = player({ status });
    assert.equal(page.userActions.value.delete, true, `player ${status}`);
  }
  for (const status of ['active', 'suspended', 'deletion_pending']) {
    page.user.value = admin({ status });
    assert.equal(page.userActions.value.delete, false, `admin ${status}`);
  }
});

t('a pending-deletion player can only be deleted, and the note says so; an administrator gets the standard note', () => {
  const { page } = setupAdmin(routes());
  page.user.value = player({ status: 'deletion_pending' });
  const actions = plain(page.userActions.value);
  assert.equal(actions.delete, true);
  assert.equal(actions.suspend || actions.tier || actions.wallet, false);
  assert.match(page.userNote.value, /永久刪除/);
  page.user.value = admin();
  assert.equal(page.userNote.value, '管理員帳號無法在後台操作。');
  page.user.value = player();
  assert.equal(page.userNote.value, '');
});

t('asking to delete an administrator (or nobody) opens nothing and sends nothing', () => {
  const setup = setupAdmin(routes());
  setup.page.askDelete();
  assert.equal(setup.page.confirmOpen.value, false);
  setup.page.user.value = admin();
  setup.page.askDelete();
  assert.equal(setup.page.confirmOpen.value, false);
  assert.equal(setup.requests.length, 0);
});

// ---- 確認視窗 ----------------------------------------------------------------------------------------

t('the confirmation says it cannot be undone, lists what is deleted with it, and asks for a reason and the password', () => {
  const { page } = open(player());
  page.askDelete();
  assert.equal(page.confirmOpen.value, true);
  const spec = page.confirmSpec.value;
  assert.equal(spec.title, '永久刪除這個帳號？');
  assert.equal(spec.confirmText, '確認永久刪除');
  assert.equal(spec.code, 'ADMIN_DELETE_USER');
  assert.equal(spec.needsReason, true);
  assert.equal(page.confirmState.kind, 'delete');
  assert.equal(page.confirmState.targetLabel, '小明');
  assert.match(spec.intro, /無法復原/);
  for (const word of ['登入身分', '對話', '存檔', '記事簿', '遊戲幣', '全部劇本', '已上架', '檢舉', '申訴']) { assert.ok(spec.intro.includes(word), `警告沒有提到：${word}`); }
  assert.equal(spec.path(TARGET), `/users/${TARGET}/delete`);
  assert.equal(spec.done, '已刪除帳號。');
});

t('opening the confirmation starts with an empty reason and password every time', () => {
  const { page } = open(player());
  page.confirmState.reason = '舊原因';
  page.confirmState.password = '舊密碼';
  page.askDelete();
  assert.equal(page.confirmState.reason, '');
  assert.equal(page.confirmState.password, '');
});

t('without a reason or a password nothing is sent and each field gets its own message', async () => {
  const setup = open(player());
  setup.page.askDelete();
  await setup.page.submitConfirm();
  assert.equal(posts(setup).length, 0);
  assert.match(setup.page.confirmState.error.reason, /請填寫原因/);
  assert.equal(setup.page.confirmState.error.password, '請輸入您的密碼。');
  assert.equal(setup.page.confirmOpen.value, true);
  setup.page.confirmState.reason = 'x'.repeat(201);                          // 原因上限 200 字
  setup.page.confirmState.password = '密碼';
  await setup.page.submitConfirm();
  assert.equal(posts(setup).length, 0);
  assert.match(setup.page.confirmState.error.reason, /請填寫原因/);
});

// ---- 成功 --------------------------------------------------------------------------------------------

t('success posts only the password and the reason, closes the window, toasts, and clears the panel instead of showing the empty answer', async () => {
  const setup = open(player());
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '  違反內容規範  ';
  page.confirmState.password = '管理員密碼';
  await page.submitConfirm();
  assert.deepEqual(posts(setup).map((r) => [r.url, r.data]), [[DELETE_URL, { password: '管理員密碼', reason: '違反內容規範' }]]);
  assert.equal(page.confirmOpen.value, false);
  assert.deepEqual(setup.toasts, [{ message: '已刪除帳號。', type: 'info' }]);
  assert.equal(page.user.value, null);                                       // 成功回應沒有 user：不可落入預設分支而變成 undefined
  assert.equal(page.userActions.value.delete, false);
  assert.equal(page.userNote.value, '');
});

t('success removes the account from the locked, publish-suspended and appeal lists and keeps everyone else\'s rows', async () => {
  const setup = open(player());
  const { page } = setup;
  seedLists(page);
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  await page.submitConfirm();
  assert.deepEqual(plain(page.lists.locked.items).map((r) => r.public_id), [OTHER]);
  assert.deepEqual(plain(page.lists.suspended.items).map((r) => r.public_id), [OTHER]);
  assert.deepEqual(plain(page.lists.appeals.items).map((r) => r.user_public_id), [OTHER]);
});

t('success marks the flagged, feedback, audit and overview data stale so they reload next time (the deleted conversations are gone)', async () => {
  const setup = open(player());
  const { page } = setup;
  seedLists(page);
  await page.reloadOverview();
  assert.equal(page.overviewState.loaded, true);
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  await page.submitConfirm();
  for (const id of ['flagged', 'feedback', 'audit']) {
    assert.equal(page.lists[id].loaded, false, id);
    assert.deepEqual(plain(page.lists[id].items), [], id);
  }
  assert.equal(page.overviewState.loaded, false);
});

t('the password is cleared at the moment of sending, whatever the result', async () => {
  const setup = open(player());
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  const pending = page.submitConfirm();
  assert.equal(page.confirmState.password, '');                              // 請求還沒回來就已清空
  await pending;
  const failing = open(player(), () => fail(400, 'validation_error', '密碼不正確。', { password: '密碼不正確。' }));
  failing.page.askDelete();
  failing.page.confirmState.reason = '違反條款';
  failing.page.confirmState.password = '錯的密碼';
  const rejected = failing.page.submitConfirm();
  assert.equal(failing.page.confirmState.password, '');
  await rejected;
  assert.equal(failing.page.confirmState.password, '');
});

t('a second press while the request is in flight sends nothing more', async () => {
  const setup = open(player());
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  const first = page.submitConfirm();
  const second = page.submitConfirm();
  await Promise.all([first, second]);
  assert.equal(posts(setup).length, 1);
});

// ---- 失敗 --------------------------------------------------------------------------------------------

t('a wrong password shows the error under the password field, keeps the window open and the account on screen', async () => {
  const setup = open(player(), () => fail(400, 'validation_error', '密碼不正確。', { password: '密碼不正確。' }));
  const { page } = setup;
  seedLists(page);
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '錯的密碼';
  await page.submitConfirm();
  assert.equal(page.confirmState.error.password, '密碼不正確。');
  assert.equal(page.confirmState.error.reason, '');
  assert.equal(page.confirmOpen.value, true);
  assert.equal(page.confirmState.busy, false);                              // 按鈕可以再按
  assert.equal(page.user.value.public_id, TARGET);
  assert.equal(page.lists.locked.items.length, 2);
  assert.equal(page.lists.flagged.loaded, true);                            // 沒有刪成就不標為過期
  assert.deepEqual(setup.toasts, []);
});

t('a reason the server rejects is shown under the reason field', async () => {
  const setup = open(player(), () => fail(400, 'validation_error', '請填寫原因。', { reason: '原因不合格。' }));
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  await page.submitConfirm();
  assert.equal(page.confirmState.error.reason, '原因不合格。');
  assert.equal(page.confirmState.error.password, '');
  assert.equal(page.confirmOpen.value, true);
});

t('an administrator without a password sees the no_password message under the password field', async () => {
  const setup = open(player(), () => fail(400, 'no_password', '這個管理員帳號沒有設定密碼。'));
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  await page.submitConfirm();
  assert.equal(page.confirmState.error.password, '這個管理員帳號沒有設定密碼。');
});

t('the target having changed (409) or being an administrator (403) shows the message inside the window and changes nothing', async () => {
  for (const [status, code, message] of [[409, 'state_changed', '帳號狀態已改變，請重新整理。'], [403, 'admin_target_forbidden', '管理員帳號無法在後台操作。']]) {
    const setup = open(player(), () => fail(status, code, message));
    const { page } = setup;
    page.askDelete();
    page.confirmState.reason = '違反條款';
    page.confirmState.password = '密碼';
    await page.submitConfirm();
    assert.equal(page.confirmState.error.general, message, code);
    assert.equal(page.confirmOpen.value, true, code);
    assert.equal(page.user.value.public_id, TARGET, code);
  }
});

t('closing the window clears the reason, the password and the errors', async () => {
  const setup = open(player(), () => fail(400, 'validation_error', '密碼不正確。', { password: '密碼不正確。' }));
  const { page } = setup;
  page.askDelete();
  page.confirmState.reason = '違反條款';
  page.confirmState.password = '密碼';
  await page.submitConfirm();
  page.confirmOpen.value = false;
  await tick();
  assert.equal(page.confirmState.reason, '');
  assert.equal(page.confirmState.password, '');
  assert.equal(page.confirmState.error.password, '');
});

t('an expired session sends the administrator to the login page', async () => {
  const setup = open(player(), () => fail(401, 'unauthorized', '請重新登入。'));
  setup.page.askDelete();
  setup.page.confirmState.reason = '違反條款';
  setup.page.confirmState.password = '密碼';
  await setup.page.submitConfirm();
  assert.deepEqual(setup.navigations, ['/login?next=%2Fadmin']);
});

// ---- 樣板 --------------------------------------------------------------------------------------------

t('the user card has the delete button only behind the action flag, styled as a danger button, with no list or batch control', () => {
  const html = read('app/templates/admin/console.html');
  const card = html.slice(html.indexOf('<!-- ===== 使用者 ===== -->'), html.indexOf('<!-- ===== 模型失敗 ===== -->'));
  assert.ok(card.includes('<button v-if="userActions.delete" type="button" class="ui-btn admin-btn--danger" @click="askDelete">刪除帳號</button>'));
  assert.equal(card.split('askDelete').length - 1, 1);
  for (const forbidden of ['v-html', 'innerHTML', 'data-bs-', 'v-for="u in users']) { assert.ok(!html.includes(forbidden), forbidden); }
  const { page } = setupAdmin(routes());
  assert.equal(typeof page.askDelete, 'function');
  assert.ok('userActions' in page && 'userNote' in page);
});

t('the confirmation window is the shared one, so its text is plain interpolation and the reason box is shown for this kind', () => {
  const html = read('app/templates/admin/console.html');
  const modal = html.slice(html.indexOf('<!-- 危險操作共用的確認視窗'), html.indexOf('</ui-modal>', html.indexOf('<!-- 危險操作共用的確認視窗')));
  for (const expression of ['{{ confirmSpec.intro }}', '{{ confirmState.targetLabel }}', 'v-if="confirmSpec.needsReason"']) { assert.ok(modal.includes(expression), expression); }
  assert.ok(!modal.includes('v-html'));
});
