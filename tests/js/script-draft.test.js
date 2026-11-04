'use strict';
/* script-authoring 規格的前端部分：草稿模型（表單資料 <-> 要送出的內容）、字數、欄位路徑的中文名稱與審查拒絕的說明。
 * 伺服器是唯一的驗證者；這裡只驗證「搬運」是否正確、不吞掉也不改寫使用者輸入。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const Draft = require('../../app/static/js/scripts/script-draft.js');

const SERVER_CONTENT = {
  spec_version: 1, title: '青雲問道', summary: '少年入宗門', world_name: '乾歷朝', content_tier: 'standard', background: '靈氣復甦的年代。',
  opening: '山門前，晨霧未散。', rules: '境界分為煉氣、築基。', tags: ['仙俠', '成長'], calendar: { name: '乾歷朝', start_year: 1200 },
  ending_mode: 'custom', ending_text: '飛升而去。', ending_age: 90,
  characters: [{ name: '林曉', age: 16, personality: '活潑', appearance: '瘦小', occupation: null, speech_style: null, hidden_trait: '身世成謎', image: null }],
  locations: ['古劍山莊', '青雲宗'],
  milestones: [{ id: 'm1', title: '宗門大測', note: null, trigger: { kind: 'age', value: 20 } }], cover_image: null
};

test('a server content round-trips through the draft without losing or inventing anything', () => {
  const content = Draft.toContent(Draft.toDraft(SERVER_CONTENT));
  assert.equal(content.title, '青雲問道');
  assert.equal(content.content_tier, 'standard');
  assert.deepEqual(content.tags, ['仙俠', '成長']);
  assert.deepEqual(content.calendar, { name: '乾歷朝', start_year: 1200 });
  assert.deepEqual([content.ending_mode, content.ending_text, content.ending_age], ['custom', '飛升而去。', 90]);
  assert.deepEqual(content.characters, [{
    name: '林曉', age: 16, personality: '活潑', appearance: '瘦小', occupation: null, speech_style: null, hidden_trait: '身世成謎'
  }]);
  assert.deepEqual(content.locations, ['古劍山莊', '青雲宗']);
  assert.deepEqual(content.milestones, [{ title: '宗門大測', note: null, trigger: { kind: 'age', value: 20 }, id: 'm1' }]);
});

test('a blank draft sends empty optional fields as null and no list items', () => {
  const content = Draft.toContent(Draft.blankDraft());
  assert.equal(content.opening, null);
  assert.equal(content.rules, null);
  assert.equal(content.calendar, null);
  assert.deepEqual([content.tags, content.characters, content.locations, content.milestones], [[], [], [], []]);
  assert.deepEqual([content.ending_mode, content.ending_text, content.ending_age], ['none', null, null]);
  assert.equal(content.content_tier, 'safe');
});

test('whitespace is trimmed but the text itself is never rewritten', () => {
  const draft = Draft.blankDraft();
  draft.title = '  標題  ';
  draft.background = '\n第一行\n第二行\n';
  draft.opening = '   ';
  const content = Draft.toContent(draft);
  assert.equal(content.title, '標題');
  assert.equal(content.background, '第一行\n第二行');
  assert.equal(content.opening, null);
});

test('numbers are converted only when they are plain digits; anything else goes to the server untouched', () => {
  const draft = Draft.blankDraft();
  draft.characters.push({ key: 1, name: 'a', age: ' 16 ', personality: 'p', appearance: 'x', occupation: '', speech_style: '', hidden_trait: '' });
  draft.characters.push({ key: 2, name: 'b', age: '十六', personality: 'p', appearance: 'x', occupation: '', speech_style: '', hidden_trait: '' });
  draft.characters.push({ key: 3, name: 'c', age: '', personality: 'p', appearance: 'x', occupation: '', speech_style: '', hidden_trait: '' });
  draft.characters.push({ key: 4, name: 'd', age: '-3', personality: 'p', appearance: 'x', occupation: '', speech_style: '', hidden_trait: '' });
  assert.deepEqual(Draft.toContent(draft).characters.map((c) => c.age), [16, '十六', null, '-3']);
});

test('the calendar is sent when either field is filled, so the server can name the missing one', () => {
  const onlyName = Draft.blankDraft();
  onlyName.calendar_name = '乾歷朝';
  assert.deepEqual(Draft.toContent(onlyName).calendar, { name: '乾歷朝', start_year: null });
  const onlyYear = Draft.blankDraft();
  onlyYear.calendar_start_year = '1200';
  assert.deepEqual(Draft.toContent(onlyYear).calendar, { name: '', start_year: 1200 });
});

test('choosing no ending drops the ending text and age even if they were typed before', () => {
  const draft = Draft.toDraft(SERVER_CONTENT);
  draft.ending_mode = 'none';
  const content = Draft.toContent(draft);
  assert.deepEqual([content.ending_mode, content.ending_text, content.ending_age], ['none', null, null]);
});

test('tags are split on commas, ideographic commas and newlines and empty ones dropped', () => {
  assert.deepEqual(Draft.splitTags('仙俠、成長, 科幻，\n  ,  '), ['仙俠', '成長', '科幻']);
  assert.deepEqual(Draft.splitTags(''), []);
  assert.deepEqual(Draft.splitTags(undefined), []);
});

test('a milestone without an id gets none (the server generates one) and an existing id is kept', () => {
  const draft = Draft.blankDraft();
  draft.milestones.push({ key: 1, id: '', title: '新事件', note: '', kind: 'year', value: '1205' });
  draft.milestones.push({ key: 2, id: 'm9', title: '舊事件', note: '備註', kind: 'age', value: '30' });
  const [fresh, old] = Draft.toContent(draft).milestones;
  assert.equal('id' in fresh, false);
  assert.deepEqual(fresh.trigger, { kind: 'year', value: 1205 });
  assert.deepEqual([old.id, old.note], ['m9', '備註']);
});

test('list rows get stable distinct keys', () => {
  const draft = Draft.toDraft({ characters: [{}, {}], locations: ['a', 'b'], milestones: [{}] });
  const keys = [...draft.characters, ...draft.locations, ...draft.milestones].map((row) => row.key);
  assert.equal(new Set(keys).size, keys.length);
});

test('malformed server content never throws and produces an empty editable draft', () => {
  for (const bad of [null, undefined, 42, 'x', [], { characters: 'oops', locations: 7, milestones: null, tags: 'x' }]) {
    const draft = Draft.toDraft(bad);
    assert.deepEqual([draft.characters.length, draft.locations.length, draft.milestones.length], [0, 0, 0]);
    assert.equal(draft.content_tier, 'safe');
  }
});

test('an unknown tier falls back to the safest mode', () => {
  assert.equal(Draft.toDraft({ content_tier: 'wild' }).content_tier, 'safe');
});

test('the dirty check compares what would be sent, not the form object', () => {
  const draft = Draft.toDraft(SERVER_CONTENT);
  const before = Draft.snapshot(draft);
  draft.title = '青雲問道  ';
  assert.equal(Draft.snapshot(draft), before);              // 只多了空白：送出的內容相同
  draft.title = '另一個標題';
  assert.notEqual(Draft.snapshot(draft), before);
});

test('characters are counted as Unicode characters, so an emoji counts once', () => {
  assert.equal(Draft.charCount('雲遊者'), 3);
  assert.equal(Draft.charCount('😀😀'), 2);
  assert.equal(Draft.charCount(undefined), 0);
});

test('the total counts every text field once and ignores numbers', () => {
  const draft = Draft.blankDraft();
  draft.title = '標題';
  draft.background = '背景背景';
  draft.tagsText = '甲、乙乙';
  draft.locations.push({ key: 1, text: '地點' });
  draft.characters.push({ key: 2, name: '角', age: '16', personality: '性', appearance: '貌貌', occupation: '', speech_style: '', hidden_trait: '' });
  draft.milestones.push({ key: 3, id: '', title: '事件', note: '', kind: 'age', value: '20' });
  assert.equal(Draft.totalChars(draft), 2 + 4 + 3 + 2 + (1 + 1 + 2) + 2);
});

test('the declared tier choices stop at the authors cap', () => {
  assert.deepEqual(Draft.allowedTiers('safe'), ['safe']);
  assert.deepEqual(Draft.allowedTiers('standard'), ['safe', 'standard']);
  assert.deepEqual(Draft.allowedTiers('adult'), ['safe', 'standard', 'adult']);
  assert.deepEqual(Draft.allowedTiers('nonsense'), ['safe']);
  assert.deepEqual(Draft.allowedTiers(undefined), ['safe']);
});

test('server error paths get Chinese field names', () => {
  assert.equal(Draft.labelFor('title'), '標題');
  assert.equal(Draft.labelFor('characters[0].name'), '角色 1 的姓名');
  assert.equal(Draft.labelFor('characters[2].hidden_trait'), '角色 3 的隱藏個性');
  assert.equal(Draft.labelFor('milestones[1].trigger.value'), '里程碑 2 的觸發數值');
  assert.equal(Draft.labelFor('locations[4]'), '地點 5');
  assert.equal(Draft.labelFor('calendar.start_year'), '曆法起始年');
  assert.equal(Draft.labelFor('total'), '全部文字');
  assert.equal(Draft.labelFor('something_unknown'), '欄位');
});

test('an error list pairs each label with the server message and keeps the order', () => {
  const list = Draft.errorList({ title: '這個欄位為必填。', 'characters[0].age': '必須是整數。' });
  assert.deepEqual(list.map((item) => item.label), ['標題', '角色 1 的年齡']);
  assert.equal(list[1].message, '必須是整數。');
  assert.deepEqual(Draft.errorList(undefined), []);
});

test('a rejection names only the fixed categories and never anything from the content', () => {
  const message = Draft.rejectionMessage(['minor_sexual', 'non_consensual']);
  assert.ok(message.includes('涉及未成年角色的性內容') && message.includes('非自願的性情節'));
  assert.ok(message.includes('無法儲存或送審'));
  assert.ok(!Draft.rejectionMessage(['unknown_category']).includes('unknown_category'));
  assert.ok(Draft.rejectionMessage([]).includes('違反平台的內容規範'));
});

test('status and tier labels cover every value the server can send', () => {
  for (const status of ['draft', 'pending_review', 'published', 'rejected', 'superseded']) { assert.ok(Draft.STATUS_LABELS[status], status); }
  for (const tier of ['safe', 'standard', 'adult']) { assert.ok(Draft.TIER_LABELS[tier], tier); }
});
