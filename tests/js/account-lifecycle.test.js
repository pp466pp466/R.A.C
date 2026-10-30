'use strict';
/* account-deletion / personal-data-export 規格的前端部分：確認文字、送出條件、檔名、預定刪除時間、錯誤代碼說明、Blob 錯誤。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Lifecycle = require('../../app/static/js/auth/account-lifecycle.js');

const CONFIRM = '刪除我的帳號';

// ---- 確認文字 ----------------------------------------------------------------

test('the confirmation text must match exactly after trimming, including full-width spaces', () => {
  assert.equal(Lifecycle.matchesConfirmText(CONFIRM, CONFIRM), true);
  assert.equal(Lifecycle.matchesConfirmText('　' + CONFIRM + '　', CONFIRM), true);
  assert.equal(Lifecycle.matchesConfirmText('  ' + CONFIRM + '\n', CONFIRM), true);
});

test('NFC normalisation makes composed and decomposed forms equal', () => {
  const decomposed = 'é';          // e + 結合尖音符
  const composed = 'é';
  assert.equal(Lifecycle.matchesConfirmText(decomposed, composed), true);
});

test('any other text, type or empty expected value does not match', () => {
  for (const typed of ['', '刪除帳號', '刪除 我的帳號', '刪除我的帳號。', null, undefined, 123, ['x']]) {
    assert.equal(Lifecycle.matchesConfirmText(typed, CONFIRM), false, String(typed));
  }
  assert.equal(Lifecycle.matchesConfirmText(CONFIRM, ''), false);
  assert.equal(Lifecycle.matchesConfirmText(CONFIRM, undefined), false);
});

// ---- 送出條件與本文 ------------------------------------------------------------

test('an account with a password can submit only when a password was typed', () => {
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: true, password: '' }), false);
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: true, password: undefined }), false);
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: true, password: 'x', confirmText: '' }), true);
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: true, password: '', confirmText: CONFIRM, expectedText: CONFIRM }), false);
});

test('an account without a password can submit only with the exact text', () => {
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: false, confirmText: CONFIRM, expectedText: CONFIRM }), true);
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: false, confirmText: '刪除帳號', expectedText: CONFIRM }), false);
  assert.equal(Lifecycle.canSubmitDeletion({ hasPassword: false, password: 'x', confirmText: '', expectedText: CONFIRM }), false);
});

test('the request body carries only the field the server looks at', () => {
  assert.deepEqual(Lifecycle.deletionBody({ hasPassword: true, password: 'p', confirmText: CONFIRM }), { password: 'p' });
  assert.deepEqual(Lifecycle.deletionBody({ hasPassword: false, password: 'p', confirmText: CONFIRM }), { confirm_text: CONFIRM });
});

// ---- 匯出檔名 ----------------------------------------------------------------

test('the file name comes from Content-Disposition when it is a plain ASCII name', () => {
  assert.equal(Lifecycle.filenameFrom('attachment; filename=my-data-20261027.json', 'x.json'), 'my-data-20261027.json');
  assert.equal(Lifecycle.filenameFrom('attachment; filename="my-data-20261027.zip"', 'x.zip'), 'my-data-20261027.zip');
});

test('a missing, odd or path-like name falls back to the default', () => {
  for (const header of [undefined, null, '', 'attachment', 'attachment; filename=../../etc/passwd', 'attachment; filename="a b.json"', 'attachment; filename=' + 'a'.repeat(200)]) {
    assert.equal(Lifecycle.filenameFrom(header, 'my-data.json'), 'my-data.json', String(header));
  }
});

test('both export formats have a label, an extension and a fallback name', () => {
  assert.deepEqual(Object.keys(Lifecycle.EXPORT_FORMATS).sort(), ['csv', 'json']);
  for (const format of Object.values(Lifecycle.EXPORT_FORMATS)) {
    assert.ok(format.label && format.extension && format.fallbackName.endsWith('.' + format.extension));
  }
  assert.ok(Lifecycle.EXPORT_TIMEOUT_MS > 15000);
});

// ---- 預定刪除時間 ---------------------------------------------------------------

test('the scheduled time is shown in Taiwan time', () => {
  assert.equal(Lifecycle.formatScheduledAt('2026-11-03T12:00:00Z'), '2026 年 11 月 3 日 20:00（台灣時間）');
  assert.equal(Lifecycle.formatScheduledAt('2026-12-31T16:05:00Z'), '2027 年 1 月 1 日 00:05（台灣時間）');   // 跨年
});

test('an unparseable time shows a dash instead of throwing', () => {
  for (const value of [undefined, null, '', 'not-a-date', 123]) {
    assert.equal(Lifecycle.formatScheduledAt(value), '—', String(value));
  }
});

// ---- 錯誤說明 -------------------------------------------------------------------

test('every known lifecycle error code has a Traditional Chinese explanation', () => {
  for (const code of ['grace_expired', 'account_deletion_pending', 'admin_delete_forbidden', 'deletion_not_allowed', 'not_pending']) {
    const message = Lifecycle.describeLifecycleError({ code, message: '伺服器訊息' });
    assert.equal(message, Lifecycle.ERROR_MESSAGES[code]);
    assert.match(message, /[一-鿿]/);
  }
});

test('an unknown code falls back to the server message and then to a generic one', () => {
  assert.equal(Lifecycle.describeLifecycleError({ code: 'something_new', message: '新的伺服器訊息' }), '新的伺服器訊息');
  assert.equal(Lifecycle.describeLifecycleError({ code: 'something_new' }), '發生錯誤，請稍後再試。');
  assert.equal(Lifecycle.describeLifecycleError(null), '發生錯誤，請稍後再試。');
  assert.equal(Lifecycle.describeLifecycleError({ code: 'toString', message: 'x' }), 'x');          // 原型上的名稱不是錯誤代碼
});

// ---- Blob 錯誤 ------------------------------------------------------------------

test('an error whose body is a JSON blob is unwrapped so the shared error handling can read it', async () => {
  const body = { error: { code: 'validation_error', message: '格式必須是 json 或 csv。', fields: { format: '格式必須是 json 或 csv。' } } };
  const error = { response: { status: 400, data: { text: () => Promise.resolve(JSON.stringify(body)) } } };
  await assert.rejects(Lifecycle.unwrapBlobError(error), (rejected) => rejected === error);
  assert.deepEqual(error.response.data, body);
});

test('a blob that is not JSON, unreadable, or a plain error is passed through unchanged', async () => {
  const notJson = { response: { status: 500, data: { text: () => Promise.resolve('<html>boom</html>') } } };
  await assert.rejects(Lifecycle.unwrapBlobError(notJson), (rejected) => rejected === notJson);
  assert.equal(typeof notJson.response.data.text, 'function');

  const unreadable = { response: { data: { text: () => Promise.reject(new Error('讀取失敗')) } } };
  await assert.rejects(Lifecycle.unwrapBlobError(unreadable), (rejected) => rejected === unreadable);

  const network = { code: 'ERR_NETWORK' };
  await assert.rejects(Lifecycle.unwrapBlobError(network), (rejected) => rejected === network);
});
