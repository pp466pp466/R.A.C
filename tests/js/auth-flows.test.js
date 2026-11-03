'use strict';
/* auth-pages 與 post-login-age-confirmation 規格的前端部分：登入頁、年齡確認頁、申訴頁與管理員密碼驗證頁的判斷邏輯。
 * 頁面腳本只負責收集輸入、呼叫 API、顯示結果；每一種錯誤代碼要怎麼處理都集中在 auth-flows.js，在這裡逐一驗證。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Auth = require('../../app/static/js/auth/auth-common.js');
const Flows = require('../../app/static/js/auth/auth-flows.js');

const server = (status, code, message, fields) => Auth.describeError({
  response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } }
});

// ---- 安全的導向位置 ------------------------------------------------------------------------------

test('only same-site paths are ever used as a destination', () => {
  assert.equal(Flows.safeDestination('/cs'), '/cs');
  assert.equal(Flows.safeDestination('/account?tab=security'), '/account?tab=security');
  for (const bad of ['https://evil.example/', '//evil.example', 'javascript:alert(1)', '/\\evil', '', null, undefined, 5, {}, ['/cs']]) {
    assert.equal(Flows.safeDestination(bad), '/cs', String(bad));
  }
  assert.equal(Flows.safeDestination('//x', '/age'), '/age');                    // 自訂後備
});

// ---- 登入頁 --------------------------------------------------------------------------------------

test('the Google start request carries only the terms flag and a safe next', () => {
  assert.deepEqual(Flows.googleStartBody(true, '/account'), { accept_terms: true, next: '/account' });
  assert.deepEqual(Flows.googleStartBody(false, undefined), { accept_terms: false, next: '/cs' });
  for (const notTrue of [false, 0, '', null, undefined, 'true', 1]) {
    assert.equal(Flows.googleStartBody(notTrue, '/cs').accept_terms, false, String(notTrue));   // 只有明確的 true 才算勾選
  }
  assert.deepEqual(Flows.googleStartBody(true, 'https://evil.example/'), { accept_terms: true, next: '/cs' });
  assert.deepEqual(Object.keys(Flows.googleStartBody(true, '/cs')).sort(), ['accept_terms', 'next']);   // 沒有生日、區間或任何個資
});

test('only https authorization URLs are followed', () => {
  assert.equal(Flows.isSafeProviderUrl('https://accounts.example/auth?state=x', 'http://localhost/login'), true);
  for (const bad of ['http://accounts.example/auth', 'javascript:alert(1)', 'data:text/html,x', '/relative/only', '', null, undefined]) {
    assert.equal(Flows.isSafeProviderUrl(bad), false, String(bad));
  }
});

test('a failed Google start shows the server message or a fixed fallback, never an empty message', () => {
  assert.equal(Flows.googleStartOutcome(server(404, 'not_found', '找不到您要的內容。')).message, Flows.TEXT.googleUnavailable);
  assert.equal(Flows.googleStartOutcome(server(400, 'validation_error', '輸入不正確')).message, '輸入不正確');
  assert.equal(Flows.googleStartOutcome(undefined).message, Flows.TEXT.googleUnavailable);
});

// ---- 年齡確認頁 ----------------------------------------------------------------------------------

test('both age fields are required before a request is sent', () => {
  assert.deepEqual(Flows.validateAge({ birthDate: '', ageRange: '' }), { birth_date: '請輸入生日。', age_range: '請選擇年齡區間。' });
  assert.deepEqual(Flows.validateAge({ birthDate: '1996-10-27', ageRange: '' }), { age_range: '請選擇年齡區間。' });
  assert.deepEqual(Flows.validateAge({ birthDate: '', ageRange: '25_34' }), { birth_date: '請輸入生日。' });
  assert.deepEqual(Flows.validateAge({ birthDate: '1996-10-27', ageRange: '25_34' }), {});
  assert.deepEqual(Flows.validateAge(undefined), { birth_date: '請輸入生日。', age_range: '請選擇年齡區間。' });
});

test('the age request carries exactly the birthday and the range', () => {
  assert.deepEqual(Flows.ageBody({ birthDate: '1996-10-27', ageRange: '25_34', extra: 'x' }), { birth_date: '1996-10-27', age_range: '25_34' });
});

test('age_not_eligible ends the flow with the server message and a link back to the login page', () => {
  const outcome = Flows.ageOutcome(server(400, 'age_not_eligible', '本服務僅供 13 歲以上使用，您的帳號與相關資料已刪除。'));
  assert.deepEqual(outcome, { action: 'final', message: '本服務僅供 13 歲以上使用，您的帳號與相關資料已刪除。', to: '/login' });
  assert.equal(Flows.ageOutcome({ code: 'age_not_eligible', status: 400 }).message, Flows.TEXT.ageErased);     // 沒有訊息時的後備
});

test('age_mismatch gets the generic message and clears both fields without saying which one is wrong', () => {
  const outcome = Flows.ageOutcome(server(400, 'age_mismatch', '資料有誤，請重新填寫。'));
  assert.deepEqual(outcome, { action: 'message', message: '資料有誤，請重新填寫。', clear: ['birthDate', 'ageRange'] });
  assert.ok(!/生日|區間/.test(outcome.message));
});

test('field errors are shown beside the fields and nothing is cleared', () => {
  const outcome = Flows.ageOutcome(server(400, 'validation_error', '輸入的內容不正確', { birth_date: '請輸入有效的生日。' }));
  assert.deepEqual(outcome, { action: 'fields', fields: { birth_date: '請輸入有效的生日。' } });
});

test('a lost login goes to the login page and keeps the age page as the destination', () => {
  assert.deepEqual(Flows.ageOutcome(server(401, 'unauthorized', '請先登入。')), { action: 'redirect', to: '/login?next=%2Fage' });
});

test('a lock that happens while filling in the age goes to the appeal page', () => {
  assert.deepEqual(Flows.ageOutcome(server(403, 'account_locked', '您的帳號已被鎖定。')), { action: 'redirect', to: '/appeal' });
});

test('any other error shows its message; network failures keep the message the form already toasted', () => {
  assert.equal(Flows.ageOutcome(server(500, 'internal_error', '發生錯誤，請稍後再試。')).action, 'message');
  assert.equal(Flows.ageOutcome({ code: 'network', status: 0, message: Auth.MESSAGES.network, fields: {} }).message, Auth.MESSAGES.network);
  assert.equal(Flows.ageOutcome(undefined).action, 'message');
});

// ---- 申訴頁 --------------------------------------------------------------------------------------

test('the counter counts Unicode characters the way the server does', () => {
  assert.deepEqual(Flows.appealCounter('', 1000), { used: 0, max: 1000, text: '0 / 1000', over: false });
  assert.equal(Flows.appealCounter('字'.repeat(1000), 1000).over, false);
  assert.equal(Flows.appealCounter('字'.repeat(1001), 1000).over, true);
  assert.equal(Flows.charCount('😀'.repeat(3)), 3);                               // 一個表情符號算一個字，不是兩個 UTF-16 單位
  assert.equal(Flows.charCount(undefined), 0);
  assert.equal(Flows.appealCounter('abc', 1000).text, '3 / 1000');
});

test('an empty, blank or too long appeal is refused before a request is sent', () => {
  for (const blank of ['', '   ', '\n\t ', null, undefined, 5]) {
    assert.deepEqual(Flows.validateAppeal(blank, 1000), { content: '請輸入申訴內容。' }, String(blank));
  }
  assert.deepEqual(Flows.validateAppeal('字'.repeat(1001), 1000), { content: '申訴內容最多 1000 字。' });
  assert.deepEqual(Flows.validateAppeal('字'.repeat(1000), 1000), {});
  assert.deepEqual(Flows.validateAppeal('  說明情況  ', 1000), {});
});

test('the appeal status labels cover both states and fall back to a dash', () => {
  assert.equal(Flows.appealStatusLabel('pending'), '等待管理員處理');
  assert.equal(Flows.appealStatusLabel('handled'), '管理員已處理');
  assert.equal(Flows.appealStatusLabel('whatever'), '—');
});

test('appeal errors: not locked any more goes to the default page, 409 and 429 show the server message, 401 goes to login', () => {
  assert.deepEqual(Flows.appealOutcome(server(404, 'not_found', '找不到')), { action: 'redirect', to: '/cs' });
  assert.deepEqual(Flows.appealOutcome(server(401, 'unauthorized', '請先登入。')), { action: 'redirect', to: '/login?next=%2Fappeal' });
  assert.deepEqual(Flows.appealOutcome(server(409, 'appeal_pending', '您已有一則申訴正在等待管理員處理，請耐心等候。')),
    { action: 'message', message: '您已有一則申訴正在等待管理員處理，請耐心等候。' });
  assert.equal(Flows.appealOutcome(server(429, 'appeal_rate_limited', '一小時內最多只能送出 3 則申訴，請稍後再試。')).action, 'message');
  assert.deepEqual(Flows.appealOutcome(server(400, 'validation_error', '輸入不正確', { content: '請輸入申訴內容。' })),
    { action: 'fields', fields: { content: '請輸入申訴內容。' } });
});

// ---- 管理員的密碼驗證頁 --------------------------------------------------------------------------

test('the password is required before a request is sent', () => {
  assert.deepEqual(Flows.validateAdminPassword(''), { password: '請輸入密碼。' });
  assert.deepEqual(Flows.validateAdminPassword(undefined), { password: '請輸入密碼。' });
  assert.deepEqual(Flows.validateAdminPassword('x'), {});
});

test('a missing or expired step sends the admin back to Google through the login page with a reason', () => {
  assert.deepEqual(Flows.adminPasswordOutcome(server(401, 'unauthorized', '請先登入。')), { action: 'redirect', to: '/login?error=google_state' });
});

test('a wrong password and a locked account share one generic message and clear the password', () => {
  const generic = '信箱或密碼不正確，或登入嘗試過多而暫時鎖定，請稍後再試。';
  const outcome = Flows.adminPasswordOutcome(server(401, 'invalid_credentials', generic));
  assert.deepEqual(outcome, { action: 'message', message: generic, clear: ['password'] });
});

test('other password-step errors show their message and still clear the password', () => {
  const outcome = Flows.adminPasswordOutcome(server(500, 'internal_error', '發生錯誤，請稍後再試。'));
  assert.deepEqual(outcome, { action: 'message', message: '發生錯誤，請稍後再試。', clear: ['password'] });
  assert.deepEqual(Flows.adminPasswordOutcome(server(400, 'validation_error', '請輸入密碼。', { password: '請輸入密碼。' })),
    { action: 'fields', fields: { password: '請輸入密碼。' } });
});

test('the forgot-password button disappears when the server says it is unavailable', () => {
  const gone = Flows.adminForgotOutcome(server(404, 'not_found', '找不到'));
  assert.equal(gone.hideButton, true);
  assert.match(gone.message, /重新以 Google 登入/);
  const failed = Flows.adminForgotOutcome(server(500, 'internal_error', '發生錯誤，請稍後再試。'));
  assert.deepEqual(failed, { action: 'message', message: '發生錯誤，請稍後再試。' });
});

// ---- 訊息一律是純文字 ----------------------------------------------------------------------------

test('every fixed message is plain Chinese text with no markup', () => {
  for (const text of Object.values(Flows.TEXT)) {
    assert.ok(text.length > 0 && !/[<>]/.test(text) && /[一-鿿]/.test(text), text);
  }
});
