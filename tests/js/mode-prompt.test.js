'use strict';
/* 詢問「要不要修改安全模式」的共用模組（mode-prompt.js；入口頁與劇本詳情頁共用，見 rpg-entry-pages 規格與 rpg-script-publishing 的 design D7）。
 * 純函式（文案與 `needsAsk`）直接 require；狀態機（`create`）用真的 Vue（vm 沙盒載入離線的 vue.global.prod.js），導向與模式都由替身提供，
 * 所以模組本身不碰 `window`、不送請求、不寫任何瀏覽器儲存也在這裡固定。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const t = (name, fn) => test(name, { timeout: 5000 }, fn);

const ROOT = path.join(__dirname, '..', '..');
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const ModePrompt = require(path.join(ROOT, 'app', 'static', 'js', 'game', 'mode-prompt.js'));
const Labels = require(path.join(ROOT, 'app', 'static', 'js', 'game', 'game-labels.js'));

const sandbox = { console };
sandbox.window = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(read('app/static/vendor/vue/vue.global.prod.js'), sandbox, { filename: 'vue.global.prod.js' });
const Vue = sandbox.Vue;

const ID = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const unbound = (id = ID) => ({ id, content_tier: 'adult' });
const bound = (id = ID) => ({ id, content_tier: 'safe' });
const safeOn = { effective: 'safe', forced: false };

/* `mode` 可以在測試中途改（物件的欄位被替換），模組每次都現讀。 */
function machine(initialMode = safeOn) {
  const navigations = [];
  const holder = { mode: initialMode };
  const prompt = ModePrompt.create(Vue, {
    mode: () => holder.mode,
    navigate: (target) => navigations.push(target),
    startPath: Labels.startPath,
    accountLink: Labels.accountLink,
  });
  return { prompt, navigations, holder };
}

// ---- 文案 ------------------------------------------------------------------------------------------

t('the copy has a title, a status code, two paragraphs and three answers, all in Traditional Chinese', () => {
  const copy = ModePrompt.copy();
  assert.equal(copy.code, 'SAFE_MODE');
  assert.equal(copy.title, '要調整安全模式嗎？');
  assert.equal(copy.body.length, 2);
  assert.deepEqual([copy.goSettings, copy.keepSafe, copy.cancel], ['前往設定', '維持安全模式繼續', '取消']);
  for (const line of copy.body) { assert.ok(/[一-鿿]/.test(line)); }
});

t('the copy is a fresh object every time, so changing one never leaks into the next', () => {
  const first = ModePrompt.copy();
  first.body.push('多出來的一行');
  first.title = '被改過';
  const second = ModePrompt.copy();
  assert.equal(second.body.length, 2);
  assert.equal(second.title, '要調整安全模式嗎？');
});

t('the copy never promises a result and never uses the old three mode names or age wording', () => {
  const text = JSON.stringify(ModePrompt.copy());
  assert.ok(text.includes('不保證'));
  for (const forbidden of ['一般模式', '成人模式', '未滿 18', '一定', '保證可以']) { assert.ok(!text.includes(forbidden), forbidden); }
});

t('Game.modePromptCopy in game-labels.js is a thin adapter for the very same copy', () => {
  assert.deepEqual(Labels.modePromptCopy(), ModePrompt.copy());
  assert.notEqual(Labels.modePromptCopy().body, Labels.modePromptCopy().body);              // 每次都是複本
  assert.ok(!/var MODE_PROMPT\b/.test(read('app/static/js/game/game-labels.js')), '文案只有一份，在 mode-prompt.js');
});

// ---- needsAsk ---------------------------------------------------------------------------------------

t('only an unbound script, safe mode on and no emergency brake is asked about', () => {
  assert.equal(ModePrompt.needsAsk(unbound(), safeOn), true);
  assert.equal(ModePrompt.needsAsk(bound(), safeOn), false);                                  // 綁定安全模式：不問
  assert.equal(ModePrompt.needsAsk(unbound(), { effective: 'adult', forced: false }), false); // 已關閉安全模式：不問
  assert.equal(ModePrompt.needsAsk(unbound(), { effective: 'safe', forced: true }), false);   // 緊急煞車：不問
  assert.equal(ModePrompt.needsAsk(unbound(), { effective: 'adult', forced: true }), false);
});

t('missing or odd data is never a reason to ask (the server decides the real mode)', () => {
  assert.equal(ModePrompt.needsAsk(unbound(), null), false);
  assert.equal(ModePrompt.needsAsk(unbound(), undefined), false);
  assert.equal(ModePrompt.needsAsk(null, safeOn), false);
  assert.equal(ModePrompt.needsAsk(undefined, safeOn), false);
  for (const tier of ['standard', 'mystery', undefined, null, 7, '']) {
    assert.equal(ModePrompt.needsAsk({ id: ID, content_tier: tier }, safeOn), false, String(tier));
  }
  assert.equal(ModePrompt.needsAsk(unbound(), { effective: 'mystery', forced: false }), false);
  assert.equal(ModePrompt.needsAsk(unbound(), { forced: false }), false);
});

// ---- 狀態機 -----------------------------------------------------------------------------------------

t('begin on an unbound script with safe mode on only opens the question and goes nowhere', () => {
  const { prompt, navigations } = machine();
  assert.equal(prompt.open.value, false);
  assert.equal(prompt.begin(unbound()), false);
  assert.equal(prompt.open.value, true);
  assert.equal(prompt.target.value.id, ID);
  assert.deepEqual(navigations, []);
});

