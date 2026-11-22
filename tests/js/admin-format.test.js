// 管理員後台顯示邏輯的單元測試（純函式）。執行：node --test "tests/js/**/*.test.js"
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Format = require('../../app/static/js/admin/admin-format.js');

const STATUSES = ['active', 'suspended', 'deletion_pending'];
const TIERS = ['free', 'plus', 'vip'];
const ACTIONS = ['user.lookup', 'user.suspend', 'user.unsuspend', 'user.change_tier', 'conversation.read', 'conversation.delete', 'failures.read', 'reauth.failed',
  'account.lock', 'appeal.submit', 'appeal.read', 'account.unlock', 'limit.set', 'publish.suspend', 'publish.lift', 'wallet.adjust',
  'script.read', 'script.approve', 'script.reject', 'script.remove', 'script.restore', 'report.read', 'report.dismiss'];

test('every status, tier, role, action, kind and message value has a Chinese label', () => {
  for (const value of STATUSES) { assert.notEqual(Format.statusLabel(value), value, value); }
  for (const value of TIERS) { assert.notEqual(Format.tierLabel(value), value, value); }
  for (const value of ['user', 'admin']) { assert.notEqual(Format.roleLabel(value), value, value); }
  for (const value of ACTIONS) { assert.notEqual(Format.actionLabel(value), value, value); }
  for (const value of ['chat', 'summary', 'script_review', 'rpg_notebook']) { assert.notEqual(Format.kindLabel(value), value, value); }
  for (const value of ['user', 'assistant']) { assert.notEqual(Format.messageRoleLabel(value), value, value); }
  for (const value of ['ok', 'fallback']) { assert.notEqual(Format.messageStatusLabel(value), value, value); }
});

test('the labels cover exactly the known values and no others', () => {
  assert.deepEqual(Object.keys(Format.LABELS.status).sort(), [...STATUSES].sort());
  assert.deepEqual(Object.keys(Format.LABELS.tier).sort(), [...TIERS].sort());
  assert.deepEqual(Object.keys(Format.LABELS.action).sort(), [...ACTIONS].sort());
});

test('the tier labels match the ones the customer-service page shows', () => {
  assert.deepEqual([Format.tierLabel('free'), Format.tierLabel('plus'), Format.tierLabel('vip')], ['一般', '進階', '尊榮']);
});

test('an unknown value falls back to its raw code and an empty value to a dash, never throwing', () => {
  assert.equal(Format.statusLabel('brand_new_status'), 'brand_new_status');
  assert.equal(Format.actionLabel('user.teleport'), 'user.teleport');
  assert.equal(Format.tierLabel(42), '42');
  for (const empty of [null, undefined, '']) {
    assert.equal(Format.statusLabel(empty), '—');
    assert.equal(Format.actionLabel(empty), '—');
  }
  assert.equal(Format.statusLabel('toString'), 'toString');         // 不會取到物件原型上的屬性
  assert.equal(Format.actionLabel('constructor'), 'constructor');
});

test('dates are shown in Taipei time (UTC+8) with zero padding', () => {
  assert.equal(Format.taipeiDate('2026-10-29T04:50:00+00:00'), '2026-10-29');
  assert.equal(Format.taipeiDateTime('2026-10-29T04:50:00+00:00'), '2026-10-29 12:50');
  assert.equal(Format.taipeiDateTime('2026-10-29T00:05:00Z'), '2026-10-29 08:05');
  assert.equal(Format.taipeiClock('2026-10-29T00:05:00Z'), '08:05');
});

test('the day boundary is Taipei midnight, 16:00 UTC', () => {
  assert.equal(Format.taipeiDate('2026-10-29T15:59:59+00:00'), '2026-10-29');
  assert.equal(Format.taipeiDate('2026-10-29T16:00:00+00:00'), '2026-10-30');
  assert.equal(Format.taipeiDateTime('2026-10-29T15:59:00Z'), '2026-10-29 23:59');
  assert.equal(Format.taipeiDateTime('2026-10-29T16:00:00Z'), '2026-10-30 00:00');
  assert.equal(Format.taipeiDate('2026-12-31T16:00:00Z'), '2027-01-01');       // 跨年
});

