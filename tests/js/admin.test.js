'use strict';
/* 後台「被檢舉」檢視與檢舉面板（admin.js；見 admin-script-reports 規格「後台『被檢舉』檢視」）：
 * 以真的 Vue、真的 Auth 與真的 admin-format.js 在 `vm` 沙盒執行 admin.js 的 setup()，只把 axios、Toast、捲動與 mount 換成替身。
 * 驗證被檢舉清單的來源與順序（前端不重排）、開啟版本時先讀全文再讀檢舉面板（只有上架中且有未結案檢舉才讀）、讀取失敗不阻止全文、
 * 「不成立（結案）」的請求與每種失敗的處理（409／404 關閉視窗並重新載入）、下架與核可之後被檢舉清單的處理，以及樣板用到的每個名稱都由 setup() 回傳。
 * 沒有瀏覽器可以掛載；真實的點擊流程在瀏覽器驗收。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { setupAdmin, fail, read, plain } = require('./support/admin-sandbox.js');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);
const tick = () => new Promise((resolve) => setImmediate(resolve));

const V1 = '11111111-1111-4111-8111-111111111111';
const V2 = '22222222-2222-4222-8222-222222222222';
const V3 = '33333333-3333-4333-8333-333333333333';
const S1 = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

const row = (id, extra = {}) => ({
  id, script_id: S1, title: `劇本 ${id.slice(0, 2)}`, summary: '', status: 'published', version_no: 1, audience: 'all', content_tier: 'adult', author_name: '作者', author_code: 'AB12',
  is_builtin: false, has_note: false, report_count: 2, red_line_reported: false, ...extra,
});
const reportRows = () => ([
  { reasons: ['plagiarism'], text: '<b>抄襲</b>', created_at: '2026-11-20T04:30:00Z', closed_at: null, closed_result: null },
  { reasons: ['minor_sexual', 'other'], text: '', created_at: '2026-11-21T04:30:00Z', closed_at: null, closed_result: null },
]);

const ok = (body) => body;

/* 一組常用的路由：清單、全文、檢舉、結案。`overrides` 以路徑尾巴或完整網址覆寫。 */
function routesFor({ versions = {}, reports = reportRows(), listBody, dismiss, reportsRoute } = {}) {
  return (request) => {
    const url = request.url;
    if (request.method === 'get' && url === '/api/admin/scripts') {
      const status = request.params && request.params.status;
      return listBody ? listBody(status) : { ok: true, versions: status === 'reported' ? [row(V1, { red_line_reported: true }), row(V2, { report_count: 1 })] : [] };
    }
    if (request.method === 'get' && url === '/api/admin/overview') { return { ok: true, overview: { warnings: {}, usage: {} } }; }
    const detail = /^\/api\/admin\/scripts\/versions\/([^/]+)$/.exec(url);
    if (request.method === 'get' && detail) { return { ok: true, version: { ...(versions[detail[1]] || row(detail[1])), decision_note: null, content: {} } }; }
    const reportsRead = /^\/api\/admin\/scripts\/versions\/([^/]+)\/reports$/.exec(url);
    if (request.method === 'get' && reportsRead) { return reportsRoute ? reportsRoute(reportsRead[1]) : { ok: true, reports }; }
    const closing = /^\/api\/admin\/scripts\/versions\/([^/]+)\/reports\/dismiss$/.exec(url);
    if (request.method === 'post' && closing) { return dismiss ? dismiss(closing[1]) : { ok: true, version: row(closing[1], { report_count: 0 }) }; }
    const removing = /^\/api\/admin\/scripts\/versions\/([^/]+)\/(remove|approve)$/.exec(url);
    if (request.method === 'post' && removing) { return { ok: true, version: row(removing[1], { status: removing[2] === 'remove' ? 'removed' : 'published' }) }; }
    return undefined;
  };
}

async function openReported(options) {
  const setup = setupAdmin(routesFor(options));
  await setup.mount();
  await setup.page.selectTab('scripts');
  await setup.page.selectScriptView('reported');
  return setup;
}

// ---- 清單 -------------------------------------------------------------------------------------------

t('the script views are pending, published, reported and removed, and "reported" reads status=reported from the same endpoint', async () => {
  const { page, requests } = await openReported();
  assert.deepEqual(plain(page.scriptViews).map((view) => view.id), ['pending_review', 'published', 'reported', 'removed']);
  assert.equal(page.scriptViews.find((view) => view.id === 'reported').label, '被檢舉');
  const read = requests.filter((r) => r.method === 'get' && r.params && r.params.status === 'reported');
  assert.equal(read.length, 1);
  assert.equal(read[0].url, '/api/admin/scripts');
  assert.deepEqual(read[0].params, { status: 'reported', limit: 50, offset: 0 });
  assert.equal(page.scriptListId.value, 'scripts_reported');
});

