'use strict';
/* 劇本檢舉的純函式（script-report.js；見 script-reports 規格「劇本詳情頁提供檢舉入口」「檢舉理由是固定列舉」「自寫文字限 200 字且是純文字」）：
 * 理由清單與標籤、入口狀態的整理、字數（Unicode 字元，照伺服器的整理規則）、驗證、請求本文、送出後的狀態與每種失敗的畫面文字。
 * 與頁面的串接（Modal、請求、Toast）在 tests/js/script-detail.test.js；這裡只有不碰頁面的部分。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const report = require('../../app/static/js/game/script-report.js');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);
const VALUES = ['minor_sexual', 'sexual_violence', 'real_person_sexual', 'tier_mismatch', 'plagiarism', 'privacy_harassment', 'other'];

t('the seven reasons are fixed, in the fixed order, each with a Traditional Chinese label', () => {
  assert.deepEqual(report.reasons().map((item) => item.value), VALUES);
  for (const item of report.reasons()) {
    assert.match(item.label, /[一-鿿]/, item.value);
    assert.equal(report.reasonLabel(item.value), item.label);
  }
  assert.deepEqual(report.reasons().map((item) => item.label), [
    '涉及未成年的性內容', '性暴力或非自願情節', '以真人名義的性內容', '內容分級不符', '抄襲或侵權', '洩漏個資或騷擾', '其他',
  ]);
});

t('reasons() returns a copy, so changing it never changes the next call', () => {
  const first = report.reasons();
  first.pop();
  first[0].label = '被改掉了';
  assert.equal(report.reasons().length, 7);
  assert.equal(report.reasons()[0].label, '涉及未成年的性內容');
});

t('an unknown reason value gets the fixed fallback label, never the raw value and never a prototype key', () => {
  for (const value of ['spam', '', null, undefined, 7, '__proto__', 'constructor', 'toString']) {
    assert.equal(report.reasonLabel(value), '其他', String(value));
  }
});

t('stateOf keeps the three known states and treats everything else as unavailable (the entry stays hidden)', () => {
  for (const value of ['available', 'reported', 'unavailable']) { assert.equal(report.stateOf(value), value); }
  for (const value of ['', 'AVAILABLE', 'removed', null, undefined, 1, true, {}, [], '__proto__']) { assert.equal(report.stateOf(value), 'unavailable', String(value)); }
});

t('the text count is in Unicode characters after the same cleaning the server does', () => {
  assert.equal(report.countText(''), 0);
  assert.equal(report.countText('檢舉'), 2);
  assert.equal(report.countText('😀'.repeat(200)), 200);                                      // 一個表情符號是一個字元，不是兩個 UTF-16 單元
  assert.equal(report.countText('  前後空白  '), 4);
  assert.equal(report.countText('換\n行\t與\u0000控\u007f制\u0085字元'), 7);                     // 控制字元（含換行與 Tab）整理掉
  assert.equal(report.countText('é'), 1);                                               // NFC：e 加組合符號合成為一個字元
  assert.equal(report.countText(null), 0);
  assert.equal(report.countText(undefined), 0);
  assert.equal(report.countText(12345), 0);                                                   // 非字串視為沒有文字
});

t('validate needs one to seven known reasons and at most 200 characters, and says which part is wrong', () => {
  const none = report.validate([], '');
  assert.equal(none.ok, false);
  assert.equal(none.errors.reasons, '請至少選擇一個檢舉原因。');
  assert.equal(none.errors.text, undefined);

  const good = report.validate(['other'], '');
  assert.deepEqual(good, { ok: true, reasons: ['other'], text: '', length: 0, errors: {} });

  assert.equal(report.validate(VALUES, '字'.repeat(200)).ok, true);                           // 七個全選、恰好 200 字
  const long = report.validate(['other'], '字'.repeat(201));
  assert.equal(long.ok, false);
  assert.equal(long.errors.text, '補充說明至多 200 字，目前 201 字。');
  assert.equal(long.errors.reasons, undefined);

  const both = report.validate([], '字'.repeat(201));
  assert.deepEqual(Object.keys(both.errors).sort(), ['reasons', 'text']);
});

t('validate orders the chosen reasons by the fixed order and ignores unknown values, duplicates and non-arrays', () => {
  assert.deepEqual(report.validate(['other', 'minor_sexual', 'other', 'spam', 7, null], '').reasons, ['minor_sexual', 'other']);
  for (const bad of [null, undefined, 'other', 7, {}, ['spam'], [null]]) {
    assert.equal(report.validate(bad, '').ok, false, JSON.stringify(bad));
  }
});

t('requestBody has the reasons in the fixed order and the text only when it is not empty after cleaning', () => {
  assert.deepEqual(report.requestBody(['plagiarism', 'minor_sexual'], ''), { reasons: ['minor_sexual', 'plagiarism'] });
  assert.deepEqual(report.requestBody(['other'], ' \n\t '), { reasons: ['other'] });
  assert.deepEqual(report.requestBody(['other'], '  抄襲\n某作品 '), { reasons: ['other'], text: '抄襲某作品' });
  assert.deepEqual(Object.keys(report.requestBody(['other'], '字')).sort(), ['reasons', 'text']);
  assert.deepEqual(report.requestBody(['spam', 'other', 'other'], ''), { reasons: ['other'] });
});

t('stateAfter: success and 409 already_reported are "reported"; 404 and report_own_script hide the entry; the rest changes nothing', () => {
  assert.equal(report.stateAfter(null), 'reported');
  assert.equal(report.stateAfter(undefined), 'reported');
  assert.equal(report.stateAfter({ status: 409, code: 'already_reported' }), 'reported');
  assert.equal(report.stateAfter({ status: 404, code: 'not_found' }), 'unavailable');
  assert.equal(report.stateAfter({ status: 403, code: 'report_own_script' }), 'unavailable');
  for (const failure of [
    { status: 400, code: 'validation_error' }, { status: 429, code: 'report_limit' }, { status: 401, code: 'unauthorized' },
    { status: 403, code: 'age_confirmation_required' }, { status: 403, code: 'account_locked' }, { status: 409, code: 'something_else' },
    { status: 500, code: '' }, { status: 0, code: 'network', kind: 'network' },
  ]) {
    assert.equal(report.stateAfter(failure), null, JSON.stringify(failure));
  }
});

t('failureText gives a fixed Traditional Chinese sentence per failure and never shows the raw server message', () => {
  const cases = [
    [{ status: 429, code: 'report_limit', message: '伺服器訊息' }, /今天的檢舉次數已用完/],
    [{ status: 409, code: 'already_reported', message: '伺服器訊息' }, /已經檢舉過/],
    [{ status: 404, code: 'not_found', message: '伺服器訊息' }, /找不到這個劇本/],
    [{ status: 403, code: 'report_own_script', message: '伺服器訊息' }, /不能檢舉自己的劇本/],
    [{ status: 403, code: 'age_confirmation_required', message: '伺服器訊息' }, /年齡確認/],
    [{ status: 0, code: 'network', kind: 'network', message: '伺服器訊息' }, /連線不穩/],
    [{ status: 0, code: 'timeout', kind: 'timeout', message: '伺服器訊息' }, /連線不穩/],
    [{ status: 500, code: 'internal', message: '伺服器訊息' }, /暫時無法送出檢舉/],
    [null, /暫時無法送出檢舉/],
    [undefined, /暫時無法送出檢舉/],
  ];
  for (const [failure, expected] of cases) {
    const text = report.failureText(failure);
    assert.match(text, expected, JSON.stringify(failure));
    assert.ok(!text.includes('伺服器訊息'), JSON.stringify(failure));
  }
});

t('a 400 repeats the field message the server named (reasons first, then text), or a fixed sentence without one', () => {
  assert.equal(report.failureText({ status: 400, code: 'validation_error', fields: { reasons: '請選擇 1 至 7 個檢舉原因，且不可重複。' } }), '請選擇 1 至 7 個檢舉原因，且不可重複。');
  assert.equal(report.failureText({ status: 400, code: 'validation_error', fields: { text: '檢舉說明至多 200 字。' } }), '檢舉說明至多 200 字。');
  assert.equal(report.failureText({ status: 400, code: 'validation_error', fields: { reasons: 'A', text: 'B' } }), 'A');
  for (const fields of [{}, null, undefined, { other: 'x' }, { reasons: '' }, { text: 7 }]) {
    assert.equal(report.failureText({ status: 400, code: 'validation_error', fields }), '檢舉的內容不符合規定，請檢查後再試。', JSON.stringify(fields));
  }
});

t('the copy is fixed text without any placeholder, and changing the returned copy does not change the next one', () => {
  const copy = report.copy();
  assert.equal(copy.code, 'REPORT_SCRIPT');
  assert.equal(copy.openButton, '檢舉');
  assert.equal(copy.reportedButton, '已檢舉');
  assert.match(copy.intro, /不會自動下架/);
  assert.match(copy.intro, /不會收到處理結果/);
  assert.match(copy.textHint, /不會送入模型/);
  for (const [key, value] of Object.entries(copy)) { assert.ok(typeof value === 'string' && value !== '' && !/[{}<>]/.test(value), key); }
  copy.title = '被改掉了';
  assert.equal(report.copy().title, '檢舉這個劇本');
});

t('the module touches no browser storage, network or page: it is pure', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const code = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'script-report.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  for (const forbidden of ['localStorage', 'sessionStorage', 'indexedDB', 'document.', 'window.', 'axios', 'fetch(', 'innerHTML', 'v-html', 'console.']) {
    assert.ok(!code.includes(forbidden), forbidden);
  }
});