test('a time with an explicit offset is converted, and one without any is read as UTC rather than the browser zone', () => {
  assert.equal(Format.taipeiDateTime('2026-10-29T12:50:00+08:00'), '2026-10-29 12:50');
  assert.equal(Format.taipeiDateTime('2026-10-29T04:50:00'), '2026-10-29 12:50');
});

test('an unparsable or missing time shows a dash', () => {
  for (const value of [null, undefined, '', 'not a date', 12345, {}]) {
    assert.equal(Format.taipeiDate(value), '—', String(value));
    assert.equal(Format.taipeiDateTime(value), '—', String(value));
    assert.equal(Format.taipeiClock(value), '—', String(value));
  }
});

test('the overview day label drops the year and shows a dash for anything else', () => {
  assert.equal(Format.shortDate('2026-10-29'), '10/29');
  assert.equal(Format.shortDate('2027-01-01'), '01/01');
  for (const value of [null, undefined, '', '2026/10/29', 'x', 20261029]) { assert.equal(Format.shortDate(value), '—', String(value)); }
});

test('the usage percentage rounds down so 100% only appears when the budget is really used up', () => {
  assert.equal(Format.ratioPercent(0), '0%');
  assert.equal(Format.ratioPercent(0.005), '0%');
  assert.equal(Format.ratioPercent(0.5), '50%');
  assert.equal(Format.ratioPercent(0.8), '80%');
  assert.equal(Format.ratioPercent(0.7999), '79%');
  assert.equal(Format.ratioPercent(0.998), '99%');
  assert.equal(Format.ratioPercent(1), '100%');
  assert.equal(Format.ratioPercent(1.0426), '104%');
});

test('the usage level changes at exactly 80% and 100%', () => {
  assert.equal(Format.ratioLevel(0), 'ok');
  assert.equal(Format.ratioLevel(0.7999), 'ok');
  assert.equal(Format.ratioLevel(0.8), 'warn');
  assert.equal(Format.ratioLevel(0.9999), 'warn');
  assert.equal(Format.ratioLevel(1), 'full');
  assert.equal(Format.ratioLevel(1.5), 'full');
});

test('the percentage and level are consistent with each other at the boundaries', () => {
  for (const ratio of [0.7999, 0.8, 0.9999, 1]) {
    const percent = parseInt(Format.ratioPercent(ratio), 10);
    const level = Format.ratioLevel(ratio);
    assert.equal(percent >= 100, level === 'full', String(ratio));
    assert.equal(percent >= 80 && percent < 100, level === 'warn', String(ratio));
  }
});

test('incomplete usage data is unknown, not zero or full', () => {
  for (const value of [null, undefined, '0.5', NaN, Infinity, -0.1, {}]) {
    assert.equal(Format.ratioPercent(value), '—', String(value));
    assert.equal(Format.ratioLevel(value), 'unknown', String(value));
  }
});

test('the bar width is clamped to 0 through 100', () => {
  assert.equal(Format.ratioWidth(0), 0);
  assert.equal(Format.ratioWidth(0.456), 45);
  assert.equal(Format.ratioWidth(1), 100);
  assert.equal(Format.ratioWidth(7), 100);
  for (const value of [null, undefined, -1, NaN, 'x']) { assert.equal(Format.ratioWidth(value), 0, String(value)); }
});

test('counts get thousands separators and bad data shows a dash', () => {
  assert.equal(Format.count(0), '0');
  assert.equal(Format.count(999), '999');
  assert.equal(Format.count(1000), '1,000');
  assert.equal(Format.count(1234567), '1,234,567');
  assert.equal(Format.count(-12345), '-12,345');
  for (const value of [null, undefined, '12', NaN, Infinity]) { assert.equal(Format.count(value), '—', String(value)); }
});

test('a public id is shortened to its first eight characters for tables', () => {
  assert.equal(Format.shortId('4d6f5c2a-1b3e-4f70-9a8c-0123456789ab'), '4d6f5c2a');
  for (const value of [null, undefined, '', 123]) { assert.equal(Format.shortId(value), '—', String(value)); }
});

test('the suspension note appears only while the suspension lasts, with the resume time in Taipei time', () => {
  const until = '2026-10-29T04:50:00+00:00';
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T04:00:00Z')), '客服對話暫停中，12:50 恢復');
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T04:50:00Z')), '');       // 到點就解除
  assert.equal(Format.suspensionText(until, Date.parse('2026-10-29T05:00:00Z')), '');
  for (const value of [null, undefined, '', 'nope']) { assert.equal(Format.suspensionText(value, 0), '', String(value)); }
});

