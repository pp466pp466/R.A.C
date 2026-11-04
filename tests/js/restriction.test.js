'use strict';
/* appeal-shortcut 規格：申訴捷徑只依伺服器的回應判定，按鈕是指向 /appeal 的一般連結。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Restriction = require('../../app/static/js/ui/restriction.js');

test('a 403 account_locked response means locked', () => {
  assert.equal(Restriction.restrictionOf({ status: 403, code: 'account_locked' }), 'locked');
});

test('a 403 publish_suspended response means suspended', () => {
  assert.equal(Restriction.restrictionOf({ status: 403, code: 'publish_suspended' }), 'suspended');
});

test('other 403 codes and other statuses never show the shortcut', () => {
  for (const source of [
    { status: 403, code: 'csrf_failed' }, { status: 403, code: 'age_confirmation_required' }, { status: 403, code: 'forbidden' },
    { status: 404, code: 'account_locked' }, { status: 429, code: 'publish_suspended' }, { status: 0, code: 'network' }, { status: 403 },
  ]) {
    assert.equal(Restriction.restrictionOf(source), null, JSON.stringify(source));
  }
});

test('the profile from /api/auth/me is understood: restriction locked or publish_suspended true', () => {
  assert.equal(Restriction.restrictionOf({ restriction: 'locked', publish_suspended: false }), 'locked');
  assert.equal(Restriction.restrictionOf({ restriction: null, publish_suspended: true }), 'suspended');
  assert.equal(Restriction.restrictionOf({ restriction: 'age_pending' }), null);
  assert.equal(Restriction.restrictionOf({ restriction: null, publish_suspended: false }), null);
  assert.equal(Restriction.restrictionOf({ publish_suspended: 'true' }), null);           // 只認真正的布林
});

test('a locked account wins when both signals are present', () => {
  assert.equal(Restriction.restrictionOf({ restriction: 'locked', publish_suspended: true }), 'locked');
});

test('unrelated input gives null instead of throwing', () => {
  for (const source of [null, undefined, 0, '', 'locked', [], {}, true]) { assert.equal(Restriction.restrictionOf(source), null); }
});

test('nothing is remembered between calls: once the signal is gone the answer is null again', () => {
  assert.equal(Restriction.restrictionOf({ status: 403, code: 'account_locked' }), 'locked');
  assert.equal(Restriction.restrictionOf({ restriction: null, publish_suspended: false }), null);
});

test('the notice is Traditional Chinese plain text with a fixed link to the appeal page and no user data', () => {
  for (const kind of ['locked', 'suspended']) {
    const notice = Restriction.restrictionNoticeOf(kind);
    assert.equal(notice.href, '/appeal');
    assert.equal(notice.linkText, '前往申訴');
    assert.ok(/[一-鿿]/.test(notice.text) && notice.text.includes('申訴'));
    assert.ok(!/[<>]|\?|http/.test(notice.text) && !notice.href.includes('?'));
  }
  assert.ok(Restriction.restrictionNoticeOf('suspended').text.includes('其他功能照常'));
});

test('there is no notice for an unknown kind', () => {
  for (const kind of [null, undefined, '', 'banned', 'constructor', '__proto__']) { assert.equal(Restriction.restrictionNoticeOf(kind), null); }
});

test('the appeal path is a constant', () => {
  assert.equal(Restriction.APPEAL_PATH, '/appeal');
});
