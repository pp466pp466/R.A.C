/* 開存檔精靈的純函式（與框架無關，Node 可以直接測試；見 rpg-creation-wizard 規格與 rpg-entry-ui 的 design D5、D6）。
 *
 * 精靈的狀態只放在頁面記憶體：這個模組只做「資料搬運與即時回饋」，**不寫任何瀏覽器儲存、不發請求、不輸出日誌**。
 * 合規與審查完全由伺服器判斷：前端只檢查「必要欄位已填、沒有超過長度」這類即時回饋，不重複實作規則，也不洩漏規則。
 * - 草稿（draft）是一組字串欄位（角色卡的七個欄位加 `origin` 來歷與選填的 `intro` 自我介紹）；聯繫（anchor）有三態：
 *   未設定、已設定（`kind`、`ref`、`text`）、略過（`skipped`）。`anchorState()` 另分出「選了一半」（選了對象沒寫、或寫了沒選對象）。
 * - `applySuggestion()` 回傳新的 `{ draft, anchor }`，**不修改傳入的物件**：AI 預覽覆蓋角色卡與聯繫，保留玩家選的來歷與自我介紹。
 * - `buildStartPayload()` 只搬運與去頭尾空白：看不懂的數字原樣送出，讓伺服器指出是哪個欄位；略過聯繫時帶 `skip_anchor: true` 而不帶 `anchor`。
 * - `describe*Failure(info)`：把 `Auth.describeError` 整理過的失敗（再加 `categories`）對照成畫面要顯示的固定繁體中文文字。
 *   **訊息只來自固定文字與列舉對照，不含玩家文字**：審查類別只取列舉識別字的對照；只有 400 的逐欄位說明是伺服器的規則說明
 *   （伺服器保證不回顯輸入），原樣放在 `fields`。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 * 載入順序：game-labels.js 在前（審查類別的對照表取自它）。
 */