test('audit details are turned into one readable line and never throw on odd shapes', () => {
  assert.equal(Format.auditDetailText('user.change_tier', { from_tier: 'free', to_tier: 'plus' }), '一般 → 進階');
  assert.equal(Format.auditDetailText('user.suspend', { from_status: 'active' }), '原狀態：使用中');
  assert.equal(Format.auditDetailText('user.unsuspend', { to_status: 'active' }), '還原為：使用中');
  assert.equal(Format.auditDetailText('user.lookup', { via: 'email', found: 1 }), '以信箱');
  assert.equal(Format.auditDetailText('user.lookup', { via: 'id', found: 0 }), '以識別碼，查無此人');
  assert.equal(Format.auditDetailText('failures.read', { count: 3 }), '3 筆');
  assert.equal(Format.auditDetailText('reauth.failed', { attempted: 'user.suspend' }), '嘗試：停權');
  assert.equal(Format.auditDetailText('reauth.failed', { attempted: 'account.unlock' }), '嘗試：解除帳號鎖定');
  assert.equal(Format.auditDetailText('account.lock', { reason: 'rate_limited' }), '原因：短時間內訊息過多');
  assert.equal(Format.auditDetailText('account.lock', { reason: 'identity_rejected' }), '原因：登入或年齡確認多次被拒');
  assert.equal(Format.auditDetailText('appeal.submit', { length: 42 }), '42 字');
  assert.equal(Format.auditDetailText('appeal.read', { count: 2 }), '2 則');
  assert.equal(Format.auditDetailText('account.unlock', {}), '');
  assert.equal(Format.lockReasonLabel('rate_limited'), '短時間內訊息過多');
  assert.equal(Format.lockReasonLabel('something_new'), 'something_new');          // 不認得的值退回原始代碼
  for (const detail of [null, undefined, {}, [], 'text', 5]) {
    for (const action of ACTIONS.concat(['something.else'])) {
      assert.equal(typeof Format.auditDetailText(action, detail), 'string', action + ' ' + JSON.stringify(detail));
    }
  }
  assert.equal(Format.auditDetailText('conversation.read', {}), '');
  assert.equal(Format.auditDetailText('something.else', { x: 1 }), '');
});

test('wallet adjustments get a Chinese action label, bucket labels and a readable detail line', () => {
  assert.equal(Format.actionLabel('wallet.adjust'), '調整遊戲幣');
  assert.equal(Format.bucketLabel('free'), '免費幣');
  assert.equal(Format.bucketLabel('paid'), '付費幣');
  assert.equal(Format.bucketLabel('gold'), 'gold');                                   // 不認得的值退回原始代碼
  assert.deepEqual(Object.keys(Format.LABELS.bucket).sort(), ['free', 'paid']);
  assert.equal(Format.auditDetailText('wallet.adjust', { bucket: 'paid', delta: 500, balance_after: 1500 }), '付費幣 +500，調整後 1,500');
  assert.equal(Format.auditDetailText('wallet.adjust', { bucket: 'free', delta: -30, balance_after: 170 }), '免費幣 -30，調整後 170');
  assert.equal(Format.auditDetailText('wallet.adjust', { bucket: 'free' }), '');
  assert.equal(Format.auditDetailText('wallet.adjust', null), '');
});

test('signed counts show a plus for gains and a minus for losses and a dash for bad data', () => {
  assert.equal(Format.signedCount(500), '+500');
  assert.equal(Format.signedCount(1234567), '+1,234,567');
  assert.equal(Format.signedCount(-30), '-30');
  assert.equal(Format.signedCount(0), '0');
  for (const bad of [null, undefined, 'x', NaN, Infinity]) { assert.equal(Format.signedCount(bad), Format.EMPTY); }
});

test('the coin amount an administrator types must be a non-zero whole number', () => {
  assert.equal(Format.parseCoinDelta('500'), 500);
  assert.equal(Format.parseCoinDelta('+500'), 500);
  assert.equal(Format.parseCoinDelta('-30'), -30);
  assert.equal(Format.parseCoinDelta('  12  '), 12);
  for (const bad of ['', '  ', '0', '-0', '+0', '1.5', '1,000', '１２', 'abc', '12a', '--5', '1e3', '1234567890', null, undefined, 12, {}]) {
    assert.equal(Format.parseCoinDelta(bad), null, JSON.stringify(bad));
  }
});

