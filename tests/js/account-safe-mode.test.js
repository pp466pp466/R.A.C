'use strict';
/* account-safe-mode-setting 規格的前端部分：返回位置驗證、初始狀態整理、切換的決策表，以及狀態機
 * （開→關需同意、已同意直接送出、關→開直接送出、緊急煞車停用、請求期間禁用並恢復、失敗還原、成功後導向返回位置）。
 * 狀態機以替身的 request／toast／navigate 執行，不需要瀏覽器；另以沙盒（vm）載入檔案，確認瀏覽器載入後掛在 `Auth.safeMode`。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..', '..');
const FILE = path.join(ROOT, 'app', 'static', 'js', 'auth', 'account-safe-mode.js');
const SafeMode = require(FILE);
const Common = require('../../app/static/js/auth/auth-common.js');
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'safe-return-path-cases.json'), 'utf8'));

const RETURN = '/game/start/0b6f3a52-1c8e-4d57-9a41-7f2e6c1d8b90';
const ON = { effective: 'safe', confirmed: false, forced: false, returnPath: null };
const ON_CONFIRMED = { effective: 'safe', confirmed: true, forced: false, returnPath: null };
const OFF = { effective: 'adult', confirmed: true, forced: false, returnPath: null };

function serverError(status, code, message, fields) {
  return { response: { status, data: { error: Object.assign({ code, message }, fields ? { fields } : {}) } } };
}

const answer = (effective, confirmed, forced = false) => ({
  data: { ok: true, content_mode: { preference: effective, effective, confirmed, forced } },
});

/* 建立一個以替身執行的狀態機；`handler(body)` 決定每次請求的結果。 */
function build(initial, handler) {
  const requests = [];
  const toasts = [];
  const navigations = [];
  const delayed = [];
  const errors = [];
  const controller = SafeMode.createController({
    initial,
    request: (body) => { requests.push(JSON.parse(JSON.stringify(body))); return handler(body); },
    describeError: Common.describeError,
    toast: (message, settings) => toasts.push({ message, type: settings && settings.type ? settings.type : 'info' }),
    navigate: (target) => navigations.push(target),
    later: (fn, ms) => delayed.push({ fn, ms }),
    onError: (info) => errors.push(info.code),
  });
  return { controller, view: controller.view, requests, toasts, navigations, delayed, errors };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ---- 返回位置 -------------------------------------------------------------------------------

test('only paths inside the game area are valid return positions (table shared with the server test)', () => {
  for (const value of CASES.valid) { assert.equal(SafeMode.safeReturnPath(value), value, value); }
  for (const value of CASES.invalid) { assert.equal(SafeMode.safeReturnPath(value), null, JSON.stringify(value)); }
});

test('the length limit is 200 characters and anything that is not a string is dropped', () => {
  const longest = SafeMode.RETURN_PREFIX + 'a'.repeat(SafeMode.MAX_RETURN_LENGTH - SafeMode.RETURN_PREFIX.length);
  assert.equal(longest.length, 200);
  assert.equal(SafeMode.safeReturnPath(longest), longest);
  assert.equal(SafeMode.safeReturnPath(longest + 'a'), null);
  assert.equal(CASES.tooLongLength, 201);
  for (const value of CASES.notStrings) { assert.equal(SafeMode.safeReturnPath(value), null, JSON.stringify(value)); }
  assert.equal(SafeMode.safeReturnPath(undefined), null);
});

// ---- 初始狀態 -------------------------------------------------------------------------------

test('a missing or malformed initial state makes the block unavailable (an admin who has not confirmed the age)', () => {
  for (const raw of [undefined, null, {}, [], 'safe', 7, { effective: 'standard' }, { effective: 'Safe' }, { confirmed: true }]) {
    const view = SafeMode.normalize(raw);
    assert.equal(view.available, false, JSON.stringify(raw));
    assert.equal(SafeMode.planToggle(Object.assign({ busy: false }, view)).action, 'none');
  }
});

test('the switch shows the effective mode; the emergency brake always shows it on and disabled', () => {
  assert.deepEqual({ ...SafeMode.normalize(ON) }, { available: true, safe: true, confirmed: false, forced: false, returnPath: null });
  assert.equal(SafeMode.normalize(OFF).safe, false);
  const braked = SafeMode.normalize({ effective: 'adult', confirmed: true, forced: true });
  assert.equal(braked.safe, true);
  assert.equal(braked.forced, true);
});

test('booleans must be real booleans: strings and numbers never count as confirmed or forced', () => {
  const view = SafeMode.normalize({ effective: 'adult', confirmed: 'true', forced: 1 });
  assert.equal(view.confirmed, false);
  assert.equal(view.forced, false);
});

test('the return position the server sent is validated again before it is used', () => {
  assert.equal(SafeMode.normalize({ ...ON, returnPath: RETURN }).returnPath, RETURN);
  for (const bad of ['https://example.com/', '//example.com/', '/account', '/game//x', '/game/../account', 7, null]) {
    assert.equal(SafeMode.normalize({ ...ON, returnPath: bad }).returnPath, null, JSON.stringify(bad));
  }
});

// ---- 決策表 ---------------------------------------------------------------------------------

test('what a click on the switch does, for every combination', () => {
  const view = (patch) => Object.assign({ available: true, safe: true, confirmed: false, forced: false, busy: false }, patch);
  assert.deepEqual(SafeMode.planToggle(view({ safe: true, confirmed: false })), { action: 'confirm' });                    // 第一次關閉：先同意
  assert.deepEqual(SafeMode.planToggle(view({ safe: true, confirmed: true })), { action: 'send', body: { mode: 'adult' } });   // 已同意：直接送出
  assert.deepEqual(SafeMode.planToggle(view({ safe: false, confirmed: true })), { action: 'send', body: { mode: 'safe' } });   // 回到安全模式：直接送出
  assert.deepEqual(SafeMode.planToggle(view({ safe: false, confirmed: false })), { action: 'send', body: { mode: 'safe' } });
  for (const blocked of [{ forced: true }, { busy: true }, { available: false }]) {
    assert.deepEqual(SafeMode.planToggle(view(blocked)), { action: 'none' }, JSON.stringify(blocked));
  }
});

test('the confirmation request carries confirm_rules only when the box is really checked', () => {
  assert.deepEqual(SafeMode.confirmBody(true), { mode: 'adult', confirm_rules: true });
  for (const value of [false, undefined, null, 'true', 1, 0, '']) { assert.equal(SafeMode.confirmBody(value), null, String(value)); }
});

// ---- 開 → 關 ---------------------------------------------------------------------------------

test('turning it off for the first time opens the rules modal and sends nothing', async () => {
  const { controller, view, requests } = build(ON, () => Promise.reject(new Error('no request expected')));
  assert.equal(await controller.toggle(), false);
  assert.equal(view.modalOpen, true);
  assert.equal(view.agreed, false);
  assert.equal(view.safe, true);
  assert.equal(requests.length, 0);
});

test('the confirm button does nothing until the rules box is checked', async () => {
  const { controller, view, requests } = build(ON, () => Promise.resolve(answer('adult', true)));
  await controller.toggle();
  assert.equal(await controller.confirm(), false);
  assert.equal(requests.length, 0);
  assert.equal(view.modalOpen, true);
});

test('agreeing and confirming sends mode adult with confirm_rules, then closes the modal and shows a toast', async () => {
  const { controller, view, requests, toasts } = build(ON, () => Promise.resolve(answer('adult', true)));
  await controller.toggle();
  view.agreed = true;
  assert.equal(await controller.confirm(), true);
  assert.deepEqual(requests, [{ mode: 'adult', confirm_rules: true }]);
  assert.equal(view.safe, false);
  assert.equal(view.confirmed, true);
  assert.equal(view.modalOpen, false);
  assert.equal(view.agreed, false);
  assert.deepEqual(toasts, [{ message: '安全模式已關閉。', type: 'info' }]);
  assert.equal(view.busy, false);
});

test('cancel, Esc and the overlay close the modal without a request and leave the switch on', async () => {
  const { controller, view, requests } = build(ON, () => Promise.reject(new Error('no request expected')));
  await controller.toggle();
  view.agreed = true;
  controller.cancel();
  assert.equal(view.modalOpen, false);
  assert.equal(view.agreed, false);                          // 下次開啟 Modal 時不會殘留上次的勾選
  assert.equal(view.safe, true);
  assert.equal(requests.length, 0);
  await controller.toggle();
  assert.equal(view.agreed, false);
});

test('a player who already agreed to the current version turns it off with one click and no modal', async () => {
  const { controller, view, requests, toasts } = build(ON_CONFIRMED, () => Promise.resolve(answer('adult', true)));
  assert.equal(await controller.toggle(), true);
  assert.deepEqual(requests, [{ mode: 'adult' }]);            // 不重送同意，伺服器也不再要求
  assert.equal(view.modalOpen, false);
  assert.equal(view.safe, false);
  assert.equal(toasts.length, 1);
});

// ---- 關 → 開 ---------------------------------------------------------------------------------

test('switching back to safe mode sends at once without a modal', async () => {
  const { controller, view, requests, toasts } = build(OFF, () => Promise.resolve(answer('safe', true)));
  assert.equal(await controller.toggle(), true);
  assert.deepEqual(requests, [{ mode: 'safe' }]);
  assert.equal(view.modalOpen, false);
  assert.equal(view.safe, true);
  assert.deepEqual(toasts, [{ message: '安全模式已開啟。', type: 'info' }]);
});

// ---- 緊急煞車 --------------------------------------------------------------------------------

test('with the emergency brake on, nothing can be sent and no modal opens', async () => {
  const { controller, view, requests } = build({ effective: 'adult', confirmed: true, forced: true }, () => Promise.reject(new Error('no request expected')));
  assert.equal(view.safe, true);
  assert.equal(await controller.toggle(), false);
  assert.equal(await controller.confirm(), false);
  assert.equal(view.modalOpen, false);
  assert.equal(requests.length, 0);
});

test('the server saying the brake is on updates the page: forced, shown on, modal closed, explained', async () => {
  const { controller, view, errors } = build(ON, () => Promise.reject(serverError(403, 'safe_mode_forced', 'x')));
  await controller.toggle();
  view.agreed = true;
  assert.equal(await controller.confirm(), false);
  assert.equal(view.forced, true);
  assert.equal(view.safe, true);
  assert.equal(view.modalOpen, false);
  assert.equal(view.error, '目前系統暫時只提供安全模式，無法關閉。');
  assert.deepEqual(errors, ['safe_mode_forced']);
  assert.equal(view.busy, false);
});

// ---- 請求期間與失敗 --------------------------------------------------------------------------

test('while the request is in flight the switch is busy and a second click sends nothing; it recovers afterwards', async () => {
  let release;
  const { controller, view, requests } = build(OFF, () => new Promise((resolve) => { release = () => resolve(answer('safe', true)); }));
  const first = controller.toggle();
  await tick();
  assert.equal(view.busy, true);
  assert.equal(await controller.toggle(), false);
  assert.equal(requests.length, 1);
  release();
  assert.equal(await first, true);
  assert.equal(view.busy, false);
});

test('a failed request leaves the switch as it was, explains in Traditional Chinese and re-enables the button', async () => {
  const { controller, view, requests } = build(ON_CONFIRMED, () => Promise.reject(serverError(500, 'internal_error', '系統發生錯誤，請稍後再試。')));
  assert.equal(await controller.toggle(), false);
  assert.equal(view.safe, true);                              // 不樂觀更新
  assert.equal(view.error, '系統發生錯誤，請稍後再試。');
  assert.equal(view.busy, false);
  assert.equal(requests.length, 1);
});

test('network failures and timeouts also restore the switch and explain', async () => {
  for (const error of [new Error('Network Error'), Object.assign(new Error('timeout'), { code: 'ECONNABORTED' })]) {
    const { controller, view } = build(ON_CONFIRMED, () => Promise.reject(error));
    assert.equal(await controller.toggle(), false);
    assert.equal(view.safe, true);
    assert.match(view.error, /[一-鿿]/);
    assert.equal(view.busy, false);
  }
});

test('a failed confirmation keeps the modal open with the error so the player can retry', async () => {
  let attempt = 0;
  const { controller, view, requests } = build(ON, () => { attempt += 1; return attempt === 1 ? Promise.reject(new Error('Network Error')) : Promise.resolve(answer('adult', true)); });
  await controller.toggle();
  view.agreed = true;
  assert.equal(await controller.confirm(), false);
  assert.equal(view.modalOpen, true);
  assert.equal(view.agreed, true);
  assert.ok(view.error);
  assert.equal(await controller.confirm(), true);
  assert.equal(view.error, '');
  assert.equal(view.safe, false);
  assert.equal(requests.length, 2);
});

test('the server asking for the rules agreement shows the agreement message', async () => {
  const { controller, view } = build(ON, () => Promise.reject(serverError(400, 'validation_failed', '請勾選同意安全規範。', { confirm_rules: '請勾選同意安全規範。' })));
  await controller.toggle();
  view.agreed = true;
  await controller.confirm();
  assert.equal(view.error, '請勾選同意安全規範。');
  assert.equal(view.safe, true);
});

test('an age gate refusal is explained and reported to the page (restriction shortcuts, 401 handling)', async () => {
  const { controller, view, errors } = build(ON_CONFIRMED, () => Promise.reject(serverError(403, 'age_confirmation_required', 'x')));
  await controller.toggle();
  assert.equal(view.error, '請先完成年齡確認，再調整安全模式。');
  assert.deepEqual(errors, ['age_confirmation_required']);
});

test('an aborted request is not an error', async () => {
  const { controller, view } = build(ON_CONFIRMED, () => Promise.reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' })));
  assert.equal(await controller.toggle(), false);
  assert.equal(view.error, '');
  assert.equal(view.busy, false);
});

test('a success body that is not a mode state does not change the switch and asks for a refresh', async () => {
  for (const data of [undefined, null, {}, { content_mode: null }, { content_mode: { effective: 'standard' } }, { content_mode: 'adult' }]) {
    const { controller, view } = build(ON_CONFIRMED, () => Promise.resolve({ data }));
    assert.equal(await controller.toggle(), false, JSON.stringify(data));
    assert.equal(view.safe, true);
    assert.equal(view.error, '無法確認目前的設定，請重新整理頁面後再試。');
    assert.equal(view.busy, false);
  }
});

// ---- 返回位置：成功後導向 -------------------------------------------------------------------

test('with a valid return position a successful change shows the toast and then goes back to the script', async () => {
  const { controller, requests, toasts, navigations, delayed } = build({ ...ON_CONFIRMED, returnPath: RETURN }, () => Promise.resolve(answer('adult', true)));
  assert.equal(await controller.toggle(), true);
  assert.equal(requests.length, 1);
  assert.deepEqual(toasts, [{ message: '安全模式已關閉，正在回到劇本…', type: 'info' }]);
  assert.deepEqual(navigations, []);                           // 先讓玩家看到提示
  assert.equal(delayed.length, 1);
  assert.equal(delayed[0].ms, SafeMode.NAVIGATE_DELAY_MS);
  delayed[0].fn();
  assert.deepEqual(navigations, [RETURN]);
});

test('a failed change never navigates, and without a return position nothing navigates at all', async () => {
  const failing = build({ ...ON_CONFIRMED, returnPath: RETURN }, () => Promise.reject(serverError(500, 'internal_error', 'x')));
  await failing.controller.toggle();
  assert.equal(failing.delayed.length, 0);
  assert.deepEqual(failing.navigations, []);
  const plain = build(ON_CONFIRMED, () => Promise.resolve(answer('adult', true)));
  await plain.controller.toggle();
  assert.equal(plain.delayed.length, 0);
  assert.deepEqual(plain.navigations, []);
});

test('an invalid return position is ignored, so the page behaves as usual', async () => {
  const { controller, view, delayed } = build({ ...ON_CONFIRMED, returnPath: 'https://example.com/' }, () => Promise.resolve(answer('adult', true)));
  assert.equal(view.returnPath, null);
  await controller.toggle();
  assert.equal(delayed.length, 0);
});

// ---- 文案與檔案 ------------------------------------------------------------------------------

test('the rules text covers explicit content, the absolute bans and provider retention, with no age checkbox wording', () => {
  const rules = SafeMode.COPY.rulesPoints.join('\n');
  for (const needle of ['服務含露骨內容', '一律禁止', '未成年', '性暴力', '非自願', '以真人名義', '敏感內容可能被模型供應商留存或人工審閱']) {
    assert.ok(rules.includes(needle), needle);
  }
  const everything = JSON.stringify(SafeMode.COPY) + JSON.stringify(SafeMode.MESSAGES);
  for (const forbidden of ['已滿 18 歲', '我確定', '一般模式', '成人模式', '保證會', '一定會']) {
    assert.ok(!everything.includes(forbidden), forbidden);
  }
  assert.ok(SafeMode.COPY.stateOff.includes('不保證'));
  assert.equal(SafeMode.COPY.agreeLabel, '我同意安全規範');
});

test('the file keeps to the rules: no v-html, no browser storage, no external hosts', () => {
  const source = fs.readFileSync(FILE, 'utf8');
  assert.ok(!source.includes('v-html'));
  assert.ok(!/localStorage|sessionStorage|indexedDB/i.test(source));
  assert.ok(!/https?:\/\//.test(source));
});

test('loaded in a browser-like sandbox the module hangs off Auth.safeMode', () => {
  const sandbox = { console, Promise, setTimeout };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), sandbox, { filename: FILE });
  assert.equal(typeof sandbox.Auth.safeMode.createController, 'function');
  assert.equal(sandbox.Auth.safeMode.safeReturnPath('/game/start/abc'), '/game/start/abc');
  assert.equal(sandbox.Auth.safeMode.safeReturnPath('//evil.example'), null);
});