t('the reported list keeps the order the server gave (priority is decided by the backend) and does not sort', async () => {
  const body = { ok: true, versions: [row(V3, { report_count: 1 }), row(V1, { report_count: 9, red_line_reported: false }), row(V2, { report_count: 3, red_line_reported: true })] };
  const { page } = await openReported({ listBody: (status) => (status === 'reported' ? body : { ok: true, versions: [] }) });
  assert.deepEqual(plain(page.lists.scripts_reported.items).map((item) => item.id), [V3, V1, V2]);
});

t('every other script list is also read once with its own status, and nothing is read twice when switching views', async () => {
  const { page, gets } = await openReported();
  await page.selectScriptView('published');
  await page.selectScriptView('reported');
  await page.selectScriptView('removed');
  assert.deepEqual(gets().filter((url) => url.startsWith('/api/admin/scripts?')).sort(), [
    '/api/admin/scripts?status=pending_review', '/api/admin/scripts?status=published', '/api/admin/scripts?status=removed', '/api/admin/scripts?status=reported',
  ]);
});

t('the empty-list text follows the view', async () => {
  const { page } = await openReported({ listBody: () => ({ ok: true, versions: [] }) });
  assert.equal(page.scriptEmptyText.value, '目前沒有被檢舉的劇本。');
  await page.selectScriptView('removed');
  assert.equal(page.scriptEmptyText.value, '目前沒有已下架的劇本。');
});

// ---- 開啟版本：先全文、再檢舉 ------------------------------------------------------------------------

t('opening a published version with open reports reads the full text first and the reports second, then shows both', async () => {
  const setup = await openReported();
  const before = setup.requests.length;
  await setup.page.openScript(V1);
  const sent = setup.requests.slice(before).map((r) => `${r.method} ${r.url}`);
  assert.deepEqual(sent, [`get /api/admin/scripts/versions/${V1}`, `get /api/admin/scripts/versions/${V1}/reports`]);
  const { page } = setup;
  assert.equal(page.scriptOpen.value, true);
  assert.equal(page.scriptDoc.version.id, V1);
  assert.equal(page.scriptDoc.loading, false);
  assert.equal(page.scriptReports.shown, true);
  assert.equal(page.scriptReports.loading, false);
  assert.equal(page.openReportCount.value, 2);
  assert.deepEqual(plain(page.scriptReportRows.value).map((r) => [r.reasonsText, r.redLine, r.text]), [['抄襲或侵權', false, '<b>抄襲</b>'], ['涉及未成年的性內容、其他', true, '']]);
  assert.equal(page.scriptDocActions.value.dismiss, true);
});

t('a version with no open report, a pending version and a removed one never read the reports (each read would only write an audit row)', async () => {
  const versions = {
    [V1]: row(V1, { report_count: 0 }),
    [V2]: row(V2, { status: 'pending_review', report_count: 0 }),
    [V3]: row(V3, { status: 'removed', report_count: 0, removed_by: 'admin' }),
  };
  const setup = await openReported({ versions });
  for (const id of [V1, V2, V3]) {
    await setup.page.openScript(id);
    assert.equal(setup.page.scriptReports.shown, false, id);
    assert.equal(setup.page.scriptDocActions.value.dismiss, false, id);
  }
  assert.equal(setup.requests.filter((r) => r.url.endsWith('/reports')).length, 0);
});

t('a failed report read shows the error in the panel only: the full text stays, dismissing is blocked, and retry works', async () => {
  let failing = true;
  const setup = await openReported({ reportsRoute: () => (failing ? fail(500, 'internal_error', '讀取檢舉失敗') : { ok: true, reports: reportRows() }) });
  const { page, requests } = setup;
  await page.openScript(V1);
  assert.equal(page.scriptDoc.version.id, V1);
  assert.equal(page.scriptDoc.error, '');
  assert.equal(page.scriptReports.error, '讀取檢舉失敗');
  assert.equal(page.scriptReports.loading, false);
  await page.dismissOpenScript();
  assert.equal(requests.filter((r) => r.method === 'post').length, 0);                       // 沒讀到檢舉就不能結案
  failing = false;
  await page.reloadScriptReports();
  assert.equal(page.scriptReports.error, '');
  assert.equal(page.openReportCount.value, 2);
});

t('closing the window clears the panel, and a reports answer that arrives late is dropped', async () => {
  let release = null;
  const setup = await openReported({ reportsRoute: () => new Promise((resolve) => { release = () => resolve({ ok: true, reports: reportRows() }); }) });
  const { page } = setup;
  const opening = page.openScript(V1);
  await tick();
  await tick();
  assert.equal(page.scriptReports.loading, true);
  page.scriptOpen.value = false;
  await tick();
  assert.equal(page.scriptReports.shown, false);
  release();
  await opening;
  assert.equal(page.scriptReports.shown, false);
  assert.deepEqual(plain(page.scriptReports.items), []);
});