// ---- 劇本審核（rpg-script-publishing）------------------------------------------------------------------

const SCRIPT_STATUSES = ['draft', 'pending_review', 'published', 'rejected', 'removed', 'superseded'];
const CATEGORIES = ['minor_sexual', 'sexual_violence', 'non_consensual', 'real_person_sexual',
  'minor_suspected', 'real_person_suspected', 'tier_mismatch', 'other_concern', 'other_policy'];

test('every script status, remover, category, verdict, audience, ending mode and trigger kind has a Chinese label', () => {
  for (const value of SCRIPT_STATUSES) { assert.notEqual(Format.scriptStatusLabel(value), value, value); }
  for (const value of ['author', 'admin']) { assert.notEqual(Format.removerLabel(value), value, value); }
  for (const value of CATEGORIES) { assert.notEqual(Format.categoryLabel(value), value, value); }
  for (const value of ['pass', 'flag', 'reject']) { assert.notEqual(Format.verdictLabel(value), value, value); }
  for (const value of ['all', 'male', 'female']) { assert.notEqual(Format.audienceLabel(value), value, value); }
  assert.deepEqual(Object.keys(Format.LABELS.scriptStatus).sort(), [...SCRIPT_STATUSES].sort());
  assert.deepEqual(Object.keys(Format.LABELS.category), CATEGORIES);               // 順序固定：先四項紅線、再四項疑慮、最後是人工專用
  assert.equal(Format.categoryLabel('something_new'), 'something_new');           // 不認得的值退回原始代碼
  assert.equal(Format.scriptStatusLabel(null), '—');
});

test('the category dropdown lists every decision category once, in backend order, with other_policy last', () => {
  const options = Format.categoryOptions();
  assert.deepEqual(options.map((option) => option.value), CATEGORIES);
  assert.equal(options[options.length - 1].value, 'other_policy');
  for (const option of options) { assert.ok(option.label && option.label !== option.value, option.value); }
});

test('a list of categories becomes one line of Chinese and odd shapes become an empty string', () => {
  assert.equal(Format.categoriesText(['minor_suspected', 'other_concern']), '疑似涉及未成年角色、其他疑慮');
  assert.equal(Format.categoriesText(['brand_new']), 'brand_new');
  for (const bad of [null, undefined, 'minor_sexual', {}, [], ['', null, 5]]) { assert.equal(Format.categoriesText(bad), '', JSON.stringify(bad)); }
});

test('the state line of a list row says who removed it and why, and flags what the model was unsure about', () => {
  assert.equal(Format.scriptStateText({ status: 'pending_review', review_verdict: 'pass', review_categories: [] }), '待審');
  assert.equal(Format.scriptStateText({ status: 'pending_review', review_verdict: 'flag', review_categories: ['tier_mismatch'] }), '待審，有疑慮：內容不符合綁定的安全模式');
  assert.equal(Format.scriptStateText({ status: 'pending_review', review_verdict: 'flag', review_categories: [] }), '待審，有疑慮');
  assert.equal(Format.scriptStateText({ status: 'pending_review', review_verdict: null }), '待審');
  assert.equal(Format.scriptStateText({ status: 'published' }), '上架中');
  assert.equal(Format.scriptStateText({ status: 'removed', removed_by: 'author', decision_category: null }), '作者自行下架');
  assert.equal(Format.scriptStateText({ status: 'removed', removed_by: 'admin', decision_category: 'other_policy' }), '管理員下架（其他違反平台規範）');
  assert.equal(Format.scriptStateText({ status: 'removed', removed_by: null }), '已下架');
  assert.equal(Format.scriptStateText({ status: 'rejected', decision_category: 'sexual_violence' }), '已退回（性暴力）');
  for (const bad of [null, undefined, 5, 'x', []]) { assert.equal(typeof Format.scriptStateText(bad), 'string'); }
});

