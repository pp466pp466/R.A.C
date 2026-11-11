/* 劇本編輯器的草稿模型（純函式，與框架無關，Node 可以直接測試；見 script-authoring 規格）。
 *
 * - `toDraft(content)`：把伺服器的標準化內容轉成編輯器用的表單資料（欄位全是字串，清單的每一列有穩定的 `key`）。
 * - `toContent(draft)`：把表單資料轉回要送出的內容。**只做搬運與去頭尾空白**：長度、格式、數量與列舉值全由伺服器驗證，
 *   前端不重複實作規則（看不懂的數字原樣送出，讓伺服器指出是哪個欄位）。
 * - 字數與數量的上限來自 `GET /api/scripts/limits` 的 `spec`（單一來源是驗證器的常數），這裡不寫死任何數字。
 * - `labelFor(path)`、`errorList(fields)`：把伺服器回的欄位路徑（`characters[0].name`）轉成繁體中文的欄位名稱。
 * - `categoryText(categories)`：審查拒絕時的類別說明；只有固定列舉，不含也不需要內容的位置。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Scripts = root.Scripts || {};
    Object.keys(api).forEach(function (key) { root.Scripts[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TIER_ORDER = ['safe', 'standard', 'adult'];
  var TIER_LABELS = { safe: '安全模式', standard: '一般模式', adult: '成人模式' };
  var STATUS_LABELS = {
    draft: '草稿', pending_review: '待審', published: '已上架', rejected: '已被退回', superseded: '已被取代'
  };
  var CATEGORY_LABELS = {
    minor_sexual: '涉及未成年角色的性內容', sexual_violence: '性暴力', non_consensual: '非自願的性情節', real_person_sexual: '以真人名義的性內容'
  };
  var FIELD_LABELS = {
    content: '劇本內容', title: '標題', summary: '簡介', world_name: '世界觀名稱', content_tier: '內容模式', background: '背景與體系',
    opening: '初始對話', rules: '世界規則（狀態轉移規則）', tags: '標籤', calendar: '曆法', 'calendar.name': '曆法名稱', 'calendar.start_year': '曆法起始年',
    ending_mode: '結局模式', ending_text: '結局說明', ending_age: '壽終年齡', audience: '讀者頻道', characters: '角色', locations: '地點', milestones: '里程碑',
    total: '全部文字', spec_version: '規格版本', cover_image: '封面圖片', base_hash: '編輯版本'
  };
  var CHARACTER_FIELDS = {
    name: '姓名', age: '年齡', personality: '個性', appearance: '外貌', occupation: '職業', speech_style: '說話風格', hidden_trait: '隱藏個性', image: '頭像'
  };
  var MILESTONE_FIELDS = { id: '編號', title: '事件名稱', note: '備註', trigger: '觸發', 'trigger.kind': '觸發種類', 'trigger.value': '觸發數值' };

  /* 讀者頻道：全讀者是預設與缺值（舊版本沒有這個鍵）；伺服器只在男頻、女頻時才保存該鍵。 */
  var AUDIENCES = ['all', 'male', 'female'];
  var AUDIENCE_LABELS = { all: '全讀者', male: '男頻', female: '女頻' };
  function audienceOf(value) { return AUDIENCES.indexOf(value) >= 0 ? value : 'all'; }

  var counter = 0;
  function nextKey() { counter += 1; return counter; }
  function text(value) { return typeof value === 'string' ? value : ''; }
  function numberText(value) { return typeof value === 'number' && isFinite(value) ? String(value) : (typeof value === 'string' ? value : ''); }
  function orNull(value) { var trimmed = text(value).trim(); return trimmed ? trimmed : null; }

  /* 空欄位 -> null；整數文字 -> 數字；其餘原樣送出（伺服器會指出是哪個欄位不合法）。 */
  function intOrRaw(value) {
    var raw = (typeof value === 'number' ? String(value) : text(value)).trim();
    if (raw === '') { return null; }
    return /^[0-9]+$/.test(raw) ? Number(raw) : raw;
  }

  function charCount(value) { return Array.from(text(value)).length; }

  function allowedTiers(cap) {
    var index = TIER_ORDER.indexOf(cap);
    return TIER_ORDER.slice(0, (index < 0 ? 0 : index) + 1);
  }

  function blankDraft() { return toDraft({}); }

  function toDraft(content) {
    var c = content && typeof content === 'object' ? content : {};
    var calendar = c.calendar && typeof c.calendar === 'object' ? c.calendar : {};
    return {
      title: text(c.title), summary: text(c.summary), world_name: text(c.world_name),
      content_tier: TIER_ORDER.indexOf(c.content_tier) >= 0 ? c.content_tier : 'safe',
      background: text(c.background), opening: text(c.opening), rules: text(c.rules),
      tagsText: Array.isArray(c.tags) ? c.tags.map(text).join('、') : '',
      calendar_name: text(calendar.name), calendar_start_year: numberText(calendar.start_year),
      ending_mode: c.ending_mode === 'custom' ? 'custom' : 'none', ending_text: text(c.ending_text), ending_age: numberText(c.ending_age),
      audience: audienceOf(c.audience),
      characters: (Array.isArray(c.characters) ? c.characters : []).map(function (ch) {
        ch = ch || {};
        return {
          key: nextKey(), name: text(ch.name), age: numberText(ch.age), personality: text(ch.personality), appearance: text(ch.appearance),
          occupation: text(ch.occupation), speech_style: text(ch.speech_style), hidden_trait: text(ch.hidden_trait)
        };
      }),
      locations: (Array.isArray(c.locations) ? c.locations : []).map(function (place) { return { key: nextKey(), text: text(place) }; }),
      milestones: (Array.isArray(c.milestones) ? c.milestones : []).map(function (m) {
        m = m || {};
        var trigger = m.trigger && typeof m.trigger === 'object' ? m.trigger : {};
        return {
          key: nextKey(), id: text(m.id), title: text(m.title), note: text(m.note),
          kind: trigger.kind === 'year' ? 'year' : 'age', value: numberText(trigger.value)
        };
      })
    };
  }

  function splitTags(value) {
    return text(value).split(/[,，、\n]+/).map(function (tag) { return tag.trim(); }).filter(Boolean);
  }

  function toContent(d) {
    var calendarName = text(d.calendar_name).trim();
    var calendarYear = text(d.calendar_start_year).trim();
    var custom = d.ending_mode === 'custom';
    return {
      title: text(d.title).trim(), summary: text(d.summary).trim(), world_name: text(d.world_name).trim(), content_tier: d.content_tier,
      background: text(d.background).trim(), opening: orNull(d.opening), rules: orNull(d.rules), tags: splitTags(d.tagsText),
      calendar: calendarName || calendarYear ? { name: calendarName, start_year: intOrRaw(calendarYear) } : null,
      ending_mode: custom ? 'custom' : 'none', ending_text: custom ? orNull(d.ending_text) : null, ending_age: custom ? intOrRaw(d.ending_age) : null,
      audience: audienceOf(d.audience),            // 永遠送出；全讀者時由伺服器決定不保存該鍵（雜湊相容，見 story-preference 設計 D1）
      characters: (d.characters || []).map(function (ch) {
        return {
          name: text(ch.name).trim(), age: intOrRaw(ch.age), personality: text(ch.personality).trim(), appearance: text(ch.appearance).trim(),
          occupation: orNull(ch.occupation), speech_style: orNull(ch.speech_style), hidden_trait: orNull(ch.hidden_trait)
        };
      }),
      locations: (d.locations || []).map(function (place) { return text(place.text).trim(); }).filter(Boolean),
      milestones: (d.milestones || []).map(function (m) {
        var item = { title: text(m.title).trim(), note: orNull(m.note), trigger: { kind: m.kind, value: intOrRaw(m.value) } };
        if (text(m.id).trim()) { item.id = text(m.id).trim(); }
        return item;
      })
    };
  }

  /* 全部文字的約略字數（與伺服器的「全部文字合計」同樣不計數字）；只用來顯示，不用來擋下請求。 */
  function totalChars(d) {
    var sum = 0;
    [d.title, d.summary, d.world_name, d.background, d.opening, d.rules, d.calendar_name].forEach(function (value) { sum += charCount(value); });
    if (d.ending_mode === 'custom') { sum += charCount(d.ending_text); }
    splitTags(d.tagsText).forEach(function (tag) { sum += charCount(tag); });
    (d.locations || []).forEach(function (place) { sum += charCount(text(place.text).trim()); });
    (d.characters || []).forEach(function (ch) {
      [ch.name, ch.personality, ch.appearance, ch.occupation, ch.speech_style, ch.hidden_trait].forEach(function (value) { sum += charCount(value); });
    });
    (d.milestones || []).forEach(function (m) { sum += charCount(m.title) + charCount(m.note); });
    return sum;
  }

  function snapshot(d) { return JSON.stringify(toContent(d)); }

  // ---- 伺服器的欄位錯誤 -> 繁體中文的欄位名稱 ----------------------------------------------------------

  function labelFor(path) {
    var key = String(path);
    if (Object.prototype.hasOwnProperty.call(FIELD_LABELS, key)) { return FIELD_LABELS[key]; }
    var match = /^characters\[(\d+)\](?:\.(\w+))?$/.exec(key);
    if (match) { return '角色 ' + (Number(match[1]) + 1) + (match[2] ? ' 的' + (CHARACTER_FIELDS[match[2]] || match[2]) : ''); }
    match = /^milestones\[(\d+)\](?:\.([\w.]+))?$/.exec(key);
    if (match) { return '里程碑 ' + (Number(match[1]) + 1) + (match[2] ? ' 的' + (MILESTONE_FIELDS[match[2]] || match[2]) : ''); }
    match = /^(locations|tags)\[(\d+)\]$/.exec(key);
    if (match) { return (match[1] === 'locations' ? '地點 ' : '標籤 ') + (Number(match[2]) + 1); }
    return '欄位';
  }

  function errorList(fields) {
    return Object.keys(fields || {}).map(function (path) { return { path: path, label: labelFor(path), message: String(fields[path]) }; });
  }

  function categoryText(categories) {
    var labels = (categories || []).map(function (category) { return CATEGORY_LABELS[category]; }).filter(Boolean);
    return labels.length ? labels.join('、') : '';
  }

  /* 審查拒絕的說明：只有固定列舉的類別，不指出內容的位置或文字。 */
  function rejectionMessage(categories) {
    var named = categoryText(categories);
    return '這份劇本的內容違反平台的內容規範' + (named ? '（' + named + '）' : '') + '，無法儲存或送審。請修改後再試。';
  }

  return {
    TIER_LABELS: TIER_LABELS, STATUS_LABELS: STATUS_LABELS, CATEGORY_LABELS: CATEGORY_LABELS, AUDIENCE_LABELS: AUDIENCE_LABELS,
    charCount: charCount, allowedTiers: allowedTiers, blankDraft: blankDraft, toDraft: toDraft, toContent: toContent,
    splitTags: splitTags, totalChars: totalChars, snapshot: snapshot, labelFor: labelFor, errorList: errorList,
    categoryText: categoryText, rejectionMessage: rejectionMessage
  };
});