(function (root, factory) {
  'use strict';
  var labels = (typeof module === 'object' && module.exports) ? require('./game-labels.js') : root.Game;
  var api = factory(labels);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    Object.keys(api).forEach(function (key) { root.Game[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function (labels) {
  'use strict';

  // ---- 步驟與欄位 --------------------------------------------------------------------------------------

  var STEPS = [
    { id: 'prologue', label: '序章' },
    { id: 'contact', label: '聯繫' },
    { id: 'create', label: '創角' }
  ];

  /* 角色卡欄位（順序即畫面順序）。`counted`：顯示字數與剩餘；年齡是數字，上限是數值（`limits.age_max`）不是字數。
     頭像與性別不在這裡：規格沒有這兩個欄位。 */
  var CARD_FIELDS = [
    { key: 'name', label: '姓名', required: true, multiline: false, counted: true, hint: '' },
    { key: 'personality', label: '個性', required: true, multiline: false, counted: true, hint: '' },
    { key: 'age', label: '年齡', required: true, multiline: false, counted: false, hint: '這是主角在劇中的年齡，與您本人的年齡無關。' },
    { key: 'appearance', label: '外貌', required: true, multiline: true, counted: true, hint: '' },
    { key: 'occupation', label: '職業', required: false, multiline: false, counted: true, hint: '' },
    { key: 'speech_style', label: '說話風格', required: false, multiline: true, counted: true, hint: '' },
    { key: 'hidden_trait', label: '隱藏個性', required: false, multiline: false, counted: true, hint: '劇情裡不會一開始就公開的一面。' }
  ];
  var CARD_KEYS = CARD_FIELDS.map(function (field) { return field.key; });
  var COUNTED_KEYS = CARD_FIELDS.filter(function (field) { return field.counted; }).map(function (field) { return field.key; }).concat(['intro']);

  var ORIGINS = ['native', 'transmigrator'];
  var ANCHOR_KINDS = ['character', 'location', 'milestone'];
  var TARGET_KIND_LABELS = { character: '人物', location: '地點', milestone: '里程碑' };
  var DEMO_NAME_FALLBACK = '你的主角';

  function has(map, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key); }
  function text(value) { return typeof value === 'string' ? value : ''; }
  function numberText(value) { return typeof value === 'number' && isFinite(value) ? String(value) : text(value); }
  function orNull(value) { var trimmed = text(value).trim(); return trimmed ? trimmed : null; }
  function list(value) { return Array.isArray(value) ? value : []; }
  function validMax(value) { return typeof value === 'number' && isFinite(value) && value > 0 ? value : null; }

  /* 空欄位 -> null；整數文字 -> 數字；其餘原樣送出（伺服器會指出是哪個欄位不合法）。 */
  function intOrRaw(value) {
    var raw = (typeof value === 'number' ? String(value) : text(value)).trim();
    if (raw === '') { return null; }
    return /^[0-9]+$/.test(raw) ? Number(raw) : raw;
  }

  function originOf(value) { return ORIGINS.indexOf(value) >= 0 ? value : 'native'; }

  function stepIndex(id) {
    for (var i = 0; i < STEPS.length; i += 1) { if (STEPS[i].id === id) { return i; } }
    return -1;
  }

  // ---- 草稿與聯繫 --------------------------------------------------------------------------------------

  function blankDraft() {
    var draft = { origin: 'native', intro: '' };
    CARD_KEYS.forEach(function (key) { draft[key] = ''; });
    return draft;
  }

  function copyDraft(draft) {
    var source = draft && typeof draft === 'object' ? draft : {};
    var copy = { origin: originOf(source.origin), intro: text(source.intro) };
    CARD_KEYS.forEach(function (key) { copy[key] = numberText(source[key]); });
    return copy;
  }

  function blankAnchor() { return { skipped: false, kind: '', ref: '', text: '' }; }
  function skippedAnchor() { return { skipped: true, kind: '', ref: '', text: '' }; }

  function copyAnchor(anchor) {
    var source = anchor && typeof anchor === 'object' ? anchor : {};
    return { skipped: source.skipped === true, kind: text(source.kind), ref: text(source.ref), text: text(source.text) };
  }

  /* 'skipped'（略過）、'complete'（選了對象也寫了一句）、'target_missing'（寫了沒選對象）、'text_missing'（選了對象沒寫）、'unset'（什麼都沒有）。 */
  function anchorState(anchor) {
    var a = copyAnchor(anchor);
    if (a.skipped) { return 'skipped'; }
    var hasTarget = a.kind !== '' && a.ref.trim() !== '';
    var hasText = a.text.trim() !== '';
    if (hasTarget && hasText) { return 'complete'; }
    if (hasTarget) { return 'text_missing'; }
    if (hasText || a.kind !== '' || a.ref.trim() !== '') { return 'target_missing'; }
    return 'unset';
  }

  /* 生成前要不要先確認覆蓋：角色卡有任何欄位有內容，或聯繫已經動過（含略過）。來歷與自我介紹不會被覆蓋，不算。 */
  function hasManualContent(draft, anchor) {
    var d = copyDraft(draft);
    var filled = CARD_KEYS.some(function (key) { return d[key].trim() !== ''; });
    return filled || anchorState(anchor) !== 'unset';
  }

  // ---- 字數與前進條件 ----------------------------------------------------------------------------------

  /* 字數以 Unicode 字元計（與伺服器相同的單位；NFC 正規化、去頭尾空白）。只用來顯示，最終以伺服器為準。 */
  function charCount(value) {
    var raw = text(value).trim();
    if (typeof raw.normalize === 'function') { raw = raw.normalize('NFC'); }
    return Array.from(raw).length;
  }

  function countOf(value, max) {
    var count = charCount(value);
    var limit = validMax(max);
    return { count: count, max: limit, remaining: limit === null ? null : limit - count, over: limit !== null && count > limit };
  }

  /* 各欄位的字數：{ name: { count, max, remaining, over }, … }，含選填的自我介紹；`limits` 沒有該欄位的上限時 max 為 null、不會超過。 */
  function fieldCounts(draft, limits) {
    var d = copyDraft(draft);
    var caps = limits && typeof limits === 'object' ? limits : {};
    var counts = {};
    COUNTED_KEYS.forEach(function (key) { counts[key] = countOf(d[key], caps[key]); });
    return counts;
  }

  /* 聯繫那一句的字數（上限是 `limits.anchor_text`）。 */
  function anchorCount(anchor, limits) {
    var caps = limits && typeof limits === 'object' ? limits : {};
    return countOf(copyAnchor(anchor).text, caps.anchor_text);
  }

  /* 超過上限的欄位鍵（角色卡欄位的順序，最後是自我介紹）。 */
  function overLimit(draft, limits) {
    var counts = fieldCounts(draft, limits);
    return COUNTED_KEYS.filter(function (key) { return counts[key].over; });
  }

  function fieldLabel(key) {
    for (var i = 0; i < CARD_FIELDS.length; i += 1) { if (CARD_FIELDS[i].key === key) { return CARD_FIELDS[i].label; } }
    return key === 'intro' ? '自我介紹' : '欄位';
  }

  function anchorBlockers(anchor, limits) {
    var state = anchorState(anchor);
    var found = [];
    if (state === 'unset') {
      found.push({ code: 'anchor_unset', field: 'anchor', step: 'contact', message: '「聯繫」還沒有完成：請選一個對象並寫一句關係，或選擇略過聯繫。' });
    } else if (state === 'target_missing') {
      found.push({ code: 'anchor_target_missing', field: 'anchor', step: 'contact', message: '「聯繫」請選擇一個對象，或改為略過聯繫。' });
    } else if (state === 'text_missing') {
      found.push({ code: 'anchor_text_missing', field: 'anchor_text', step: 'contact', message: '「聯繫」請寫一句與對象的關係，或改為略過聯繫。' });
    }
    var count = anchorCount(anchor, limits);
    if (state !== 'skipped' && count.over) {
      found.push({ code: 'anchor_over', field: 'anchor_text', step: 'contact', message: '「聯繫」的那一句話超過 ' + count.max + ' 字的上限。' });
    }
    return found;
  }

  function cardBlockers(draft, limits) {
    var d = copyDraft(draft);
    var counts = fieldCounts(d, limits);
    var found = [];
    CARD_FIELDS.forEach(function (field) {
      if (field.required && d[field.key].trim() === '') {
        found.push({ code: 'missing', field: field.key, step: 'create', message: '請填寫「' + field.label + '」。' });
      } else if (field.counted && counts[field.key].over) {
        found.push({ code: 'over', field: field.key, step: 'create', message: '「' + field.label + '」超過 ' + counts[field.key].max + ' 字的上限。' });
      }
    });
    return found;
  }

  /* 目前這一步「按下一步（創角步驟是開始冒險）」被什麼擋住：[{ code, field, step, message }]，空陣列代表可以前進。
     序章沒有條件；聯繫看聯繫的完成度；創角看角色卡的必要欄位與長度，再加上聯繫（開存檔必須帶聯繫或明確略過）。
     自我介紹不在這裡：它不會隨開存檔送出，只影響「請 AI 生成」。 */
  function blockers(step, draft, anchor, limits) {
    if (step === 'contact') { return anchorBlockers(anchor, limits); }
    if (step === 'create') { return cardBlockers(draft, limits).concat(anchorBlockers(anchor, limits)); }
    return [];
  }

  function canAdvance(step, draft, anchor, limits) { return blockers(step, draft, anchor, limits).length === 0; }

  // ---- 示範氣泡與聯繫對象 ------------------------------------------------------------------------------

  /* 固定句型加玩家輸入的姓名與說話風格；在前端渲染，不呼叫模型、不送請求。 */
  function demoBubbles(draft) {
    var d = copyDraft(draft);
    var name = d.name.trim() || DEMO_NAME_FALLBACK;
    var style = d.speech_style.trim();
    return [
      { kind: 'narration', speaker: '', text: name + '站在路口，四周的人聲漸漸安靜下來。', note: '' },
      { kind: 'speech', speaker: name, text: '「這裡……就是故事開始的地方嗎？」', note: style ? '說話風格：' + style : '' }
    ];
  }

  /* 聯繫步驟的選項：人物只取姓名與職業（即使回應多帶了年齡或隱藏特質也不會出現在選項裡）、地點的名稱、里程碑的編號與標題。 */
  function targetGroups(setup) {
    var s = setup && typeof setup === 'object' ? setup : {};
    var characters = list(s.characters).map(function (item) {
      var name = item && typeof item === 'object' ? text(item.name) : '';
      return name ? { kind: 'character', ref: name, label: name, detail: text(item.occupation) } : null;
    });
    var locations = list(s.locations).map(function (place) {
      var name = text(place);
      return name ? { kind: 'location', ref: name, label: name, detail: '' } : null;
    });
    var milestones = list(s.milestones).map(function (item) {
      var id = item && typeof item === 'object' ? text(item.id) : '';
      return id ? { kind: 'milestone', ref: id, label: text(item.title) || id, detail: '' } : null;
    });
    return [['character', characters], ['location', locations], ['milestone', milestones]].map(function (pair) {
      return { kind: pair[0], label: TARGET_KIND_LABELS[pair[0]], options: pair[1].filter(Boolean) };
    }).filter(function (group) { return group.options.length > 0; });
  }

  // ---- AI 預覽與送出的內容 -----------------------------------------------------------------------------

  /* 套用 AI 預覽：覆蓋角色卡與聯繫，保留玩家選的來歷與自我介紹。預覽缺角色卡或聯繫不完整時，不用空白蓋掉玩家的內容。
     回傳新的 { draft, anchor }，傳入的物件不被修改。 */
  function applySuggestion(draft, anchor, suggestion) {
    var nextDraft = copyDraft(draft);
    var nextAnchor = copyAnchor(anchor);
    var proposal = suggestion && typeof suggestion === 'object' ? suggestion : {};
    var card = proposal.protagonist && typeof proposal.protagonist === 'object' ? proposal.protagonist : null;
    if (card) {
      CARD_KEYS.forEach(function (key) { nextDraft[key] = numberText(card[key]); });
    }
    var link = proposal.anchor && typeof proposal.anchor === 'object' ? proposal.anchor : null;
    if (link && ANCHOR_KINDS.indexOf(link.kind) >= 0 && typeof link.ref === 'string' && typeof link.text === 'string') {
      nextAnchor = { skipped: false, kind: link.kind, ref: link.ref, text: link.text };
    }
    return { draft: nextDraft, anchor: nextAnchor };
  }

  /* `POST /api/sessions` 的本文：角色卡（含來歷）加上聯繫或 `skip_anchor`（二選一）。選填欄位空白時送 null；沒有頭像與性別。 */
  function buildStartPayload(versionId, draft, anchor) {
    var d = copyDraft(draft);
    var payload = {
      version_id: versionId,
      protagonist: {
        name: d.name.trim(), personality: d.personality.trim(), age: intOrRaw(d.age), appearance: d.appearance.trim(),
        occupation: orNull(d.occupation), speech_style: orNull(d.speech_style), hidden_trait: orNull(d.hidden_trait), origin: d.origin
      }
    };
    var link = copyAnchor(anchor);
    var state = anchorState(link);
    if (state === 'skipped') {
      payload.skip_anchor = true;
    } else if (state === 'unset') {
      payload.anchor = null;
    } else {
      payload.anchor = { kind: link.kind, ref: link.ref, text: link.text.trim() };
    }
    return payload;
  }

  /* `POST /api/sessions/protagonist/suggest` 的本文：自我介紹是選填的，空白時送 null。 */
  function buildSuggestPayload(versionId, draft) {
    return { version_id: versionId, intro: orNull(copyDraft(draft).intro) };
  }

  // ---- 伺服器的欄位錯誤 -> 步驟與表單欄位 --------------------------------------------------------------

  /* 欄位路徑屬於哪一步：`protagonist.*`、`intro` 在創角，`anchor.*` 在聯繫；其餘（例如 `version_id`）沒有對應的步驟。 */
  function stepOfField(path) {
    var p = typeof path === 'string' ? path : '';
    if (p === 'anchor' || p.indexOf('anchor.') === 0) { return 'contact'; }
    if (p === 'protagonist' || p.indexOf('protagonist.') === 0 || p === 'intro') { return 'create'; }
    return null;
  }

  /* 欄位路徑對應的表單欄位鍵：角色卡欄位用自己的鍵，`origin`、`intro` 同名，聯繫是 `anchor_kind`／`anchor_ref`／`anchor_text`，
     整張角色卡或不認得的子欄位歸到 `protagonist`／`anchor`；與表單無關的路徑回傳 null。 */
  function fieldKeyOf(path) {
    var p = typeof path === 'string' ? path : '';
    if (p === 'intro') { return 'intro'; }
    if (p === 'protagonist') { return 'protagonist'; }
    if (p === 'anchor') { return 'anchor'; }
    var match = /^protagonist\.(\w+)$/.exec(p);
    if (match) { return CARD_KEYS.indexOf(match[1]) >= 0 || match[1] === 'origin' ? match[1] : 'protagonist'; }
    match = /^anchor\.(\w+)$/.exec(p);
    if (match) { return ['kind', 'ref', 'text'].indexOf(match[1]) >= 0 ? 'anchor_' + match[1] : 'anchor'; }
    return null;
  }

  // ---- 失敗 -> 畫面訊息 --------------------------------------------------------------------------------

  var HOME_LINK = { href: '/game', text: '回到冒險' };
  var ACCOUNT_LINK = { href: '/account', text: '前往帳號設定' };
  var APPEAL_LINK = { href: '/appeal', text: '前往申訴頁' };

  function linkCopy(link) { return link ? { href: link.href, text: link.text } : null; }

  function codeOf(info) { return typeof info.code === 'string' && /^[a-z_]{1,64}$/.test(info.code) ? info.code : ''; }

  function failure(info, kind, message, extra) {
    var result = { kind: kind, code: codeOf(info), message: message, step: null, fields: {}, link: null, retry: false };
    if (extra) { Object.keys(extra).forEach(function (key) { result[key] = extra[key]; }); }
    return result;
  }

  var GATE_FAILURES = {
    content_tier_restricted: { message: '這個劇本的內容模式高於您目前的模式，所以無法開始。', link: ACCOUNT_LINK },
    age_confirmation_required: { message: '請先到帳號設定完成年齡確認，才能開始冒險。', link: ACCOUNT_LINK },
    age_restricted: { message: '您的年齡目前不符合使用這項功能的規定，所以無法開始冒險。', link: null },
    account_locked: { message: '您的帳號目前被鎖定，只能到申訴頁留言給管理員。', link: APPEAL_LINK },
    account_deletion_pending: { message: '您的帳號正在等待刪除，目前無法開始冒險。', link: ACCOUNT_LINK }
  };
  var GATE_FALLBACK = { message: '您的帳號目前無法開始冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_LINK };

  /* 審查類別的說明：只取列舉識別字的對照，不認得的識別字略過（不顯示原始值）。沒有可顯示的類別時回傳空字串。 */
  function categoryNames(categories) {
    var names = [];
    list(categories).forEach(function (category) {
      if (typeof category === 'string' && has(labels.CATEGORY_LABELS, category)) {
        var name = labels.CATEGORY_LABELS[category];
        if (names.indexOf(name) < 0) { names.push(name); }
      }
    });
    return names.join('、');
  }

  function fieldFailure(info) {
    var fields = {};
    var step = null;
    var raw = info.fields && typeof info.fields === 'object' ? info.fields : {};
    Object.keys(raw).forEach(function (path) {
      var key = fieldKeyOf(path);
      if (key === null) { return; }
      fields[key] = String(raw[path]);
      var at = stepOfField(path);
      if (at !== null && (step === null || stepIndex(at) < stepIndex(step))) { step = at; }       // 有多一步的錯誤時，先回到較前面的那一步
    });
    var mapped = Object.keys(fields).length > 0;
    return failure(info, 'fields', mapped ? '請修正標示的欄位後再試。' : '送出的資料有問題，請檢查後再試。', { step: step, fields: fields });
  }

  /* 三種呼叫共用的失敗：中止（不顯示任何文字）、逾時、斷線、頁面憑證失效、登入過期、帳號閘門、找不到。沒有處理時回傳 null。 */
  function commonFailure(info, messages) {
    if (info.kind === 'canceled') { return failure(info, 'canceled', ''); }
    if (info.kind === 'timeout') { return failure(info, 'timeout', messages.timeout, { retry: true }); }
    if (info.kind === 'network') { return failure(info, 'network', messages.network, { retry: true }); }
    if (info.kind === 'csrf') { return failure(info, 'csrf', '頁面的連線憑證已失效，請重新整理頁面後再試（重新整理會清空已填寫的內容）。'); }
    if (info.status === 401) { return failure(info, 'login', '登入已過期，請重新登入。'); }
    if (info.status === 403) {
      var gate = has(GATE_FAILURES, codeOf(info)) ? GATE_FAILURES[codeOf(info)] : GATE_FALLBACK;
      return failure(info, 'gate', gate.message, { link: linkCopy(gate.link) });
    }
    if (info.status === 404) { return failure(info, 'not_found', '找不到這個劇本，它可能已下架或不存在。', { link: linkCopy(HOME_LINK) }); }
    return null;
  }

  function normalizeInfo(info) { return info && typeof info === 'object' ? info : {}; }

  /* 載入創角資料（`GET /api/sessions/setup/<id>`）失敗。 */
  function describeSetupFailure(info) {
    info = normalizeInfo(info);
    var common = commonFailure(info, { timeout: '載入的時間比預期久，請稍後重試。', network: '連線不穩，無法載入這個劇本的資料，請稍後重試。' });
    if (common) { return common; }
    return failure(info, 'unavailable', '暫時無法載入這個劇本的資料，請稍後重試。', { retry: true });
  }

  /* 開始冒險（`POST /api/sessions`）失敗。所有失敗都保留玩家已填的內容。 */
  function describeStartFailure(info) {
    info = normalizeInfo(info);
    var common = commonFailure(info, {
      timeout: '處理的時間比預期久。伺服器可能已經完成，重新送出之前，可以先到「我的存檔」確認有沒有建立；您填寫的內容還在這個頁面。',
      network: '連線中斷，無法確認有沒有建立成功。重新送出之前，可以先到「我的存檔」確認；您填寫的內容還在這個頁面。'
    });
    if (common) { return common; }
    if (info.status === 400) { return fieldFailure(info); }
    if (info.status === 422) {
      var names = categoryNames(info.categories);
      var scoped = names ? '（' + names + '）' : '';
      if (codeOf(info) === 'script_rejected') {
        return failure(info, 'rejected', '這個劇本目前無法用來開始冒險，因為它沒有通過內容審查' + scoped + '。');
      }
      return failure(info, 'rejected', '主角的內容未通過內容規範' + scoped + '，請修改後再試。');
    }
    if (info.status === 429) { return failure(info, 'limit', '操作太頻繁，請稍後再試。'); }
    if (info.status === 503) {
      if (codeOf(info) === 'review_unavailable') { return failure(info, 'unavailable', '內容審查暫時無法使用，請稍後再試。'); }
      return failure(info, 'unavailable', '服務暫時無法使用，請稍後再試。');
    }
    return failure(info, 'unknown', '開始冒險時發生問題，請稍後再試；您填寫的內容還在這個頁面。');
  }

  /* 請 AI 生成（`POST /api/sessions/protagonist/suggest`）失敗。預覽只是填表，失敗時玩家已填的內容完全不變，可以改為自創。 */
  function describeSuggestFailure(info) {
    info = normalizeInfo(info);
    var common = commonFailure(info, {
      timeout: '生成的時間比預期久，您填寫的內容沒有變動；可以再試一次，或直接自己創角。',
      network: '連線不穩，無法生成。您填寫的內容沒有變動；可以再試一次，或直接自己創角。'
    });
    if (common) { return common; }
    if (info.status === 400) { return fieldFailure(info); }
    if (info.status === 422) {
      var names = categoryNames(info.categories);
      return failure(info, 'rejected', '您的自我介紹未通過內容規範' + (names ? '（' + names + '）' : '') + '，請修改後再試，或直接自己創角。');
    }
    if (info.status === 429) {
      if (codeOf(info) === 'suggestion_limit') { return failure(info, 'limit', '今天的 AI 生成次數已經用完，可以改為自己創角，明天再試。'); }
      return failure(info, 'limit', '操作太頻繁，請稍後再試。');
    }
    if (info.status === 503) {
      if (codeOf(info) === 'suggestion_unavailable') { return failure(info, 'unavailable', '目前暫時無法生成，您填寫的內容沒有變動，可以直接自己創角。'); }
      return failure(info, 'unavailable', '服務暫時無法使用，您填寫的內容沒有變動，可以稍後再試或直接自己創角。');
    }
    return failure(info, 'unknown', '生成時發生問題，您填寫的內容沒有變動，可以再試一次或直接自己創角。');
  }

  return {
    STEPS: STEPS,
    CARD_FIELDS: CARD_FIELDS,
    TARGET_KIND_LABELS: TARGET_KIND_LABELS,
    stepIndex: stepIndex,
    blankDraft: blankDraft,
    blankAnchor: blankAnchor,
    skippedAnchor: skippedAnchor,
    anchorState: anchorState,
    hasManualContent: hasManualContent,
    charCount: charCount,
    fieldCounts: fieldCounts,
    anchorCount: anchorCount,
    overLimit: overLimit,
    fieldLabel: fieldLabel,
    blockers: blockers,
    canAdvance: canAdvance,
    demoBubbles: demoBubbles,
    targetGroups: targetGroups,
    applySuggestion: applySuggestion,
    buildStartPayload: buildStartPayload,
    buildSuggestPayload: buildSuggestPayload,
    stepOfField: stepOfField,
    fieldKeyOf: fieldKeyOf,
    describeSetupFailure: describeSetupFailure,
    describeStartFailure: describeStartFailure,
    describeSuggestFailure: describeSuggestFailure
  };
});