t('opening another version replaces the panel with that version\'s reports', async () => {
  const bodies = { [V1]: reportRows(), [V2]: [reportRows()[0]] };
  const setup = await openReported({ reportsRoute: (id) => ({ ok: true, reports: bodies[id] }) });
  await setup.page.openScript(V1);
  assert.equal(setup.page.scriptReportRows.value.length, 2);
  await setup.page.openScript(V2);
  assert.equal(setup.page.scriptReportRows.value.length, 1);
});

// ---- 不成立（結案）-----------------------------------------------------------------------------------

t('dismissing posts an empty body without any password, closes the window, toasts, drops the row from the reported list and marks the published list stale', async () => {
  const setup = await openReported();
  const { page, requests, toasts } = setup;
  await page.selectScriptView('published');
  await page.selectScriptView('reported');
  await page.openScript(V1);
  await page.dismissOpenScript();
  const post = requests.filter((r) => r.method === 'post');
  assert.deepEqual(post.map((r) => [r.url, r.data]), [[`/api/admin/scripts/versions/${V1}/reports/dismiss`, {}]]);
  assert.equal(page.scriptOpen.value, false);
  assert.equal(page.scriptDoc.busy, false);
  assert.deepEqual(toasts, [{ message: '已結案：檢舉不成立。', type: 'info' }]);
  assert.deepEqual(plain(page.lists.scripts_reported.items).map((item) => item.id), [V2]);
  assert.equal(page.lists.scripts_published.loaded, false);
  assert.equal(page.lists.audit.loaded, false);
});

t('a second press while the request is in flight sends nothing more', async () => {
  let release = null;
  const setup = await openReported({ dismiss: () => new Promise((resolve) => { release = () => resolve({ ok: true }); }) });
  const { page, requests } = setup;
  await page.openScript(V1);
  const first = page.dismissOpenScript();
  await tick();
  assert.equal(page.scriptDoc.busy, true);
  await page.dismissOpenScript();
  assert.equal(requests.filter((r) => r.method === 'post').length, 1);
  release();
  await first;
  assert.equal(page.scriptDoc.busy, false);
});

t('409 (someone else closed or took it down) closes the window, shows the reason and reloads the lists', async () => {
  const setup = await openReported({ dismiss: () => fail(409, 'no_open_reports', '這個版本沒有未結案的檢舉。') });
  const { page, toasts, gets } = setup;
  await page.openScript(V1);
  const before = gets().length;
  await page.dismissOpenScript();
  assert.equal(page.scriptOpen.value, false);
  assert.deepEqual(toasts, [{ message: '這個版本沒有未結案的檢舉。', type: 'error' }]);
  const after = gets().slice(before);
  assert.ok(after.includes('/api/admin/scripts?status=pending_review'), after.join());         // 標籤上的待審數量也要更新
  assert.ok(after.includes('/api/admin/scripts?status=reported'), after.join());
  assert.equal(page.scriptDoc.busy, false);
});

t('404 is handled the same way as 409', async () => {
  const setup = await openReported({ dismiss: () => fail(404, 'not_found', '找不到。') });
  await setup.page.openScript(V1);
  await setup.page.dismissOpenScript();
  assert.equal(setup.page.scriptOpen.value, false);
  assert.equal(setup.toasts[0].type, 'error');
});

t('any other failure keeps the window open with the message inside it, and the button is usable again', async () => {
  const setup = await openReported({ dismiss: () => fail(500, 'internal_error', '伺服器忙碌。') });
  const { page } = setup;
  await page.openScript(V1);
  await page.dismissOpenScript();
  assert.equal(page.scriptOpen.value, true);
  assert.equal(page.scriptDoc.actionError, '伺服器忙碌。');
  assert.equal(page.scriptDoc.busy, false);
  assert.equal(page.scriptDocActions.value.dismiss, true);
});

t('an expired session sends the administrator to the login page', async () => {
  const setup = await openReported({ dismiss: () => fail(401, 'unauthorized', '請先登入。') });
  await setup.page.openScript(V1);
  await setup.page.dismissOpenScript();
  assert.deepEqual(setup.navigations, ['/login?next=%2Fadmin']);
});

t('dismiss does nothing when nothing is open or the version has no open reports', async () => {
  const versions = { [V1]: row(V1, { report_count: 0 }) };
  const setup = await openReported({ versions });
  await setup.page.dismissOpenScript();
  await setup.page.openScript(V1);
  await setup.page.dismissOpenScript();
  assert.equal(setup.requests.filter((r) => r.method === 'post').length, 0);
});