test('each row shows a time that matches its state, in Taipei time, and a dash when the time is unknown', () => {
  assert.equal(Format.scriptTimeText({ status: 'pending_review', submitted_at: '2026-11-20T04:50:00+00:00' }), '送審於 2026-11-20 12:50');
  assert.equal(Format.scriptTimeText({ status: 'published', published_at: '2026-11-20T16:00:00+00:00' }), '發佈於 2026-11-21 00:00');
  assert.equal(Format.scriptTimeText({ status: 'removed', decided_at: '2026-11-20T04:50:00+00:00' }), '下架於 2026-11-20 12:50');
  assert.equal(Format.scriptTimeText({ status: 'pending_review', submitted_at: null }), '送審於 —');
  assert.equal(Format.scriptTimeText({ status: 'draft' }), '');
  assert.equal(Format.scriptTimeText(null), '');
});

test('the action buttons follow the status: only pending can be approved, only non-builtin published versions can be removed, only admin takedowns can be restored', () => {
  assert.deepEqual(Format.scriptActions({ status: 'pending_review' }), { approve: true, reject: true, remove: false, restore: false, dismiss: false });
  assert.deepEqual(Format.scriptActions({ status: 'published', author_id: 'a1', is_builtin: false }), { approve: false, reject: false, remove: true, restore: false, dismiss: false });
  assert.equal(Format.scriptActions({ status: 'published', author_id: null, is_builtin: true }).remove, false);   // 內建劇本（種子鍵），後端回 409
  assert.equal(Format.scriptActions({ status: 'published', author_id: 'a1', is_builtin: true }).remove, false);
  // 匿名保留的劇本沒有作者，但不是內建劇本：有下架按鈕（「沒有作者」不等於內建）
  assert.equal(Format.scriptActions({ status: 'published', author_id: null, is_builtin: false }).remove, true);
  assert.equal(Format.scriptActions({ status: 'removed', author_id: null, is_builtin: false }).remove, false);
  assert.deepEqual(Format.scriptActions({ status: 'removed', removed_by: 'admin' }), { approve: false, reject: false, remove: false, restore: true, dismiss: false });
  assert.equal(Format.scriptActions({ status: 'removed', removed_by: 'author' }).restore, false);        // 作者自己的決定
  assert.equal(Format.scriptActions({ status: 'removed', removed_by: null }).restore, false);
  for (const status of ['draft', 'rejected', 'superseded']) {
    assert.deepEqual(Format.scriptActions({ status, author_id: 'a1', removed_by: 'admin' }), { approve: false, reject: false, remove: false, restore: false, dismiss: false }, status);
  }
  for (const bad of [null, undefined, 3, 'x']) {
    assert.deepEqual(Format.scriptActions(bad), { approve: false, reject: false, remove: false, restore: false, dismiss: false });
  }
});

test('the pending count on the tab is exact when the list is complete, "N+" when there may be more, and empty when nothing waits', () => {
  assert.equal(Format.pendingCountText(3, true), '3');
  assert.equal(Format.pendingCountText(50, false), '50+');
  assert.equal(Format.pendingCountText(0, true), '');
  assert.equal(Format.pendingCountText(0, false), '');
  for (const bad of [null, undefined, '3', NaN, Infinity, -1]) { assert.equal(Format.pendingCountText(bad, true), '', String(bad)); }
});

test('the review note limit counts Unicode characters after NFC, like the server', () => {
  assert.equal(Format.MAX_NOTE, 500);
  assert.equal(Format.noteLength('一二三'), 3);
  assert.equal(Format.noteLength('😀'.repeat(500)), 500);                          // 一個表情符號是一個字，不是兩個 UTF-16 單位
  assert.equal(Format.noteLength('é'), 1);                                   // NFC 之後合成為一個字
  assert.equal(Format.noteProblem('字'.repeat(500)), '');
  assert.equal(Format.noteProblem('字'.repeat(501)), '備註最多 500 字。');
  assert.equal(Format.noteProblem('😀'.repeat(500)), '');
  assert.equal(Format.noteProblem(''), '');
  for (const bad of [null, undefined, 5, {}]) { assert.equal(Format.noteLength(bad), 0); assert.equal(Format.noteProblem(bad), ''); }
});

test('the author is shown as pen name and short code, and a dash when there is no name', () => {
  assert.equal(Format.authorText({ author_name: '雲遊者', author_code: 'a1b2' }), '雲遊者#a1b2');
  assert.equal(Format.authorText({ author_name: '平台', author_code: '' }), '平台');
  assert.equal(Format.authorText({ author_name: '', author_code: 'a1b2' }), '—');
  assert.equal(Format.authorText(null), '—');
});

