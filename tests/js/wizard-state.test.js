'use strict';
/* rpg-creation-wizard 規格的純函式（wizard-state.js）：草稿與聯繫的三態、字數與前進條件、示範氣泡、AI 預覽的套用、開存檔的本文、
 * 伺服器欄位錯誤對應回步驟、以及三種呼叫（載入、生成、開始冒險）失敗時的固定訊息。不需要瀏覽器。
 * 核心不變量：訊息只來自固定文字與列舉對照，**不含任何傳入的玩家文字**（用帶標記字串的輸入斷言）。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const FILE = path.join(__dirname, '..', '..', 'app', 'static', 'js', 'game', 'wizard-state.js');
const W = require(FILE);

const HAN = /[一-鿿]/;
const MARK = 'ZZ-玩家文字-ZZ';
const LIMITS = {
  name: 20, personality: 60, age_max: 9999, appearance: 150, occupation: 20, speech_style: 100, hidden_trait: 60, intro: 300, anchor_text: 60,
};

function json(value) { return JSON.parse(JSON.stringify(value)); }       // vm 沙盒外沒有跨 realm 的問題，這裡只為了斷言前後不被修改

function filled(overrides) {
  return Object.assign(W.blankDraft(), { name: '林初', personality: '沉穩', age: '17', appearance: '清瘦的少年' }, overrides || {});
}

function completeAnchor(overrides) {
  return Object.assign(W.blankAnchor(), { kind: 'character', ref: '沈清河', text: '同門的師兄' }, overrides || {});
}

function server(status, code, extra) {
  return Object.assign({ kind: 'server', message: '', fields: {}, code: code || '', status: status }, extra || {});
}

// ---- 步驟與欄位 -------------------------------------------------------------------------------------

test('there are exactly three steps in the order prologue, contact, create, each with a Traditional Chinese label', () => {
  assert.deepEqual(W.STEPS.map((step) => step.id), ['prologue', 'contact', 'create']);
  assert.deepEqual(W.STEPS.map((step) => step.label), ['序章', '聯繫', '創角']);
  assert.deepEqual(['prologue', 'contact', 'create', 'x', undefined].map(W.stepIndex), [0, 1, 2, -1, -1]);
});

test('the card has the seven shared fields and no avatar or gender field', () => {
  assert.deepEqual(W.CARD_FIELDS.map((field) => field.key), ['name', 'personality', 'age', 'appearance', 'occupation', 'speech_style', 'hidden_trait']);
  assert.deepEqual(W.CARD_FIELDS.filter((field) => field.required).map((field) => field.key), ['name', 'personality', 'age', 'appearance']);
  for (const field of W.CARD_FIELDS) { assert.ok(HAN.test(field.label), field.key); }
  const serialized = JSON.stringify(W.CARD_FIELDS) + JSON.stringify(W.blankDraft());
  assert.ok(!/gender|sex|avatar|image|性別|頭像/i.test(serialized));
  assert.equal(W.CARD_FIELDS.find((field) => field.key === 'age').hint.includes('與您本人的年齡無關'), true);
});

test('a blank draft is all empty strings with the native origin, and every call returns a fresh object', () => {
  const draft = W.blankDraft();
  assert.deepEqual(Object.keys(draft).sort(), ['age', 'appearance', 'hidden_trait', 'intro', 'name', 'occupation', 'origin', 'personality', 'speech_style']);
  for (const [key, value] of Object.entries(draft)) { assert.equal(value, key === 'origin' ? 'native' : '', key); }
  draft.name = '改過';
  assert.equal(W.blankDraft().name, '');
});

// ---- 聯繫的三態 -------------------------------------------------------------------------------------

test('anchorState tells unset, a half-made choice either way, complete and skipped apart', () => {
  assert.equal(W.anchorState(W.blankAnchor()), 'unset');
  assert.equal(W.anchorState(completeAnchor()), 'complete');
  assert.equal(W.anchorState(completeAnchor({ text: '   ' })), 'text_missing');
  assert.equal(W.anchorState(completeAnchor({ kind: '', ref: '' })), 'target_missing');
  assert.equal(W.anchorState(completeAnchor({ ref: '  ' })), 'target_missing');
  assert.equal(W.anchorState(W.skippedAnchor()), 'skipped');
  assert.equal(W.anchorState(Object.assign(completeAnchor(), { skipped: true })), 'skipped');
  for (const junk of [undefined, null, 0, 'x', [], {}]) { assert.equal(W.anchorState(junk), 'unset', String(junk)); }
});

// ---- 字數 -------------------------------------------------------------------------------------------

test('fieldCounts counts Unicode characters of the trimmed text, with remaining and over, for the counted fields and the intro', () => {
  const counts = W.fieldCounts(filled({ name: '  林初  ', personality: '字'.repeat(61), intro: '😀'.repeat(3) }), LIMITS);
  assert.deepEqual(Object.keys(counts).sort(), ['appearance', 'hidden_trait', 'intro', 'name', 'occupation', 'personality', 'speech_style']);
  assert.deepEqual(json(counts.name), { count: 2, max: 20, remaining: 18, over: false });
  assert.deepEqual(json(counts.personality), { count: 61, max: 60, remaining: -1, over: true });
  assert.deepEqual(json(counts.intro), { count: 3, max: 300, remaining: 297, over: false });    // 一個表情符號算一個字元
  assert.ok(!('age' in counts));                                                                  // 年齡是數字，不計字數
});

test('exactly at the limit is not over, one more is', () => {
  assert.equal(W.fieldCounts(filled({ name: '名'.repeat(20) }), LIMITS).name.over, false);
  assert.equal(W.fieldCounts(filled({ name: '名'.repeat(21) }), LIMITS).name.over, true);
});

test('a decomposed character counts as one after NFC normalization', () => {
  assert.equal(W.charCount('é'), 1);          // é（單一字元）
  assert.equal(W.charCount('é'), 1);         // e 加上組合用的重音符號
});

test('missing or junk limits never make a field over', () => {
  for (const limits of [undefined, null, {}, { name: 0 }, { name: -3 }, { name: 'x' }, { name: NaN }]) {
    const counts = W.fieldCounts(filled({ name: '名'.repeat(500) }), limits);
    assert.equal(counts.name.max, null);
    assert.equal(counts.name.remaining, null);
    assert.equal(counts.name.over, false);
  }
});

test('overLimit lists the fields over their limit in card order with the intro last', () => {
  assert.deepEqual(W.overLimit(filled(), LIMITS), []);
  assert.deepEqual(W.overLimit(filled({ intro: '字'.repeat(301), appearance: '字'.repeat(151), name: '字'.repeat(21) }), LIMITS), ['name', 'appearance', 'intro']);
});

test('anchorCount uses the anchor text limit', () => {
  assert.deepEqual(json(W.anchorCount(completeAnchor({ text: '字'.repeat(61) }), LIMITS)), { count: 61, max: 60, remaining: -1, over: true });
  assert.equal(W.anchorCount(completeAnchor(), {}).over, false);
});

// ---- 前進條件 ---------------------------------------------------------------------------------------

test('the prologue has no condition', () => {
  assert.equal(W.canAdvance('prologue', W.blankDraft(), W.blankAnchor(), LIMITS), true);
  assert.deepEqual(W.blockers('prologue', W.blankDraft(), W.blankAnchor(), LIMITS), []);
});

test('the contact step needs a complete choice or an explicit skip', () => {
  const unset = W.blockers('contact', W.blankDraft(), W.blankAnchor(), LIMITS);
  assert.deepEqual(unset.map((b) => b.code), ['anchor_unset']);
  assert.deepEqual(W.blockers('contact', W.blankDraft(), completeAnchor({ text: '' }), LIMITS).map((b) => [b.code, b.field]), [['anchor_text_missing', 'anchor_text']]);
  assert.deepEqual(W.blockers('contact', W.blankDraft(), completeAnchor({ kind: '', ref: '' }), LIMITS).map((b) => b.code), ['anchor_target_missing']);
  assert.equal(W.canAdvance('contact', W.blankDraft(), completeAnchor(), LIMITS), true);
  assert.equal(W.canAdvance('contact', W.blankDraft(), W.skippedAnchor(), LIMITS), true);
  const over = W.blockers('contact', W.blankDraft(), completeAnchor({ text: '字'.repeat(61) }), LIMITS);
  assert.deepEqual(over.map((b) => b.code), ['anchor_over']);
  assert.ok(over[0].message.includes('60'));
  for (const blocker of unset) { assert.equal(blocker.step, 'contact'); assert.ok(HAN.test(blocker.message)); }
});

test('the create step needs the four required fields, nothing over the limit and a finished contact', () => {
  const empty = W.blockers('create', W.blankDraft(), W.skippedAnchor(), LIMITS);
  assert.deepEqual(empty.map((b) => [b.code, b.field, b.step]), [
    ['missing', 'name', 'create'], ['missing', 'personality', 'create'], ['missing', 'age', 'create'], ['missing', 'appearance', 'create'],
  ]);
  assert.equal(empty[0].message, '請填寫「姓名」。');
  assert.equal(W.canAdvance('create', filled(), W.skippedAnchor(), LIMITS), true);
  assert.equal(W.canAdvance('create', filled(), completeAnchor(), LIMITS), true);
  assert.equal(W.canAdvance('create', filled({ occupation: '', speech_style: '', hidden_trait: '' }), completeAnchor(), LIMITS), true);   // 選填欄位可以空
  assert.equal(W.canAdvance('create', filled({ personality: '字'.repeat(61) }), completeAnchor(), LIMITS), false);
  assert.equal(W.blockers('create', filled({ personality: '字'.repeat(61) }), completeAnchor(), LIMITS)[0].code, 'over');
  assert.deepEqual(W.blockers('create', filled(), W.blankAnchor(), LIMITS).map((b) => b.code), ['anchor_unset']);          // 沒有聯繫也沒有略過：提示回聯繫
  assert.equal(W.canAdvance('create', filled({ name: '   ' }), W.skippedAnchor(), LIMITS), false);                            // 只有空白等於沒填
});

test('an over-long intro does not block starting, only the generation', () => {
  assert.equal(W.canAdvance('create', filled({ intro: '字'.repeat(999) }), W.skippedAnchor(), LIMITS), true);
  assert.deepEqual(W.overLimit(filled({ intro: '字'.repeat(999) }), LIMITS), ['intro']);
});

test('an unknown step has no blockers', () => {
  assert.deepEqual(W.blockers('nope', W.blankDraft(), W.blankAnchor(), LIMITS), []);
});

test('hasManualContent is true for any card text or a touched contact, but not for origin and intro alone', () => {
  assert.equal(W.hasManualContent(W.blankDraft(), W.blankAnchor()), false);
  assert.equal(W.hasManualContent(Object.assign(W.blankDraft(), { origin: 'transmigrator', intro: '我想當劍客' }), W.blankAnchor()), false);
  assert.equal(W.hasManualContent(Object.assign(W.blankDraft(), { age: '17' }), W.blankAnchor()), true);
  assert.equal(W.hasManualContent(W.blankDraft(), W.skippedAnchor()), true);
  assert.equal(W.hasManualContent(W.blankDraft(), completeAnchor({ text: '' })), true);
  assert.equal(W.hasManualContent(Object.assign(W.blankDraft(), { name: '   ' }), W.blankAnchor()), false);
});

// ---- 示範氣泡與聯繫對象 -----------------------------------------------------------------------------

test('demo bubbles are a fixed sentence pattern around the name and the speech style', () => {
  const bubbles = W.demoBubbles(filled({ name: ' 林初 ', speech_style: ' 話少，語氣平靜 ' }));
  assert.equal(bubbles.length, 2);
  assert.equal(bubbles[0].kind, 'narration');
  assert.equal(bubbles[0].text, '林初站在路口，四周的人聲漸漸安靜下來。');
  assert.equal(bubbles[1].speaker, '林初');
  assert.equal(bubbles[1].note, '說話風格：話少，語氣平靜');
  assert.ok(!JSON.stringify(W.demoBubbles(filled({ speech_style: '' }))).includes('說話風格'));
});

test('demo bubbles fall back to a neutral name and never touch the draft', () => {
  const draft = W.blankDraft();
  const before = JSON.stringify(draft);
  assert.ok(W.demoBubbles(draft)[0].text.startsWith('你的主角'));
  assert.equal(JSON.stringify(draft), before);
  assert.ok(W.demoBubbles(undefined)[0].text.startsWith('你的主角'));
});

test('target groups hold only names and occupations for characters, and skip junk and empty groups', () => {
  const groups = W.targetGroups({
    characters: [{ name: '沈清河', occupation: '宗門師兄', age: 99, hidden_trait: MARK }, { name: '', occupation: '無名' }, null, 'x', { name: '柳青', occupation: '' }],
    locations: ['青雲山', '', 5, '落霞鎮'],
    milestones: [{ id: 'm1', title: '宗門大測' }, { id: 'm2', title: '' }, { title: '沒有編號' }],
  });
  assert.deepEqual(groups.map((g) => [g.kind, g.label]), [['character', '人物'], ['location', '地點'], ['milestone', '里程碑']]);
  assert.deepEqual(json(groups[0].options), [
    { kind: 'character', ref: '沈清河', label: '沈清河', detail: '宗門師兄' }, { kind: 'character', ref: '柳青', label: '柳青', detail: '' },
  ]);
  assert.deepEqual(groups[1].options.map((o) => o.ref), ['青雲山', '落霞鎮']);
  assert.deepEqual(groups[2].options.map((o) => [o.ref, o.label]), [['m1', '宗門大測'], ['m2', 'm2']]);
  assert.ok(!JSON.stringify(groups).includes(MARK) && !JSON.stringify(groups).includes('99'));        // 年齡與隱藏特質不會出現在選項裡
});

test('target groups for an empty or junk setup are empty', () => {
  for (const junk of [undefined, null, {}, 'x', { characters: 'x', locations: 3, milestones: null }]) { assert.deepEqual(W.targetGroups(junk), []); }
  assert.deepEqual(W.targetGroups({ locations: ['青雲山'] }).map((g) => g.kind), ['location']);
});

// ---- AI 預覽 ----------------------------------------------------------------------------------------

const SUGGESTION = {
  ok: true, remaining: 2,
  protagonist: { name: '蘇晚', personality: '外冷內熱', age: 19, appearance: '短髮，眼神清亮', occupation: null, speech_style: '簡短直接', hidden_trait: '怕黑', image: null },
  anchor: { kind: 'location', ref: '青雲山', text: '自幼在山腳長大' },
};

test('applySuggestion overwrites the card and the contact and keeps the chosen origin and the intro', () => {
  const draft = filled({ origin: 'transmigrator', intro: '我想當劍客', occupation: '舊的職業' });
  const { draft: next, anchor } = W.applySuggestion(draft, completeAnchor(), SUGGESTION);
  assert.deepEqual(json(next), {
    name: '蘇晚', personality: '外冷內熱', age: '19', appearance: '短髮，眼神清亮', occupation: '', speech_style: '簡短直接', hidden_trait: '怕黑',
    origin: 'transmigrator', intro: '我想當劍客',
  });
  assert.deepEqual(json(anchor), { skipped: false, kind: 'location', ref: '青雲山', text: '自幼在山腳長大' });
});

test('a suggestion replaces a skipped contact', () => {
  const { anchor } = W.applySuggestion(filled(), W.skippedAnchor(), SUGGESTION);
  assert.equal(W.anchorState(anchor), 'complete');
});

test('applySuggestion never mutates its inputs and returns new objects', () => {
  const draft = filled();
  const anchor = completeAnchor();
  const before = JSON.stringify([draft, anchor, SUGGESTION]);
  const result = W.applySuggestion(draft, anchor, SUGGESTION);
  assert.equal(JSON.stringify([draft, anchor, SUGGESTION]), before);
  assert.notEqual(result.draft, draft);
  assert.notEqual(result.anchor, anchor);
});

test('a broken suggestion does not wipe what the player wrote', () => {
  const draft = filled({ intro: '自我介紹' });
  const anchor = completeAnchor();
  for (const junk of [undefined, null, {}, 'x', { protagonist: 'x', anchor: 3 }]) {
    const result = W.applySuggestion(draft, anchor, junk);
    assert.deepEqual(json(result.draft), json(draft));
    assert.deepEqual(json(result.anchor), json(anchor));
  }
  const noAnchor = W.applySuggestion(draft, anchor, { protagonist: SUGGESTION.protagonist, anchor: { kind: 'nope', ref: 'x', text: 'y' } });
  assert.equal(noAnchor.draft.name, '蘇晚');                                    // 角色卡照常套用
  assert.deepEqual(json(noAnchor.anchor), json(anchor));                        // 不合格的聯繫不蓋掉原本的
});

test('a suggestion cannot decide the origin even if it carries one', () => {
  const card = Object.assign({}, SUGGESTION.protagonist, { origin: 'transmigrator' });
  const { draft } = W.applySuggestion(filled(), W.blankAnchor(), Object.assign({}, SUGGESTION, { protagonist: card }));
  assert.equal(draft.origin, 'native');
});

// ---- 開存檔與生成的本文 ----------------------------------------------------------------------------

test('the start payload carries the card with the origin and the contact, and nothing about avatars or gender', () => {
  const payload = W.buildStartPayload('v-1', filled({ name: ' 林初 ', occupation: ' 學徒 ', speech_style: '', hidden_trait: '   ', origin: 'transmigrator' }), completeAnchor({ text: ' 同門的師兄 ' }));
  assert.deepEqual(json(payload), {
    version_id: 'v-1',
    protagonist: { name: '林初', personality: '沉穩', age: 17, appearance: '清瘦的少年', occupation: '學徒', speech_style: null, hidden_trait: null, origin: 'transmigrator' },
    anchor: { kind: 'character', ref: '沈清河', text: '同門的師兄' },
  });
  assert.ok(!('skip_anchor' in payload));
  assert.ok(!/gender|avatar|image/.test(JSON.stringify(payload)));
});

test('skipping sends skip_anchor true and no anchor at all', () => {
  const payload = W.buildStartPayload('v-1', filled(), W.skippedAnchor());
  assert.equal(payload.skip_anchor, true);
  assert.ok(!('anchor' in payload));
  assert.deepEqual(Object.keys(payload).sort(), ['protagonist', 'skip_anchor', 'version_id']);
});

test('skipping wins even if a target was chosen before', () => {
  const payload = W.buildStartPayload('v-1', filled(), Object.assign(completeAnchor(), { skipped: true }));
  assert.equal(payload.skip_anchor, true);
  assert.ok(!('anchor' in payload));
});

test('an unset contact sends anchor null so the server points at it, and a half-made one is sent as typed', () => {
  const unset = W.buildStartPayload('v-1', filled(), W.blankAnchor());
  assert.equal(unset.anchor, null);
  assert.ok(!('skip_anchor' in unset));
  const half = W.buildStartPayload('v-1', filled(), completeAnchor({ text: '  ' }));
  assert.deepEqual(json(half.anchor), { kind: 'character', ref: '沈清河', text: '' });
});

test('the age is a number when it is an integer and sent as typed otherwise so the server names the field', () => {
  const age = (value) => W.buildStartPayload('v', filled({ age: value }), W.skippedAnchor()).protagonist.age;
  assert.equal(age('17'), 17);
  assert.equal(age(' 0 '), 0);
  assert.equal(age(''), null);
  assert.equal(age('17.5'), '17.5');
  assert.equal(age('-1'), '-1');
  assert.equal(age('十七'), '十七');
});

test('an unknown origin falls back to native and the draft is never modified', () => {
  const draft = Object.assign(filled(), { origin: 'wizard' });
  const before = JSON.stringify(draft);
  assert.equal(W.buildStartPayload('v', draft, W.skippedAnchor()).protagonist.origin, 'native');
  assert.equal(JSON.stringify(draft), before);
});

test('the suggest payload carries the intro, or null when it is blank', () => {
  assert.deepEqual(json(W.buildSuggestPayload('v-1', filled({ intro: ' 我想當劍客 ' }))), { version_id: 'v-1', intro: '我想當劍客' });
  assert.equal(W.buildSuggestPayload('v-1', filled({ intro: '   ' })).intro, null);
  assert.equal(W.buildSuggestPayload('v-1', undefined).intro, null);
});

// ---- 欄位路徑 ---------------------------------------------------------------------------------------

test('stepOfField sends card and intro errors to the create step, anchor errors to the contact step and the rest nowhere', () => {
  for (const p of ['protagonist.age', 'protagonist.origin', 'protagonist', 'intro']) { assert.equal(W.stepOfField(p), 'create', p); }
  for (const p of ['anchor', 'anchor.text', 'anchor.ref', 'anchor.kind', 'anchor.foo']) { assert.equal(W.stepOfField(p), 'contact', p); }
  for (const p of ['version_id', 'skip_anchor', '', 'protagonists', 'anchors.text', undefined, null, 7]) { assert.equal(W.stepOfField(p), null, String(p)); }
});

test('fieldKeyOf maps a path to the form field and groups the unknown subfields', () => {
  assert.equal(W.fieldKeyOf('protagonist.age'), 'age');
  assert.equal(W.fieldKeyOf('protagonist.origin'), 'origin');
  assert.equal(W.fieldKeyOf('protagonist.image'), 'protagonist');
  assert.equal(W.fieldKeyOf('protagonist'), 'protagonist');
  assert.equal(W.fieldKeyOf('anchor.text'), 'anchor_text');
  assert.equal(W.fieldKeyOf('anchor.ref'), 'anchor_ref');
  assert.equal(W.fieldKeyOf('anchor.kind'), 'anchor_kind');
  assert.equal(W.fieldKeyOf('anchor.foo'), 'anchor');
  assert.equal(W.fieldKeyOf('anchor'), 'anchor');
  assert.equal(W.fieldKeyOf('intro'), 'intro');
  assert.equal(W.fieldKeyOf('version_id'), null);
  assert.equal(W.fieldKeyOf(undefined), null);
});

// ---- 開始冒險失敗 -----------------------------------------------------------------------------------

test('a 400 puts each server message on its field and goes back to the step that holds it', () => {
  const failure = W.describeStartFailure(server(400, 'validation_error', { fields: { 'protagonist.age': '年齡必須是整數。' } }));
  assert.equal(failure.kind, 'fields');
  assert.equal(failure.step, 'create');
  assert.deepEqual(json(failure.fields), { age: '年齡必須是整數。' });
  assert.equal(failure.message, '請修正標示的欄位後再試。');
});

test('a 400 with errors on both steps goes back to the earlier one', () => {
  const failure = W.describeStartFailure(server(400, 'validation_error', {
    fields: { 'protagonist.name': '最多 20 字。', 'anchor.text': '這個欄位為必填。', 'anchor.ref': '這個對象不在劇本中。' },
  }));
  assert.equal(failure.step, 'contact');
  assert.deepEqual(Object.keys(failure.fields).sort(), ['anchor_ref', 'anchor_text', 'name']);
});

test('a 400 about the whole anchor goes to the contact step', () => {
  const failure = W.describeStartFailure(server(400, 'validation_error', { fields: { anchor: '請提供與劇本的聯繫，或明確選擇略過。' } }));
  assert.equal(failure.step, 'contact');
  assert.deepEqual(Object.keys(failure.fields), ['anchor']);
});

test('a 400 with no field I can show keeps the step and says something is wrong', () => {
  for (const fields of [{}, { version_id: '找不到。' }, undefined, null]) {
    const failure = W.describeStartFailure(server(400, 'validation_error', { fields }));
    assert.equal(failure.kind, 'fields');
    assert.equal(failure.step, null);
    assert.deepEqual(json(failure.fields), {});
    assert.equal(failure.message, '送出的資料有問題，請檢查後再試。');
  }
});

test('a 422 names only the known review categories, for the card and for the script', () => {
  const card = W.describeStartFailure(server(422, 'protagonist_rejected', { categories: ['minor_sexual', 'non_consensual', 'minor_sexual'] }));
  assert.equal(card.kind, 'rejected');
  assert.equal(card.message, '主角的內容未通過內容規範（涉及未成年角色的性內容、非自願的性情節），請修改後再試。');
  assert.equal(card.step, null);
  const script = W.describeStartFailure(server(422, 'script_rejected', { categories: ['sexual_violence'] }));
  assert.equal(script.message, '這個劇本目前無法用來開始冒險，因為它沒有通過內容審查（性暴力）。');
  assert.equal(W.describeStartFailure(server(422, 'protagonist_rejected', { categories: [] })).message, '主角的內容未通過內容規範，請修改後再試。');
});

test('a 503 distinguishes the review being unavailable from any other outage', () => {
  assert.equal(W.describeStartFailure(server(503, 'review_unavailable')).message, '內容審查暫時無法使用，請稍後再試。');
  assert.equal(W.describeStartFailure(server(503, 'system_busy')).message, '服務暫時無法使用，請稍後再試。');
  for (const code of ['review_unavailable', 'system_busy', '']) { assert.equal(W.describeStartFailure(server(503, code)).kind, 'unavailable'); }
});

test('the 403 codes each get their own explanation and link, and an unknown one still blocks', () => {
  const cases = {
    content_tier_restricted: ['高於您目前的模式', '/account'],
    age_confirmation_required: ['完成年齡確認', '/account'],
    age_restricted: ['年齡目前不符合', null],
    account_locked: ['被鎖定', '/appeal'],
    account_deletion_pending: ['等待刪除', '/account'],
    something_new: ['無法開始冒險', '/account'],
    '': ['無法開始冒險', '/account'],
  };
  for (const [code, [fragment, href]] of Object.entries(cases)) {
    const failure = W.describeStartFailure(server(403, code));
    assert.equal(failure.kind, 'gate', code);
    assert.ok(failure.message.includes(fragment), `${code}: ${failure.message}`);
    assert.equal(failure.link ? failure.link.href : null, href, code);
  }
});

test('404, 401, 429 and an unexpected status each have a fixed message', () => {
  const missing = W.describeStartFailure(server(404, 'not_found'));
  assert.equal(missing.kind, 'not_found');
  assert.deepEqual(json(missing.link), { href: '/game', text: '回到冒險' });
  assert.equal(W.describeStartFailure(server(401, 'unauthorized')).kind, 'login');
  assert.equal(W.describeStartFailure(server(429, 'too_many_requests')).kind, 'limit');
  for (const status of [409, 500, 502, 418]) { assert.equal(W.describeStartFailure(server(status, 'x')).kind, 'unknown', String(status)); }
});

test('a timeout or a dropped connection warns that the server may have finished, and a cancel says nothing', () => {
  const timeout = W.describeStartFailure({ kind: 'timeout', message: '', fields: {}, code: 'timeout', status: 0 });
  assert.equal(timeout.kind, 'timeout');
  assert.ok(timeout.message.includes('我的存檔') && timeout.message.includes('還在這個頁面'));
  const network = W.describeStartFailure({ kind: 'network', message: '', fields: {}, code: 'network', status: 0 });
  assert.equal(network.kind, 'network');
  assert.ok(network.message.includes('我的存檔'));
  const canceled = W.describeStartFailure({ kind: 'canceled', message: '', fields: {}, code: 'canceled', status: 0 });
  assert.equal(canceled.kind, 'canceled');
  assert.equal(canceled.message, '');
  const csrf = W.describeStartFailure({ kind: 'csrf', message: '', fields: {}, code: 'csrf_failed', status: 403 });
  assert.equal(csrf.kind, 'csrf');
  assert.ok(csrf.message.includes('重新整理'));
});

test('junk failures never throw and are treated as unexpected', () => {
  for (const junk of [undefined, null, 'x', 7, [], {}]) {
    assert.equal(W.describeStartFailure(junk).kind, 'unknown');
    assert.equal(W.describeSuggestFailure(junk).kind, 'unknown');
    assert.equal(W.describeSetupFailure(junk).kind, 'unavailable');
  }
});

// ---- 生成失敗 ---------------------------------------------------------------------------------------

test('a suggestion that ran out of tries says so and a generic 429 does not', () => {
  const out = W.describeSuggestFailure(server(429, 'suggestion_limit'));
  assert.equal(out.kind, 'limit');
  assert.equal(out.code, 'suggestion_limit');
  assert.ok(out.message.includes('次數已經用完') && out.message.includes('自己創角'));
  assert.equal(W.describeSuggestFailure(server(429, 'too_many_requests')).message, '操作太頻繁，請稍後再試。');
});

test('suggestion_unavailable tells the player to create the character themselves, and the content stays', () => {
  const failure = W.describeSuggestFailure(server(503, 'suggestion_unavailable'));
  assert.equal(failure.kind, 'unavailable');
  assert.ok(failure.message.includes('直接自己創角') && failure.message.includes('沒有變動'));
  assert.ok(W.describeSuggestFailure(server(503, 'system_busy')).message.includes('沒有變動'));
});

test('a rejected intro names only the categories and never the intro', () => {
  const failure = W.describeSuggestFailure(server(422, 'protagonist_rejected', { categories: ['real_person_sexual', MARK] }));
  assert.equal(failure.kind, 'rejected');
  assert.equal(failure.message, '您的自我介紹未通過內容規範（以真人名義的性內容），請修改後再試，或直接自己創角。');
});

test('an over-long intro is shown on the intro field of the create step', () => {
  const failure = W.describeSuggestFailure(server(400, 'validation_error', { fields: { intro: '最多 300 字。' } }));
  assert.equal(failure.step, 'create');
  assert.deepEqual(json(failure.fields), { intro: '最多 300 字。' });
});

test('suggestion timeouts and cancels keep the content and cancels are silent', () => {
  assert.ok(W.describeSuggestFailure({ kind: 'timeout', message: '', fields: {}, code: 'timeout', status: 0 }).message.includes('沒有變動'));
  assert.ok(W.describeSuggestFailure({ kind: 'network', message: '', fields: {}, code: 'network', status: 0 }).message.includes('沒有變動'));
  assert.equal(W.describeSuggestFailure({ kind: 'canceled', message: '', fields: {}, code: 'canceled', status: 0 }).message, '');
  assert.equal(W.describeSuggestFailure(server(500, 'internal_error')).kind, 'unknown');
});

// ---- 載入失敗 ---------------------------------------------------------------------------------------

test('loading failures: not found offers the way home, a tier mismatch offers the account page, outages offer a retry', () => {
  const missing = W.describeSetupFailure(server(404, 'not_found'));
  assert.equal(missing.kind, 'not_found');
  assert.equal(missing.link.href, '/game');
  assert.equal(missing.retry, false);
  const tier = W.describeSetupFailure(server(403, 'content_tier_restricted'));
  assert.equal(tier.code, 'content_tier_restricted');
  assert.equal(tier.link.href, '/account');
  assert.equal(tier.retry, false);
  for (const info of [server(500, 'internal_error'), server(503, 'system_busy'), { kind: 'network', message: '', fields: {}, code: 'network', status: 0 }, { kind: 'timeout', message: '', fields: {}, code: 'timeout', status: 0 }]) {
    assert.equal(W.describeSetupFailure(info).retry, true, JSON.stringify(info));
  }
});

// ---- 訊息不含玩家文字 ------------------------------------------------------------------------------

test('no message or category text ever carries text from the failure, only fixed wording and enum mappings', () => {
  const infos = [
    server(400, 'validation_error', { message: MARK, categories: [MARK] }),
    server(401, 'unauthorized', { message: MARK }),
    server(403, MARK, { message: MARK }),
    server(403, 'content_tier_restricted', { message: MARK }),
    server(404, 'not_found', { message: MARK }),
    server(422, 'protagonist_rejected', { message: MARK, categories: [MARK, 'minor_sexual', 7, null, { x: MARK }] }),
    server(422, 'script_rejected', { message: MARK, categories: [MARK] }),
    server(422, MARK, { message: MARK, categories: [MARK] }),
    server(429, 'suggestion_limit', { message: MARK }),
    server(503, 'review_unavailable', { message: MARK }),
    server(503, 'suggestion_unavailable', { message: MARK }),
    server(500, MARK, { message: MARK }),
    { kind: 'timeout', message: MARK, fields: {}, code: 'timeout', status: 0 },
    { kind: 'network', message: MARK, fields: {}, code: 'network', status: 0 },
    { kind: 'csrf', message: MARK, fields: {}, code: 'csrf_failed', status: 403 },
    { kind: 'canceled', message: MARK, fields: {}, code: 'canceled', status: 0 },
  ];
  for (const describe of [W.describeStartFailure, W.describeSuggestFailure, W.describeSetupFailure]) {
    for (const info of infos) {
      const result = describe(info);
      assert.ok(!JSON.stringify(result).includes(MARK), `${describe.name}: ${JSON.stringify(info)} -> ${JSON.stringify(result)}`);
      assert.equal(typeof result.message, 'string');
      if (result.kind !== 'canceled') { assert.ok(HAN.test(result.message), result.message); }
    }
  }
});

test('the only server text that is passed through is the per-field rule description of a 400', () => {
  const failure = W.describeStartFailure(server(400, 'validation_error', { message: MARK, fields: { 'protagonist.name': '最多 20 字。' } }));
  assert.ok(!failure.message.includes(MARK));
  assert.equal(failure.fields.name, '最多 20 字。');
});

test('every failure has the same shape, links stay on this site and no text mentions raw codes', () => {
  const infos = [server(400, 'x'), server(401, 'x'), server(403, 'content_tier_restricted'), server(404, 'x'), server(422, 'script_rejected'), server(429, 'x'), server(503, 'x'), server(500, 'x')];
  for (const describe of [W.describeStartFailure, W.describeSuggestFailure, W.describeSetupFailure]) {
    for (const info of infos) {
      const result = describe(info);
      assert.deepEqual(Object.keys(result).sort(), ['code', 'fields', 'kind', 'link', 'message', 'retry', 'step']);
      if (result.link) { assert.match(result.link.href, /^\/[a-z]+$/); assert.ok(HAN.test(result.link.text)); }
      assert.ok(!/[a-z]+_[a-z_]+/.test(result.message), result.message);        // 不把 account_locked 這類代碼顯示給玩家
    }
  }
});

test('failure descriptions do not share state between calls', () => {
  const first = W.describeSetupFailure(server(404, 'not_found'));
  first.link.href = '/evil';
  assert.equal(W.describeSetupFailure(server(404, 'not_found')).link.href, '/game');
  const gate = W.describeStartFailure(server(403, 'account_locked'));
  gate.link.href = '/evil';
  assert.equal(W.describeStartFailure(server(403, 'account_locked')).link.href, '/appeal');
});

// ---- 這個模組沒有副作用 ----------------------------------------------------------------------------

test('the module has no network, storage, logging or DOM access', () => {
  const source = fs.readFileSync(FILE, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(?<![:\w])\/\/[^\n]*/g, '');
  for (const word of ['axios', 'fetch(', 'XMLHttpRequest', 'localStorage', 'sessionStorage', 'indexedDB', 'document', 'window', 'console', 'innerHTML', 'eval(', 'location.']) {      // 'location' 單獨會誤中「地點」的種類值，所以只擋 `location.`（導向與讀網址）
    assert.ok(!source.includes(word), word);
  }
});