t('begin on a bound script, or when the player already turned the safe mode off, goes straight to the wizard with no question', () => {
  for (const [script, mode] of [[bound(), safeOn], [unbound(), { effective: 'adult', forced: false }], [unbound(), { effective: 'safe', forced: true }], [unbound(), null]]) {
    const { prompt, navigations } = machine(mode);
    assert.equal(prompt.begin(script), true, JSON.stringify([script, mode]));
    assert.deepEqual(navigations, [`/game/start/${ID}`]);
    assert.equal(prompt.open.value, false);
  }
});

t('go to settings leaves for the account page and comes back to the wizard of this very script', () => {
  const { prompt, navigations } = machine();
  prompt.begin(unbound());
  assert.equal(prompt.goToSettings(), true);
  assert.deepEqual(navigations, [`/account?return=${encodeURIComponent('/game/start/' + ID)}`]);
  assert.equal(prompt.open.value, false);
  assert.equal(prompt.target.value, null);
});

t('keeping the safe mode goes to the wizard and changes nothing else', () => {
  const { prompt, navigations, holder } = machine();
  prompt.begin(unbound());
  assert.equal(prompt.keepSafe(), true);
  assert.deepEqual(navigations, [`/game/start/${ID}`]);
  assert.equal(prompt.open.value, false);
  assert.deepEqual(JSON.parse(JSON.stringify(holder.mode)), safeOn);                          // 模式資料沒被動過
});

t('cancel, and the modal model set to false (Esc or the overlay), stay on the page without going anywhere', () => {
  const { prompt, navigations } = machine();
  prompt.begin(unbound());
  prompt.cancel();
  assert.equal(prompt.open.value, false);
  prompt.begin(unbound());
  prompt.open.value = false;                                                                  // ui-modal 在 Esc 與點遮罩時送出 update:modelValue(false)
  assert.equal(prompt.target.value, null);
  assert.deepEqual(navigations, []);
});

t('setting the modal model to true on its own never opens a question', () => {
  const { prompt } = machine();
  prompt.open.value = true;
  assert.equal(prompt.open.value, false);
  assert.equal(prompt.target.value, null);
});

t('with no waiting question, go to settings and keep safe do nothing', () => {
  const { prompt, navigations } = machine();
  assert.equal(prompt.goToSettings(), false);
  assert.equal(prompt.keepSafe(), false);
  prompt.begin(unbound());
  prompt.cancel();
  assert.equal(prompt.goToSettings(), false);
  assert.equal(prompt.keepSafe(), false);
  assert.deepEqual(navigations, []);
});

t('a second begin replaces the waiting script instead of stacking questions, and the answer applies to the latest one', () => {
  const { prompt, navigations } = machine();
  prompt.begin(unbound(ID));
  prompt.begin(unbound(OTHER));
  assert.equal(prompt.target.value.id, OTHER);
  prompt.keepSafe();
  assert.deepEqual(navigations, [`/game/start/${OTHER}`]);
  assert.equal(prompt.keepSafe(), false);                                                     // 一個詢問只能回答一次
  assert.equal(navigations.length, 1);
});

t('a target without a string id is ignored (nothing opens, nothing navigates)', () => {
  const { prompt, navigations } = machine();
  for (const odd of [null, undefined, {}, { id: 7, content_tier: 'adult' }, { content_tier: 'adult' }, 'text']) {
    assert.equal(prompt.begin(odd), false, JSON.stringify(odd));
  }
  assert.equal(prompt.open.value, false);
  assert.deepEqual(navigations, []);
});

t('the mode is read at the moment of begin, so a change between two clicks is honoured', () => {
  const { prompt, navigations, holder } = machine();
  assert.equal(prompt.begin(unbound()), false);                                               // 安全模式開：問
  prompt.cancel();
  holder.mode = { effective: 'adult', forced: false };                                        // 玩家在別處關掉安全模式
  assert.equal(prompt.begin(unbound()), true);                                                // 這次不問
  assert.deepEqual(navigations, [`/game/start/${ID}`]);
});

t('the module touches no browser global: it runs in a sandbox with no window, location or storage', () => {
  const bare = vm.createContext({ console });
  vm.runInContext(read('app/static/js/game/mode-prompt.js'), bare, { filename: 'mode-prompt.js' });
  assert.equal(typeof bare.Game.modePrompt.create, 'function');                               // 瀏覽器載入方式：掛在全域的 Game.modePrompt
  assert.equal(typeof bare.Game.modePrompt.needsAsk, 'function');
  const source = read('app/static/js/game/mode-prompt.js').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.ok(!/\b(window|document|location|localStorage|sessionStorage|indexedDB|axios|fetch)\b/.test(source));
});

t('the shared template fragment is what both pages include, and it is driven by Vue state only', () => {
  const partial = read('app/templates/game/_mode_prompt.html');
  assert.ok(partial.includes('<ui-modal v-model="askOpen"'));
  assert.ok(!/v-html|data-bs-/.test(partial));
  const entry = read('app/templates/game/index.html');
  assert.ok(entry.includes('{% include "game/_mode_prompt.html" %}'));
  assert.ok(!entry.includes('<ui-modal v-model="askOpen"'), '入口頁不再內嵌一份');
});