test('the safe-mode binding is described, and only "safe" counts as bound', () => {
  assert.equal(Format.tierBindingText('safe'), '綁定安全模式');
  for (const other of ['adult', 'standard', '', null, undefined]) { assert.equal(Format.tierBindingText(other), '未綁定（跟隨玩家的安全模式設定）', String(other)); }
});

test('the audit line of a script action shows short ids, the status and the category, and never any script text', () => {
  const detail = { version_id: '4d6f5c2a-1b3e-4f70-9a8c-0123456789ab', script_id: '9a8c0123-1b3e-4f70-9a8c-0123456789ab', status: 'published', category: 'sexual_violence' };
  assert.equal(Format.auditDetailText('script.remove', detail), '版本 4d6f5c2a，劇本 9a8c0123，狀態：上架中，類別：性暴力');
  assert.equal(Format.auditDetailText('script.read', { version_id: detail.version_id, script_id: detail.script_id, status: 'pending_review' }), '版本 4d6f5c2a，劇本 9a8c0123，狀態：待審');
  assert.equal(Format.auditDetailText('script.reject', { version_id: detail.version_id, script_id: detail.script_id, status: 'rejected', category: 'other_policy' }), '版本 4d6f5c2a，劇本 9a8c0123，狀態：已退回，類別：其他違反平台規範');
  assert.equal(Format.auditDetailText('script.approve', {}), '');
  assert.equal(Format.auditDetailText('script.restore', null), '');
  assert.equal(Format.auditDetailText('script.read', { version_id: 5, script_id: null, status: '' }), '');
});

test('the audit line of a script action ignores any field other than ids, status and category, so text can never leak into it', () => {
  const detail = {
    version_id: '4d6f5c2a-1b3e-4f70-9a8c-0123456789ab', script_id: '9a8c0123-1b3e-4f70-9a8c-0123456789ab', status: 'removed', category: 'other_policy',
    title: '秘密標題', summary: '秘密簡介', decision_note: '秘密備註', author_name: '秘密筆名', email: 'secret@example.com', content: { background: '秘密背景' }
  };
  const line = Format.auditDetailText('script.remove', detail);
  assert.equal(line, '版本 4d6f5c2a，劇本 9a8c0123，狀態：已下架，類別：其他違反平台規範');
  assert.ok(!line.includes('秘密') && !line.includes('secret'));
});

const FULL_CONTENT = {
  title: '<b>青雲</b>', summary: '一段簡介', world_name: '九州', tags: ['仙俠', '', '成長'], audience: 'male', content_tier: 'safe',
  calendar: { name: '青雲曆', start_year: 1200 }, background: '背景第一行\n背景第二行', rules: '規則內文', opening: '你睜開眼睛。',
  ending_mode: 'custom', ending_text: '壽終正寢。', ending_age: 90,
  characters: [
    { name: '林小雨', age: 17, occupation: '弟子', personality: '開朗', appearance: '短髮', speech_style: '直率', hidden_trait: '<script>alert(1)</script>' },
    { name: '無名' }
  ],
  locations: ['青雲宗', '', '落霞鎮'],
  milestones: [{ id: 'm1', title: '宗門大測', trigger: { kind: 'age', value: 20 }, note: '備註一' }, { id: 'm2', title: '大比', trigger: { kind: 'year', value: 1250 } }]
};

test('the full-text sections show every part of the content as plain text in a fixed order', () => {
  const sections = Format.scriptSections(FULL_CONTENT);
  assert.deepEqual(sections.map((section) => section.key), ['summary', 'world_name', 'tags', 'audience', 'content_tier', 'calendar', 'background', 'rules', 'ending', 'opening', 'characters', 'locations', 'milestones']);
  const by = Object.fromEntries(sections.map((section) => [section.key, section]));
  assert.equal(by.tags.text, '仙俠、成長');
  assert.equal(by.audience.text, '男頻');
  assert.equal(by.content_tier.text, '綁定安全模式');
  assert.equal(by.calendar.text, '青雲曆，起始年 1200');
  assert.equal(by.background.text, '背景第一行\n背景第二行');                      // 換行保留（頁面以 pre-wrap 顯示）
  assert.equal(by.ending.text, '自訂結局（壽終年齡 90 歲）\n壽終正寢。');
  assert.equal(by.characters.label, '角色（2）');
  assert.equal(by.characters.text, '姓名：林小雨　年齡：17 歲\n職業：弟子\n個性：開朗\n外貌：短髮\n說話風格：直率\n隱藏個性：<script>alert(1)</script>\n\n姓名：無名');
  assert.equal(by.locations.text, '青雲宗、落霞鎮');
  assert.equal(by.milestones.label, '里程碑（2）');
  assert.equal(by.milestones.text, '・宗門大測（年齡 20）\n　備註：備註一\n・大比（年份 1250）');
});

