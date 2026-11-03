'use strict';
/* auth-pages / csrf-protection 規格的前端部分：預設帶權杖標頭、片段權杖的讀取與移除、錯誤整理。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Auth = require('../../app/static/js/auth/auth-common.js');

function fakeDocument({ meta, stateText } = {}) {
  return {
    querySelector(selector) {
      assert.equal(selector, 'meta[name="csrf-token"]');
      return meta === undefined ? null : { getAttribute: (name) => (name === 'content' ? meta : null) };
    },
    getElementById(id) {
      assert.equal(id, 'initial-state');
      return stateText === undefined ? null : { textContent: stateText };
    }
  };
}

function fakeWindow(hash, pathname = '/reset-password', search = '') {
  const calls = [];
  return {
    calls,
    location: { hash, pathname, search },
    history: { replaceState: (...args) => calls.push(args) }
  };
}

// ---- axios 預設值 --------------------------------------------------------------

test('every axios request carries the CSRF token from the page and a 15 second timeout', () => {
  const axios = { defaults: { headers: { common: {} } } };
  Auth.configureAxios(axios, fakeDocument({ meta: 'signed-token-123' }));
  assert.equal(axios.defaults.headers.common['X-CSRF-Token'], 'signed-token-123');
  assert.equal(axios.defaults.timeout, 15000);
});

test('a page without a token meta tag yields an empty header value instead of throwing', () => {
  const axios = { defaults: { headers: { common: {} } } };
  Auth.configureAxios(axios, fakeDocument());
  assert.equal(axios.defaults.headers.common['X-CSRF-Token'], '');
});

// ---- 頁面初始狀態 --------------------------------------------------------------

test('the initial state is parsed as data and tolerates missing or broken JSON', () => {
  assert.deepEqual(Auth.readInitialState(fakeDocument({ stateText: '{"next":"/account","googleEnabled":true}' })), { next: '/account', googleEnabled: true });
  assert.deepEqual(Auth.readInitialState(fakeDocument()), {});
  assert.deepEqual(Auth.readInitialState(fakeDocument({ stateText: '{not json' })), {});
  assert.deepEqual(Auth.readInitialState(fakeDocument({ stateText: 'null' })), {});
  assert.deepEqual(Auth.readInitialState(fakeDocument({ stateText: '"text"' })), {});
});

test('script-looking content in the state is never executed, only parsed', () => {
  const state = Auth.readInitialState(fakeDocument({ stateText: '{"message":"<script>globalThis.pwned = true</script>"}' }));
  assert.equal(state.message, '<script>globalThis.pwned = true</script>');
  assert.equal(globalThis.pwned, undefined);
});

// ---- 片段權杖 -----------------------------------------------------------------

test('the token is read from the fragment and removed from the address bar', () => {
  const win = fakeWindow('#token=abc_DEF-123', '/reset-password', '?lang=zh');
  assert.equal(Auth.readFragmentToken(win), 'abc_DEF-123');
  assert.deepEqual(win.calls, [[null, '', '/reset-password?lang=zh']]);       // 片段被移除，路徑與查詢字串保留
});

test('a missing or malformed fragment yields an empty token but still cleans the address bar', () => {
  for (const hash of ['', '#', '#other=1', '#token=', '#token=bad token', '#token=<script>']) {
    const win = fakeWindow(hash);
    assert.equal(Auth.readFragmentToken(win), '', `hash ${JSON.stringify(hash)}`);
  }
  const win = fakeWindow('#other=1');
  Auth.readFragmentToken(win);
  assert.equal(win.calls.length, 1);
});

test('the token may follow other fragment parameters', () => {
  assert.equal(Auth.readFragmentToken(fakeWindow('#lang=zh&token=tok_en-9')), 'tok_en-9');
});

test('failing to rewrite the address bar does not lose the token', () => {
  const win = fakeWindow('#token=abc123');
  win.history.replaceState = () => { throw new Error('not allowed'); };
  assert.equal(Auth.readFragmentToken(win), 'abc123');
});

// ---- 錯誤整理 -----------------------------------------------------------------

function serverError(status, error) {
  return { response: { status, data: { error } } };
}

test('field errors from a 400 are returned per field as plain strings', () => {
  const info = Auth.describeError(serverError(400, {
    code: 'validation_error', message: '輸入的內容不正確，請檢查後再試。', fields: { password: '密碼長度須為 8 至 128 字。', email: 5 }
  }));
  assert.equal(info.kind, 'server');
  assert.equal(info.status, 400);
  assert.deepEqual(info.fields, { password: '密碼長度須為 8 至 128 字。', email: '5' });
  assert.equal(info.message, '輸入的內容不正確，請檢查後再試。');
});

test('a 401 keeps the generic server message and has no fields', () => {
  const info = Auth.describeError(serverError(401, { code: 'invalid_credentials', message: '信箱或密碼不正確，或登入嘗試過多而暫時鎖定，請稍後再試。' }));
  assert.equal(info.kind, 'server');
  assert.equal(info.code, 'invalid_credentials');
  assert.deepEqual(info.fields, {});
});

test('a CSRF failure is its own kind so the page can ask for a refresh', () => {
  const info = Auth.describeError(serverError(403, { code: 'csrf_failed', message: '頁面已過期或請求來源不正確，請重新整理頁面後再試。' }));
  assert.equal(info.kind, 'csrf');
  assert.match(info.message, /重新整理/);
});

test('a 403 that is not a CSRF failure stays a server error', () => {
  assert.equal(Auth.describeError(serverError(403, { code: 'account_disabled', message: '此帳號已停用，無法登入。' })).kind, 'server');
});

test('network failures and timeouts get their own Chinese messages', () => {
  const offline = Auth.describeError({ code: 'ERR_NETWORK', message: 'Network Error' });
  assert.equal(offline.kind, 'network');
  assert.equal(offline.message, Auth.MESSAGES.network);
  for (const code of ['ECONNABORTED', 'ETIMEDOUT']) {
    const info = Auth.describeError({ code });
    assert.equal(info.kind, 'timeout');
    assert.equal(info.message, Auth.MESSAGES.timeout);
  }
});

test('a cancelled request is recognised so the UI can stay quiet', () => {
  assert.equal(Auth.describeError({ code: 'ERR_CANCELED' }).kind, 'canceled');
  assert.equal(Auth.describeError({ name: 'CanceledError' }).kind, 'canceled');
});

test('an unexpected response body falls back to the generic message without leaking details', () => {
  for (const data of [undefined, null, '<html>502 Bad Gateway</html>', { detail: 'stack trace here' }]) {
    const info = Auth.describeError({ response: { status: 502, data } });
    assert.equal(info.kind, 'server');
    assert.equal(info.message, Auth.MESSAGES.unknown);
    assert.ok(!info.message.includes('stack'));
  }
});

test('describeError tolerates being called with nothing at all', () => {
  assert.equal(Auth.describeError(undefined).kind, 'network');
  assert.equal(Auth.describeError(null).kind, 'network');
});

test('the login redirect keeps the destination encoded', () => {
  assert.equal(Auth.loginUrlFor('/account?tab=security'), '/login?next=%2Faccount%3Ftab%3Dsecurity');
});
