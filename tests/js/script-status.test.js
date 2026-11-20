'use strict';
/* script-takedown 規格的「作者看得到審核結果與下架狀態」的前端部分：版本列的狀態文字、被退回與下架的說明、按鈕規則與錯誤文案。
 * 全是純函式；資料只有版本摘要的 status、decision_category、decided_at、removed_by（後端保證不含審核備註）。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Draft = require('../../app/static/js/scripts/script-draft.js');
const Status = require('../../app/static/js/scripts/script-status.js');

const NOW = '2026-11-20T04:30:00+00:00';           // 台灣時間 2026-11-20 12:30

function version(fields) { return Object.assign({ status: 'published', decision_category: null, decided_at: null, removed_by: null }, fields); }

test('the time text is Taiwan time, whatever zone the server string names and whatever zone the browser is in', () => {
  assert.equal(Status.timeText(NOW), '2026-11-20 12:30');
  assert.equal(Status.timeText('2026-11-20T04:30:00Z'), '2026-11-20 12:30');
  assert.equal(Status.timeText('2026-11-20T04:30:00'), '2026-11-20 12:30');                    // 沒有時區視為 UTC，不是瀏覽器的本地時間
  assert.equal(Status.timeText('2026-11-20T20:30:00+00:00'), '2026-11-21 04:30');                // 跨日
  assert.equal(Status.timeText('2026-11-20T12:30:00+08:00'), '2026-11-20 12:30');
  for (const bad of [null, undefined, '', 'yesterday', 12, {}]) { assert.equal(Status.timeText(bad), '', String(bad)); }
});

test('the status chip tells who took a version down, and falls back to the plain label', () => {
  assert.equal(Status.statusText(version({ status: 'removed', removed_by: 'author' })), '已下架（作者）');
  assert.equal(Status.statusText(version({ status: 'removed', removed_by: 'admin' })), '已被管理員下架');
  assert.equal(Status.statusText(version({ status: 'removed' })), '已下架');                      // 下架者缺值：不猜
  assert.equal(Status.statusText(version({ status: 'removed', removed_by: 'someone' })), '已下架');
  for (const status of ['draft', 'pending_review', 'published', 'rejected', 'superseded']) {
    assert.equal(Status.statusText(version({ status })), Draft.STATUS_LABELS[status], status);
  }
  assert.equal(Status.statusText(version({ status: 'mystery' })), 'mystery');                      // 不認得的值原樣顯示，不丟錯
  assert.equal(Status.statusText(null), '');
  assert.equal(Status.statusText(undefined), '');
});

test('a rejected version shows the category and the time, and points to a new draft', () => {
  const text = Status.versionNotice(version({ status: 'rejected', decision_category: 'minor_suspected', decided_at: NOW }));
  assert.ok(text.includes('已被退回') && text.includes('疑似涉及未成年角色') && text.includes('2026-11-20 12:30'), text);
  assert.ok(text.includes('由最新版本建立新草稿') && text.includes('重新送審'), text);
});

test('every category the server can use has a plain-language label, including the manual-only other_policy', () => {
  assert.equal(Draft.CATEGORY_LABELS.other_policy, '其他違反平台規範');
  const text = Status.versionNotice(version({ status: 'rejected', decision_category: 'other_policy', decided_at: NOW }));
  assert.ok(text.includes('其他違反平台規範'), text);
  for (const category of Object.keys(Draft.CATEGORY_LABELS)) {
    const notice = Status.versionNotice(version({ status: 'removed', removed_by: 'admin', decision_category: category, decided_at: NOW }));
    assert.ok(notice.includes(Draft.CATEGORY_LABELS[category]), category);
    assert.ok(!notice.includes(category), '不顯示原始的代碼：' + category);
  }
});

test('a category or time the server did not send is left out instead of invented', () => {
  const noCategory = Status.versionNotice(version({ status: 'rejected', decided_at: NOW }));
  assert.ok(noCategory.includes('2026-11-20 12:30') && !noCategory.includes('，2026') && !noCategory.includes('（，'), noCategory);
  const nothing = Status.versionNotice(version({ status: 'rejected' }));
  assert.ok(nothing.startsWith('這個版本已被退回。'), nothing);
  const unknown = Status.versionNotice(version({ status: 'rejected', decision_category: 'brand_new_category', decided_at: NOW }));
  assert.ok(!unknown.includes('brand_new_category'), unknown);                                         // 不認得的類別不顯示代碼
});

test('a version the author took down explains what that means and that it can come back', () => {
  const text = Status.versionNotice(version({ status: 'removed', removed_by: 'author', decided_at: NOW }));
  assert.ok(text.includes('你已下架這個版本') && text.includes('2026-11-20 12:30'), text);
  assert.ok(text.includes('不能用它開新存檔') && text.includes('已開始的存檔不受影響') && text.includes('可以隨時恢復上架'), text);
  assert.ok(!text.includes('管理員'), text);
});

test('a version an administrator took down names the category, cannot be restored by the author, and points to a new draft', () => {
  const text = Status.versionNotice(version({ status: 'removed', removed_by: 'admin', decision_category: 'tier_mismatch', decided_at: NOW }));
  assert.ok(text.includes('管理員已下架這個版本') && text.includes('內容不符合綁定的安全模式') && text.includes('2026-11-20 12:30'), text);
  assert.ok(text.includes('無法自行恢復上架') && text.includes('由最新版本建立新草稿') && text.includes('重新送審'), text);
  assert.ok(!text.includes('可以隨時恢復上架'), text);
});

test('the guidance for a disputed take-down points to the customer-service feedback, never to the appeal page an ordinary author cannot open', () => {
  const text = Status.versionNotice(version({ status: 'removed', removed_by: 'admin', decision_category: 'other_policy', decided_at: NOW }));
  assert.ok(text.includes('客服（/cs）') && text.includes('意見回饋') && text.includes('管理員會在後台看到'), text);
  assert.ok(!/保證|一定|改善|處理結果/.test(text), text);                                                         // 不承諾處理結果
  const everything = [Status.versionNotice(version({ status: 'rejected', decision_category: 'other_policy', decided_at: NOW })), text,
    Status.versionNotice(version({ status: 'removed', removed_by: 'author', decided_at: NOW })),
    Status.readOnlyNotice(version({ status: 'removed', removed_by: 'admin', decided_at: NOW })), ...Object.values(Status.ACTION_ERRORS)];
  for (const item of everything) { assert.ok(!item.includes('申訴'), item); }
});

test('the customer-service link shows only beside an administrator take-down', () => {
  assert.equal(Status.showsSupportLink(version({ status: 'removed', removed_by: 'admin' })), true);
  for (const fields of [{ status: 'removed', removed_by: 'author' }, { status: 'removed' }, { status: 'published' }, { status: 'rejected' },
    { status: 'draft' }, { status: 'pending_review' }, { status: 'superseded' }]) {
    assert.equal(Status.showsSupportLink(version(fields)), false, JSON.stringify(fields));
  }
  assert.equal(Status.showsSupportLink(null), false);
  assert.equal(Status.showsSupportLink(undefined), false);
});

test('there is no notice for the statuses that need none', () => {
  for (const status of ['draft', 'pending_review', 'published', 'superseded']) { assert.equal(Status.versionNotice(version({ status })), '', status); }
  assert.equal(Status.versionNotice(null), '');
});

test('the notices never contain a review note, because the version summary has no field that could carry one', () => {
  const secret = '審核備註的內容絕不該出現';
  const summary = version({ status: 'removed', removed_by: 'admin', decision_category: 'other_policy', decided_at: NOW, decision_note: secret, note: secret });
  for (const text of [Status.versionNotice(summary), Status.readOnlyNotice(summary), Status.statusText(summary)]) { assert.ok(!text.includes(secret), text); }
});

test('the read-only banner keeps the old wording for the old states and explains the new ones', () => {
  assert.equal(Status.readOnlyNotice(version({ status: 'draft' })), '');
  assert.equal(Status.readOnlyNotice(version({ status: 'pending_review' })), '這個版本已經送審，內容凍結中；要修改請先撤回。');
  assert.ok(Status.readOnlyNotice(version({ status: 'published' })).includes('已上架') && Status.readOnlyNotice(version({ status: 'published' })).includes('由最新版本建立新草稿'));
  assert.ok(Status.readOnlyNotice(version({ status: 'superseded' })).includes('取代'));
  assert.ok(Status.readOnlyNotice(version({ status: 'rejected', decision_category: 'other_concern', decided_at: NOW })).includes('其他疑慮'));
  assert.equal(Status.readOnlyNotice(version({ status: 'removed', removed_by: 'author', decided_at: NOW })), Status.versionNotice(version({ status: 'removed', removed_by: 'author', decided_at: NOW })));
  assert.ok(Status.readOnlyNotice(version({ status: 'mystery' })).includes('內容不可修改'));         // 不認得的狀態仍有說明
});

test('the take-down button shows only for a published version, and the restore button only for one the author took down', () => {
  const cases = [
    [{ status: 'published' }, true, false],
    [{ status: 'removed', removed_by: 'author' }, false, true],
    [{ status: 'removed', removed_by: 'admin' }, false, false],       // 管理員下架的：沒有恢復按鈕
    [{ status: 'removed' }, false, false],                             // 下架者不明：不顯示，讓後端決定
    [{ status: 'draft' }, false, false],
    [{ status: 'pending_review' }, false, false],
    [{ status: 'rejected' }, false, false],
    [{ status: 'superseded' }, false, false],
  ];
  for (const [fields, takedown, restore] of cases) {
    assert.equal(Status.canTakedown(version(fields)), takedown, JSON.stringify(fields) + ' takedown');
    assert.equal(Status.canRestore(version(fields)), restore, JSON.stringify(fields) + ' restore');
  }
  assert.equal(Status.canTakedown(null), false);
  assert.equal(Status.canRestore(undefined), false);
});

test('the server error codes of take-down and restore each get their own Traditional Chinese message', () => {
  const codes = ['not_published', 'not_removed', 'removal_locked', 'live_exists'];
  const seen = new Set();
  for (const code of codes) {
    const text = Status.actionErrorText({ code, status: code === 'removal_locked' ? 403 : 409, message: 'server text' });
    assert.ok(text && /[一-鿿]/.test(text) && !text.includes(code), code);
    seen.add(text);
  }
  assert.equal(seen.size, codes.length);                                                                // 四個代碼的說明各不相同
  assert.ok(Status.actionErrorText({ code: 'removal_locked' }).includes('管理員下架'));
  assert.ok(Status.actionErrorText({ code: 'removal_locked' }).includes('客服（/cs）'));
  assert.ok(Status.actionErrorText({ code: 'live_exists' }).includes('另一個上架中的版本'));
  assert.deepEqual(Object.keys(Status.ACTION_ERRORS).sort(), codes.slice().sort());
});

test('an error code that is not about take-down gets no special message, so the caller falls back to the server text', () => {
  for (const info of [{ code: 'account_locked' }, { code: 'draft_changed' }, { code: 'toString' }, { code: '__proto__' }, {}, null, undefined]) {
    assert.equal(Status.actionErrorText(info), '', JSON.stringify(info));
  }
});

// ---- 重新來過：被退回（或已上架、已下架）之後的「建立新草稿」按鈕（使用者驗收時回報，2026-11-20）-------------------------------

function script(versions) { return { id: 'script-1', versions }; }
function ver(no, status) { return { id: 'v' + no, version_no: no, status }; }

test('the start-over button belongs to the latest version only, once it left draft and review, and only while the script has nothing open', () => {
  const rejected = script([ver(1, 'rejected')]);
  assert.equal(Status.canStartOver(rejected, rejected.versions[0]), true);                         // 被退回的最新版本：最常見的情況
  const live = script([ver(1, 'published')]);
  assert.equal(Status.canStartOver(live, live.versions[0]), true);                                   // 已上架：要修改也是同一條路
  const taken = script([ver(1, 'removed')]);
  assert.equal(Status.canStartOver(taken, taken.versions[0]), true);
  for (const status of ['draft', 'pending_review']) {                                              // 草稿直接編輯、待審先撤回
    const open = script([ver(1, status)]);
    assert.equal(Status.canStartOver(open, open.versions[0]), false, status);
  }
});

test('an older version never gets the start-over button, and neither does any version while another one is open', () => {
  const history = script([ver(1, 'rejected'), ver(2, 'published')]);
  assert.equal(Status.canStartOver(history, history.versions[0]), false);                          // 按下去是由「最新版本」建立，放在舊版本上會誤導
  assert.equal(Status.canStartOver(history, history.versions[1]), true);
  const busy = script([ver(1, 'published'), ver(2, 'draft')]);                                     // 已經有進行中的草稿：後端只允許一個
  assert.equal(Status.canStartOver(busy, busy.versions[0]), false);
  const waiting = script([ver(1, 'rejected'), ver(2, 'pending_review')]);
  assert.equal(Status.canStartOver(waiting, waiting.versions[0]), false);
  assert.equal(Status.canStartOver(waiting, waiting.versions[1]), false);
  const unordered = script([ver(2, 'rejected'), ver(1, 'superseded')]);                           // 順序不影響「最新」的判斷
  assert.equal(Status.canStartOver(unordered, unordered.versions[0]), true);
  assert.equal(Status.canStartOver(unordered, unordered.versions[1]), false);
});

test('missing or malformed data never shows the button and never throws', () => {
  const ok = script([ver(1, 'rejected')]);
  for (const bad of [null, undefined, {}, { versions: [] }, { versions: 'x' }, 5, []]) {
    assert.equal(Status.canStartOver(bad, ok.versions[0]), false, JSON.stringify(bad));
  }
  for (const bad of [null, undefined, {}, { id: 'v1' }, { id: 'v1', status: '' }, 5]) {
    assert.equal(Status.canStartOver(ok, bad), false, JSON.stringify(bad));
  }
  assert.equal(Status.latestVersion(null), null);
  assert.equal(Status.latestVersion({ versions: [] }), null);
  assert.equal(Status.hasOpenVersion(null), false);
});

test('the button says what the player wants to do: resubmit after a rejection, or submit a new version otherwise, and always that it makes a new draft', () => {
  assert.equal(Status.startOverText(ver(1, 'rejected')), '修改後重新送審（建立新草稿）');
  for (const status of ['published', 'removed', 'superseded', 'draft', undefined]) {
    assert.equal(Status.startOverText(ver(1, status)), '修改並送審新版本（建立新草稿）', String(status));
  }
  assert.equal(Status.startOverText(null), '修改並送審新版本（建立新草稿）');                    // 不丟錯
});

test('latestVersion picks the highest version number whatever the order, and hasOpenVersion sees drafts and pending reviews', () => {
  assert.equal(Status.latestVersion(script([ver(1, 'published'), ver(3, 'rejected'), ver(2, 'superseded')])).id, 'v3');
  assert.equal(Status.hasOpenVersion(script([ver(1, 'published')])), false);
  assert.equal(Status.hasOpenVersion(script([ver(1, 'published'), ver(2, 'draft')])), true);
  assert.equal(Status.hasOpenVersion(script([ver(1, 'rejected'), ver(2, 'pending_review')])), true);
});

test('the open-version guard stands on its own, even for a version order the backend would never produce', () => {
  // 後端同一個劇本同時只有一個進行中的版本，而且它一定是最新的；這裡刻意放一個「較舊的草稿、較新的已上架」，
  // 只有「劇本沒有進行中的版本」這道檢查能擋下來（其餘的條件都滿足）。
  const odd = script([ver(2, 'published'), ver(1, 'draft')]);
  assert.equal(Status.latestVersion(odd).id, 'v2');
  assert.equal(Status.canStartOver(odd, odd.versions[0]), false);
});