test('text with markup characters is kept literally, never interpreted or escaped by the formatter', () => {
  const sections = Format.scriptSections(FULL_CONTENT);
  const text = sections.map((section) => section.text).join('\n');
  assert.ok(text.includes('<script>alert(1)</script>'));
  for (const section of sections) { assert.equal(typeof section.text, 'string'); assert.equal(typeof section.label, 'string'); }
});

test('missing parts are skipped, a missing audience means everyone, and a plain ending says so', () => {
  const sections = Format.scriptSections({ summary: '只有簡介', ending_mode: 'none' });
  assert.deepEqual(sections.map((section) => section.key), ['summary', 'audience', 'ending']);
  assert.equal(sections[1].text, '全讀者');
  assert.equal(sections[2].text, '無結局');
});

test('content of an unexpected shape never throws and shows what can be read', () => {
  for (const bad of [null, undefined, 5, 'text', [], {}]) { assert.deepEqual(Format.scriptSections(bad), [], JSON.stringify(bad)); }
  const odd = Format.scriptSections({ characters: ['x', null, 5, { age: 'old' }], milestones: [null, 'x', { trigger: 'age' }], locations: 'not a list', tags: 'x', calendar: 'x', ending_mode: 'custom', ending_age: 'x' });
  assert.ok(Array.isArray(odd));
  for (const section of odd) { assert.equal(typeof section.text, 'string'); assert.ok(section.text.length > 0, section.key); }
  assert.equal(odd.find((section) => section.key === 'characters').label, '角色（4）');
});


// ---- 劇本檢舉（rpg-script-reports）-----------------------------------------------------------------------

const REASONS = ['minor_sexual', 'sexual_violence', 'real_person_sexual', 'tier_mismatch', 'plagiarism', 'privacy_harassment', 'other'];

test('the seven report reasons and the three closing results each have a Traditional Chinese label, and nothing else is in the tables', () => {
  assert.deepEqual(Object.keys(Format.LABELS.reportReason), REASONS);
  assert.deepEqual(Object.keys(Format.LABELS.reportResult).sort(), ['dismissed', 'removed', 'superseded']);
  for (const value of REASONS) { assert.notEqual(Format.reportReasonLabel(value), value, value); }
  for (const value of ['removed', 'dismissed', 'superseded']) { assert.notEqual(Format.reportResultLabel(value), value, value); }
  assert.equal(Format.reportResultLabel('dismissed'), '不成立');
  assert.equal(Format.reportResultLabel('superseded'), '被新版本取代');
});

test('the red-line reasons are the three sexual-content ones, and every one of them is a known reason', () => {
  assert.deepEqual(Format.RED_LINE_REASONS, ['minor_sexual', 'sexual_violence', 'real_person_sexual']);
  for (const value of Format.RED_LINE_REASONS) { assert.ok(REASONS.includes(value), value); }
});

test('an unknown reason or result shows its code as it is (like every other label here), nothing shows a dash, and a prototype key is never looked up as a label', () => {
  assert.equal(Format.reportReasonLabel('spam'), 'spam');
  assert.equal(Format.reportReasonLabel('__proto__'), '__proto__');
  assert.equal(Format.reportResultLabel('constructor'), 'constructor');
  for (const value of [null, undefined, '']) {
    assert.equal(Format.reportReasonLabel(value), Format.EMPTY);
    assert.equal(Format.reportResultLabel(value), Format.EMPTY);
  }
});

test('the report count text names the open reports and marks a red-line one; no open report gives nothing', () => {
  assert.equal(Format.reportCountText({ report_count: 3, red_line_reported: false }), '未結案檢舉 3 則');
  assert.equal(Format.reportCountText({ report_count: 1, red_line_reported: true }), '未結案檢舉 1 則（含紅線類別）');
  for (const item of [{ report_count: 0, red_line_reported: true }, { report_count: -1 }, { report_count: '2' }, {}, null, undefined, 'x', []]) {
    assert.equal(Format.reportCountText(item), '', JSON.stringify(item));
  }
  assert.equal(Format.reportCountText({ report_count: 2, red_line_reported: 'yes' }), '未結案檢舉 2 則');     // 只認布林 true
});