// ---- 下架與核可之後 ----------------------------------------------------------------------------------

t('taking a reported version down removes it from the reported and published lists and marks the removed list stale', async () => {
  const setup = await openReported();
  const { page, requests } = setup;
  await page.selectScriptView('published');
  page.lists.scripts_published.items = [row(V1), row(V3)];
  page.askRemoveScript(page.lists.scripts_reported.items[0]);
  page.confirmState.password = '密碼';
  page.confirmState.category = 'minor_sexual';
  await page.submitConfirm();
  assert.deepEqual(requests.filter((r) => r.method === 'post').map((r) => r.url), [`/api/admin/scripts/versions/${V1}/remove`]);
  assert.deepEqual(plain(page.lists.scripts_reported.items).map((item) => item.id), [V2]);
  assert.deepEqual(plain(page.lists.scripts_published.items).map((item) => item.id), [V3]);
  assert.equal(page.lists.scripts_removed.loaded, false);
});

t('approving a new version marks the reported list stale too (the old version\'s reports are closed by the backend)', async () => {
  const versions = { [V3]: row(V3, { status: 'pending_review', report_count: 0 }) };
  const setup = await openReported({ versions, dismiss: undefined });
  const { page, requests } = setup;
  assert.equal(page.lists.scripts_reported.loaded, true);
  await page.openScript(V3);
  await page.approveOpenScript();
  assert.deepEqual(requests.filter((r) => r.method === 'post').map((r) => r.url), [`/api/admin/scripts/versions/${V3}/approve`]);
  assert.equal(page.lists.scripts_reported.loaded, false);
  assert.deepEqual(plain(page.lists.scripts_reported.items), []);
});

// ---- 樣板 -------------------------------------------------------------------------------------------

const JS_WORDS = new Set(['true', 'false', 'null', 'undefined', 'in', 'of', 'typeof']);
const LOOP_VARS = new Set(['row', 'view', 'report', 'index', 'section', 'item']);

function namesIn(html) {
  const expressions = [];
  for (const match of html.matchAll(/\{\{([\s\S]*?)\}\}/g)) { expressions.push(match[1]); }
  for (const match of html.matchAll(/\s(?:v-if|v-else-if|v-show|v-model|:[\w-]+|@[\w.-]+)="([^"]*)"/g)) { expressions.push(match[1]); }
  for (const match of html.matchAll(/\sv-for="[^"]*?\sin\s([^"]*)"/g)) { expressions.push(match[1]); }
  const names = new Set();
  for (const expression of expressions) {
    const stripped = expression.replace(/'[^']*'/g, "''");
    for (const match of stripped.matchAll(/(?<![\w.$])([A-Za-z_$][\w$]*)/g)) { names.add(match[1]); }
  }
  return [...names].filter((name) => !JS_WORDS.has(name) && !LOOP_VARS.has(name)).sort();
}

t('every name the script section and the script window use is returned by setup()', () => {
  const html = read('app/templates/admin/console.html');
  const section = html.slice(html.indexOf('<!-- ===== 劇本審核 ===== -->'), html.indexOf('<!-- ===== 被鎖定的帳號 ===== -->'));
  const modal = html.slice(html.indexOf('<!-- 劇本全文'), html.indexOf('<!-- 退回'));
  const names = [...new Set([...namesIn(section), ...namesIn(modal)])];
  assert.ok(names.length >= 20, `只掃到 ${names.length} 個名稱，掃描可能是空洞的：${names}`);
  const { page } = setupAdmin(() => undefined);
  const missing = names.filter((name) => !(name in page));
  assert.deepEqual(missing, [], `樣板用到但 setup() 沒有回傳：${missing}`);
  for (const must of ['scriptReports', 'scriptReportRows', 'openReportCount', 'reloadScriptReports', 'dismissOpenScript', 'scriptEmptyText']) {
    assert.ok(names.includes(must), `樣板沒有用到 ${must}`);
  }
  for (const [, fn] of (section + modal).matchAll(/fmt\.(\w+)/g)) { assert.equal(typeof page.fmt[fn], 'function', `fmt.${fn} 不存在`); }
});

t('the panel shows reasons as labels and the free text as plain interpolation, with no reporter anywhere', () => {
  const html = read('app/templates/admin/console.html');
  const modal = html.slice(html.indexOf('<!-- 劇本全文'), html.indexOf('<!-- 退回'));
  for (const expression of ['{{ report.reasonsText }}', '{{ report.text }}', '{{ report.createdText }}', '{{ report.statusText }}']) { assert.ok(modal.includes(expression), expression); }
  for (const forbidden of ['v-html', 'innerHTML', 'data-bs-', 'reporter', '檢舉人身分']) { assert.ok(!modal.includes(forbidden), forbidden); }
});