test('the reasons are joined with the Chinese labels; a non-list or a non-text entry is skipped', () => {
  assert.equal(Format.reportReasonsText(['minor_sexual', 'plagiarism']), '涉及未成年的性內容、抄襲或侵權');
  assert.equal(Format.reportReasonsText(['other', 5, null, '']), '其他');
  for (const bad of [null, undefined, 'other', {}, 7]) { assert.equal(Format.reportReasonsText(bad), '', JSON.stringify(bad)); }
});

test('report items keep the server order, mark open and closed ones, flag red lines, and show the text as plain text', () => {
  const rows = Format.reportItems([
    { reasons: ['plagiarism'], text: '<b>抄襲</b>\n第二行', created_at: '2026-11-20T04:30:00Z', closed_at: null, closed_result: null },
    { reasons: ['minor_sexual', 'other'], text: null, created_at: '2026-11-19T04:30:00Z', closed_at: '2026-11-21T00:00:00Z', closed_result: 'dismissed' },
  ]);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { open: true, statusText: '未結案', createdText: '檢舉於 2026-11-20 12:30', reasonsText: '抄襲或侵權', redLine: false, text: '<b>抄襲</b>\n第二行' });
  assert.equal(rows[1].open, false);
  assert.equal(rows[1].statusText, '已結案：不成立（2026-11-21 08:00）');
  assert.equal(rows[1].redLine, true);
  assert.equal(rows[1].text, '');
});

test('report items of an unexpected shape never throw and show what can be read', () => {
  for (const bad of [null, undefined, 'x', 5, {}]) { assert.deepEqual(Format.reportItems(bad), [], JSON.stringify(bad)); }
  const rows = Format.reportItems([null, 'x', 5, [], { reasons: 'plagiarism', text: 9, created_at: 'bad', closed_at: null }]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].reasonsText, '');
  assert.equal(rows[0].redLine, false);
  assert.equal(rows[0].text, '');
  assert.equal(typeof rows[0].createdText, 'string');
});

test('dismiss is offered only for a published version that has open reports', () => {
  assert.equal(Format.scriptActions({ status: 'published', report_count: 2 }).dismiss, true);
  for (const item of [{ status: 'published', report_count: 0 }, { status: 'published' }, { status: 'published', report_count: '2' }, { status: 'removed', report_count: 2 },
    { status: 'pending_review', report_count: 2 }, null, undefined]) {
    assert.equal(Format.scriptActions(item).dismiss, false, JSON.stringify(item));
  }
});

test('the audit text of a report read or dismissal shows the identifiers and the count and nothing else', () => {
  const detail = { version_id: '4d6f5c2a-1111-4111-8111-111111111111', script_id: '9a8c0123-2222-4222-8222-222222222222', count: 3 };
  assert.equal(Format.auditDetailText('report.read', detail), '版本 4d6f5c2a，劇本 9a8c0123，3 筆');
  assert.equal(Format.auditDetailText('report.dismiss', detail), '版本 4d6f5c2a，劇本 9a8c0123，結案 3 筆');
  assert.equal(Format.auditDetailText('report.read', { version_id: '4d6f5c2a-1111', count: 0 }), '版本 4d6f5c2a，0 筆');
  for (const action of ['report.read', 'report.dismiss']) {
    assert.equal(Format.auditDetailText(action, {}), '', action);
    assert.equal(Format.auditDetailText(action, null), '', action);
    assert.equal(Format.auditDetailText(action, { count: 'x' }), '', action);
  }
});

test('the empty-list text exists for the four script views and falls back for an unknown one', () => {
  assert.equal(Format.scriptEmptyText('reported'), '目前沒有被檢舉的劇本。');
  for (const view of ['pending_review', 'published', 'removed']) { assert.match(Format.scriptEmptyText(view), /^目前沒有.+的劇本。$/); }
  for (const view of ['__proto__', 'constructor', '', null, undefined, 'other']) { assert.equal(Format.scriptEmptyText(view), '目前沒有資料。', String(view)); }
});
