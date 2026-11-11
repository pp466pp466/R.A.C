/* 遊戲主畫面的純函式（與框架無關，沒有請求、儲存、DOM、計時器、主控台，Node 可以直接測試；見 rpg-game-screen、rpg-stream-client、
 * rpg-game-sync、rpg-wallet-ui、rpg-notebook-panel 規格與 rpg-game-ui 的 design D3、D5、D8、D9、D11）。
 *
 * 這個模組只做「資料搬運與對照」：
 * - **事件到畫面的轉換**（D3）：`newStreamTurn` 建立一輪的檢視模型；`applyStreamEvent(turn, event)` 處理串流的 meta／token／ping／transition／state／done／error，
 *   回傳 `{ turn, effects }`（不修改傳入的物件）。`effects` 是給掛載層的指令（`push` 推進打字機、`discard` 丟棄目前的氣泡、`transition` 插入一行轉場、
 *   `world`、`notices`、`coins`、`finish`、`hint`、`failure`、`done`），純資料，掛載層照做即可。`applyStreamEnd(turn, result)` 處理沒有 `done` 的結束
 *   （中斷、逾時、取消、開始前被拒）。**被擋與中斷的回合不保存任何部分文字**；`state` 事件只取白名單欄位。
 * - **時間軸**（D3、D5）：`normalizeTurn`、`mergeTurns(local, remote)`（以 `seq` 為準、後端覆寫、未完成的回合不含文字）、`timelineFrom(opening, turns, inflight)`。
 * - **失敗 -> 畫面訊息**：`describeHttpFailure(status, body)`（串流開始前的 JSON 錯誤，訊息都說明「沒有扣除遊戲幣」）、`describeRequestFailure(info)`
 *   （其他請求）、`describeStreamFailure(result)`、`describeNotebookFailure(info)`、`describeCheckinFailure(info, options)`。
 *   `info` 是 `Auth.describeError` 整理過的失敗再加 game-api 放進來的欄位：`categories`（審查類別）、`balance`／`needed`（餘額不足）、`checkin`（簽到預覽）、
 *   `notebook`（衝突時後端最新的記事簿）、`length`／`capacity`（記事簿太長）。
 *   **訊息只來自固定文字與列舉對照，不含玩家文字**：伺服器送來的 `message` 只在「不認得的 409」與 400 的逐欄位說明才顯示，而且必須通過 `trusted()`
 *   （含中文、不含角括號與控制字元、長度有上限）；審查類別只取列舉識別字的中文名稱。
 * - **小工具**：`notebookLength`（NFC 之後以 Unicode 字元計，與後端 `notebook_policy.length_of` 同一個算法，取樣案例由 pytest 比對）、
 *   `notebookView`、`worldView`／`mergeWorld`、`noticeText`、`sendBlocker`／`canSend`、`actionProblem`、`hasCompleteTurn`。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。載入順序：game-labels.js 在前（固定文字取自它）。
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

  // ---- 小工具 ----------------------------------------------------------------------------------------

  function has(map, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key); }
  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function text(value) { return typeof value === 'string' ? value : ''; }
  function list(value) { return Array.isArray(value) ? value : []; }
  function codeOf(value) { return typeof value === 'string' && /^[a-z_]{1,64}$/.test(value) ? value : ''; }
  function assign(target, source) {
    if (source) { Object.keys(source).forEach(function (key) { target[key] = source[key]; }); }
    return target;
  }
  function copy(value) { return assign({}, value); }

  /* 伺服器送來的文字只有「含中文、不含角括號與控制字元、不太長」才顯示（沿用 Auth.describeError 的信任範圍；伺服器的固定訊息不含玩家文字）。 */
  var TRUSTED_MAX = 120;
  function trusted(value) {
    return typeof value === 'string' && value.length > 0 && value.length <= TRUSTED_MAX && /[一-鿿]/.test(value) && !/[<>\u0000-\u001f\u007f]/.test(value);
  }

  /* Unicode 字元數（NFC 之後的碼點數；代理對算一個）：與後端 `len(unicodedata.normalize("NFC", text))` 相同。控制字元照算。 */
  function characterCount(value) {
    if (typeof value !== 'string' || value === '') { return 0; }
    var normalized = typeof value.normalize === 'function' ? value.normalize('NFC') : value;
    var count = 0;
    for (var index = 0; index < normalized.length; index += 1) {
      var unit = normalized.charCodeAt(index);
      if (unit >= 0xD800 && unit <= 0xDBFF && index + 1 < normalized.length) {
        var next = normalized.charCodeAt(index + 1);
        if (next >= 0xDC00 && next <= 0xDFFF) { index += 1; }
      }
      count += 1;
    }
    return count;
  }

  /* 記事簿的字數：與後端 `notebook_policy.length_of` 同一個算法。 */
  function notebookLength(value) { return characterCount(value); }

  // ---- 失敗物件 ---------------------------------------------------------------------------------------

  var HOME_LINK = { href: '/game', text: '回到冒險' };
  var ACCOUNT_LINK = { href: '/account', text: '前往帳號設定' };
  var APPEAL_LINK = { href: '/appeal', text: '前往申訴頁' };
  var NO_CHARGE = '這一輪沒有扣除遊戲幣。';

  function linkCopy(link) { return link ? { href: link.href, text: link.text } : null; }

  /* 所有失敗共用同一個形狀，掛載層不必判斷欄位存不存在。
     kind：canceled、timeout、network、csrf、login、gate、not_found、fields、coins、busy、opening_wait、opening_pending、ended、conflict、rejected、
           limit、unavailable、unknown、interrupted、stream_error、too_long、overflow、already、config。
     reload：畫面應重讀快照；catchUp：應以 since_seq 補齊；silent：不顯示成錯誤；keepDraft：保留玩家正在編輯的內容。 */
  function failure(kind, status, code, message, extra) {
    var result = {
      kind: kind, status: status, code: code, message: message, fields: {}, link: null, retry: false, reload: false, catchUp: false, silent: false,
      keepDraft: false, balance: null, needed: null, notebook: null, length: null, capacity: null, checkin: null, ended: false
    };
    return assign(result, extra);
  }

  /* 把兩種來源的失敗整理成同一個形狀：fetch 串流開始前的回應本文（`fromBody`），以及 axios 經 `Auth.describeError` 整理後的 info（`fromInfo`）。 */
  function fromBody(status, body) {
    var source = isObject(body) ? body : {};
    var error = isObject(source.error) ? source.error : {};
    return normalizeRequest({
      kind: isCount(status) && status > 0 ? 'server' : 'network', status: status, code: error.code, fields: error.fields, message: error.message,
      categories: source.categories, balance: source.balance, needed: source.needed, checkin: source.checkin, notebook: source.notebook,
      length: source.length, capacity: source.capacity
    });
  }

  function fromInfo(info) {
    var source = isObject(info) ? info : {};
    return normalizeRequest({
      kind: source.kind, status: source.status, code: source.code, fields: source.fields, message: source.message, categories: source.categories,
      balance: source.balance, needed: source.needed, checkin: source.checkin, notebook: source.notebook, length: source.length, capacity: source.capacity
    });
  }

  function normalizeRequest(raw) {
    var status = isCount(raw.status) ? raw.status : 0;
    var kind = typeof raw.kind === 'string' ? raw.kind : 'server';
    if (kind === 'server' && status === 0) { kind = 'network'; }
    var fields = {};
    if (isObject(raw.fields)) {
      Object.keys(raw.fields).forEach(function (name) {
        if (/^[a-z_]{1,40}$/.test(name) && trusted(raw.fields[name])) { fields[name] = raw.fields[name]; }
      });
    }
    return {
      kind: kind, status: status, code: codeOf(raw.code), message: text(raw.message), fields: fields, categories: list(raw.categories),
      balance: isCount(raw.balance) ? raw.balance : null, needed: isCount(raw.needed) ? raw.needed : null,
      checkin: isObject(raw.checkin) ? raw.checkin : null, notebook: raw.notebook, length: isCount(raw.length) ? raw.length : null,
      capacity: isCount(raw.capacity) ? raw.capacity : null
    };
  }

  /* 審查類別的說明：只取列舉識別字的對照，不認得的略過（不顯示原始值）。沒有可顯示的類別時回傳空字串。 */
  function categoryNames(categories) {
    var names = [];
    list(categories).forEach(function (category) {
      if (has(labels.CATEGORY_LABELS, category)) {
        var name = labels.CATEGORY_LABELS[category];
        if (names.indexOf(name) < 0) { names.push(name); }
      }
    });
    return names.join('、');
  }

  /* 連線層的失敗（取消、逾時、斷線、頁面憑證失效）：沒有處理時回傳 null。 */
  function transportFailure(request, messages) {
    if (request.kind === 'canceled') { return failure('canceled', 0, 'canceled', '', { silent: true }); }
    if (request.kind === 'timeout') { return failure('timeout', 0, 'timeout', messages.timeout, { retry: true }); }
    if (request.kind === 'network') { return failure('network', 0, 'network', messages.network, { retry: true }); }
    if (request.kind === 'csrf' || (request.status === 403 && request.code === 'csrf_failed')) {
      return failure('csrf', 403, 'csrf_failed', '頁面的連線憑證已失效，請重新整理頁面後再試。', {});
    }
    return null;
  }

  var GATE_FAILURES = {
    account_locked: { message: '您的帳號目前被鎖定，只能到申訴頁留言給管理員。', link: APPEAL_LINK },
    account_deletion_pending: { message: '您的帳號正在等待刪除，目前無法繼續冒險。', link: ACCOUNT_LINK },
    age_confirmation_required: { message: '請先到帳號設定完成年齡確認，才能繼續冒險。', link: ACCOUNT_LINK },
    age_restricted: { message: '您的年齡目前不符合使用這項功能的規定，所以無法繼續冒險。', link: null },
    content_mode_restricted: { message: '依年齡規定，您目前只能使用安全模式，這個存檔的內容模式無法使用。', link: ACCOUNT_LINK },
    content_tier_restricted: { message: '這個存檔的內容模式高於您目前的設定，無法繼續。', link: ACCOUNT_LINK }
  };
  var GATE_FALLBACK = { message: '您的帳號目前無法繼續冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_LINK };

  function gateFailure(request, suffix) {
    var gate = has(GATE_FAILURES, request.code) ? GATE_FAILURES[request.code] : GATE_FALLBACK;
    return failure('gate', 403, request.code, gate.message + suffix, { link: linkCopy(gate.link) });
  }

  /* 一般請求與串流開始前共用的對照。`suffix` 是附在訊息後的「沒有扣除遊戲幣」（只有回合串流開始前才加）。 */
  function requestFailure(request, suffix) {
    var transport = transportFailure(request, {
      timeout: '連線逾時，請稍後再試。', network: '無法連線到伺服器，請檢查網路連線後再試。'
    });
    if (transport) { return transport; }
    var status = request.status;
    var code = request.code;
    if (status === 400) {
      return failure('fields', 400, code, '送出的內容有問題，請檢查後再試。' + suffix, { fields: request.fields });
    }
    if (status === 401) { return failure('login', 401, code, '登入已過期，請重新登入。', {}); }
    if (status === 403) { return gateFailure(request, suffix); }
    if (status === 404) { return failure('not_found', 404, code, '找不到這個存檔，它可能已被刪除。', { link: linkCopy(HOME_LINK) }); }
    if (status === 409) { return conflictFailure(request, suffix); }
    if (status === 422) {
      var names = categoryNames(request.categories);
      return failure('rejected', 422, code, '這個內容未通過內容規範' + (names ? '（' + names + '）' : '') + '，無法送出。請換個說法再試。' + suffix, {});
    }
    if (status === 429) { return failure('limit', 429, code, '操作太頻繁，請稍後再試。' + suffix, { retry: true }); }
    if (status === 503) {
      var message = code === 'system_busy' ? '目前使用人數較多，請稍後再試。'
        : code === 'wallet_busy' ? '遊戲幣目前較忙碌，請稍後再試。'
        : code === 'service_unavailable' ? '故事引擎目前無法使用，請稍後再試。'
        : '服務暫時無法使用，請稍後再試。';
      return failure('unavailable', 503, code, message + suffix, { retry: true });
    }
    return failure('unknown', status, code, '發生了預期之外的問題，請稍後再試。' + suffix, { retry: true });
  }

  function conflictFailure(request, suffix) {
    var code = request.code;
    if (code === 'insufficient_coins') {
      return failure('coins', 409, code, '遊戲幣不足，' + NO_CHARGE, { balance: request.balance, needed: request.needed });
    }
    if (code === 'turn_in_progress') {
      return failure('busy', 409, code, '另一個回合還在進行中（可能來自另一個分頁或裝置）。稍後會為你重新同步。' + suffix, { catchUp: true });
    }
    if (code === 'opening_in_progress') {
      return failure('opening_wait', 409, code, '另一個裝置正在準備故事的開場，請稍候。', { retry: true });
    }
    if (code === 'opening_pending') {
      return failure('opening_pending', 409, code, '故事的開場還沒有準備好，請稍候再開始。' + suffix, { reload: true });
    }
    if (code === 'session_ended') {
      return failure('ended', 409, code, '這個存檔已經完結，無法再送出新的回合。', { ended: true });
    }
    if (code === 'message_id_reused') {
      return failure('conflict', 409, code, '這則行動的識別碼已用於不同的內容，請重新整理頁面後再送出。' + suffix, { reload: true });
    }
    var shown = trusted(request.message) ? request.message : '目前無法處理這個操作，已為你重新同步。';
    return failure('conflict', 409, code, shown + suffix, { reload: true, catchUp: true });
  }

  /* 回合串流開始前的 JSON 錯誤（`status` 與解析過的回應本文，解析不出來是 null）。這些失敗都發生在預扣之前，訊息一律說明沒有扣除遊戲幣。 */
  function describeHttpFailure(status, body) { return requestFailure(fromBody(status, body), NO_CHARGE); }

  /* 其他請求（快照、回合清單、錢包…）的失敗：info 是 `Auth.describeError` 的結果加上 game-api 放進來的欄位。 */
  function describeRequestFailure(info) { return requestFailure(fromInfo(info), ''); }

  /* 串流客戶端（turn-stream.js）沒有 `done` 的結果：開始前被拒、`error` 事件、中斷、逾時、取消。`done` 回傳 null。 */
  var KNOWN_FAILURE_KINDS = {
    canceled: true, timeout: true, network: true, csrf: true, login: true, gate: true, not_found: true, fields: true, coins: true, busy: true, opening_wait: true,
    opening_pending: true, ended: true, conflict: true, rejected: true, limit: true, unavailable: true, unknown: true
  };
  function describeStreamFailure(result) {
    var outcome = isObject(result) ? result.outcome : undefined;
    if (outcome === 'done') { return null; }
    if (outcome === 'rejected') {
      var given = isObject(result.failure) ? result.failure : null;
      if (given && has(KNOWN_FAILURE_KINDS, given.kind)) { return given; }              // describeHttpFailure 的結果原樣
      return failure('unknown', 0, 'config', '這一輪無法開始，請重新整理頁面後再試。' + NO_CHARGE, { reload: true });
    }
    if (outcome === 'error_event') {
      var code = isObject(result.failure) ? codeOf(result.failure.code) : '';
      var superseded = code === 'turn_superseded' || code === 'opening_superseded';
      return failure('stream_error', 0, code, labels.streamErrorMessage(code), { retry: !superseded, catchUp: superseded });
    }
    if (outcome === 'aborted') { return failure('canceled', 0, 'canceled', '', { silent: true, catchUp: true }); }
    var kind = outcome === 'timeout_idle' || outcome === 'timeout_total' ? 'timeout' : 'interrupted';
    return failure(kind, 0, has(labels.STREAM_OUTCOME_LABELS, outcome) ? outcome : 'interrupted', labels.streamOutcomeNotice(outcome), { catchUp: true, retry: true });
  }

  /* 記事簿面板（`GET`／`PUT …/notebook`）的失敗。所有失敗都保留玩家正在編輯的內容（`keepDraft`）。 */
  function describeNotebookFailure(info) {
    var request = fromInfo(info);
    var transport = transportFailure(request, { timeout: labels.notebookText('network'), network: labels.notebookText('network') });
    if (transport) { transport.keepDraft = true; return transport; }
    var status = request.status;
    var code = request.code;
    var keep = { keepDraft: true };
    if (status === 401) { return failure('login', 401, code, '登入已過期，請重新登入。', keep); }
    if (status === 403) { var gate = gateFailure(request, ''); gate.keepDraft = true; return gate; }
    if (status === 404) { return failure('not_found', 404, code, labels.notebookText('not_found'), assign({ link: linkCopy(HOME_LINK) }, keep)); }
    if (status === 409) {
      if (code === 'session_ended') { return failure('ended', 409, code, labels.notebookText('ended'), assign({ ended: true }, keep)); }
      if (code === 'notebook_conflict') {
        return failure('conflict', 409, code, labels.notebookText('conflict'), assign({ notebook: notebookView(request.notebook) }, keep));
      }
      return failure('conflict', 409, code, labels.notebookText('unavailable'), assign({ reload: true }, keep));
    }
    if (status === 422) {
      if (code === 'notebook_too_long') {
        return failure('too_long', 422, code, labels.notebookTooLongNotice(request.length, request.capacity), assign({ length: request.length, capacity: request.capacity }, keep));
      }
      var names = categoryNames(request.categories);
      var message = names ? '記事簿的內容未通過內容規範（' + names + '），無法儲存。請修改後再試。' : labels.notebookText('rejected');
      return failure('rejected', 422, code, message, keep);
    }
    if (status === 400) { return failure('fields', 400, code, labels.notebookText('invalid'), keep); }
    if (status === 429) { return failure('limit', 429, code, '操作太頻繁，請稍後再試。', assign({ retry: true }, keep)); }
    if (status === 503) { return failure('unavailable', 503, code, labels.notebookText('unavailable'), assign({ retry: true }, keep)); }
    return failure('unknown', status, code, labels.notebookText('unavailable'), assign({ retry: true }, keep));
  }

  /* 每日簽到（`POST /api/wallet/checkin`）的失敗。`options.nextResetAt`：錢包讀到的下次可簽到時間（UTC 的 ISO 字串）；409 的本文沒有它。 */
  function describeCheckinFailure(info, options) {
    var request = fromInfo(info);
    var transport = transportFailure(request, { timeout: '連線逾時，簽到沒有完成，請稍後再試。', network: '無法連線到伺服器，簽到沒有完成，請稍後再試。' });
    if (transport) { return transport; }
    var status = request.status;
    var code = request.code;
    if (status === 401) { return failure('login', 401, code, '登入已過期，請重新登入。', {}); }
    if (status === 403) { return gateFailure(request, ''); }
    if (status === 409 && code === 'checkin_overflow') {
      var preview = request.checkin || {};
      var gain = isCount(preview.would_gain) ? preview.would_gain : null;
      var lose = isCount(preview.would_lose) ? preview.would_lose : null;
      return failure('overflow', 409, code, labels.checkinOverflowNotice(gain, lose), {
        checkin: gain !== null && lose !== null ? { wouldGain: gain, wouldLose: lose } : null
      });
    }
    if (status === 409 && code === 'already_checked_in') {
      return failure('already', 409, code, labels.checkinAlreadyNotice(options && options.nextResetAt), {});
    }
    if (status === 429) { return failure('limit', 429, code, '操作太頻繁，請稍後再試。', { retry: true }); }
    if (status === 503) { return failure('unavailable', 503, code, code === 'wallet_busy' ? '遊戲幣目前較忙碌，請稍後再試。' : labels.checkinText('unavailable'), { retry: true }); }
    return failure('unknown', status, code, labels.checkinText('unavailable'), { retry: true });
  }

  // ---- 世界狀態與通知 ---------------------------------------------------------------------------------

  /* 世界狀態面板的玩家視圖（快照的 `world`）：只取白名單欄位與型別，其餘忽略。NPC 的特質、隱藏的歸屬與弧線階段本來就不在伺服器的回應裡。 */
  function worldView(raw) {
    var source = isObject(raw) ? raw : {};
    return {
      elapsed_months: isCount(source.elapsed_months) ? source.elapsed_months : 0,
      location: text(source.location),
      summary: text(source.summary),
      goals: list(source.goals).filter(function (goal) { return typeof goal === 'string'; }),
      places: list(source.places).filter(function (place) { return typeof place === 'string'; }),
      protagonist_age: isCount(source.protagonist_age) ? source.protagonist_age : null
    };
  }

  /* `state` 事件的白名單欄位（只有地點、月數、摘要、目標）；`updated` 不為真或沒有任何可用欄位時回傳 null。 */
  function pickWorld(data) {
    if (!isObject(data) || data.updated !== true) { return null; }
    var world = {};
    if (isCount(data.elapsed_months)) { world.elapsed_months = data.elapsed_months; }
    if (typeof data.location === 'string') { world.location = data.location; }
    if (typeof data.summary === 'string') { world.summary = data.summary; }
    if (Array.isArray(data.goals)) { world.goals = data.goals.filter(function (goal) { return typeof goal === 'string'; }); }
    return Object.keys(world).length > 0 ? world : null;
  }

  /* 把 `state` 事件的部分欄位併進目前的世界狀態（地點、月數、摘要、目標）；去過的地點與主角年齡要等重讀快照才會更新。 */
  function mergeWorld(current, partial) {
    var next = worldView(current);
    var update = isObject(partial) ? partial : {};
    if (isCount(update.elapsed_months)) { next.elapsed_months = update.elapsed_months; }
    if (typeof update.location === 'string') { next.location = update.location; }
    if (typeof update.summary === 'string') { next.summary = update.summary; }
    if (Array.isArray(update.goals)) { next.goals = update.goals.filter(function (goal) { return typeof goal === 'string'; }); }
    return next;
  }

  var NOTICE_KINDS = { age_corrected: true };

  /* 通知的白名單欄位（目前只有年齡更正）；伺服器產生的 `text` 不採用，由 `noticeText` 以固定句型組出。 */
  function pickNotices(raw) {
    var notices = [];
    list(raw).forEach(function (item) {
      if (!isObject(item) || !has(NOTICE_KINDS, item.kind) || !isCount(item.before) || !isCount(item.after)) { return; }
      notices.push({ kind: item.kind, name: text(item.name).slice(0, 60), before: item.before, after: item.after });
    });
    return notices;
  }

  /* 年齡更正通知的文字（含更正前的值）；不是年齡更正或數字不完整時回傳空字串。 */
  function noticeText(notice) {
    if (!isObject(notice) || notice.kind !== 'age_corrected' || !isCount(notice.before) || !isCount(notice.after)) { return ''; }
    var name = text(notice.name).replace(/^\s+|\s+$/g, '');
    return (name || '角色') + '的年齡由 ' + notice.before + ' 歲更正為 ' + notice.after + ' 歲。';
  }

  // ---- 串流事件 -> 一輪的檢視模型 ---------------------------------------------------------------------

  var OPENING_STATES = { pending: true, generating: true, ready: true, fallback: true };
  var TRANSITION_MAX = 200;

  /* 一輪（或第 0 幕，`kind` 為 'opening'）串流的檢視模型。`received` 是目前這個氣泡收到的字元數（只用來判斷有沒有內容，文字本身在打字機裡）。 */
  function newStreamTurn(clientMsgId, action, kind) {
    return {
      kind: kind === 'opening' ? 'opening' : 'turn', clientMsgId: text(clientMsgId), action: text(action), seq: null, replayed: false, attempt: null,
      bubble: 0, received: 0, willRetry: false, status: null, turnNumber: null, coins: null, openingState: null, ended: false,
      finished: false, failed: false
    };
  }

  /* 處理一個串流事件 `{ event, data }`；回傳 `{ turn, effects }`，不修改傳入的 `turn`。`done` 之後的事件一律忽略。 */
  function applyStreamEvent(turn, event) {
    var next = copy(turn);
    var effects = [];
    if (!isObject(event) || typeof event.event !== 'string' || !isObject(event.data) || next.finished) { return { turn: next, effects: effects }; }
    var data = event.data;
    if (event.event === 'meta') {
      if (isCount(data.turn_id)) { next.seq = data.turn_id; }
      if (isCount(data.attempt)) { next.attempt = data.attempt; }
      next.replayed = data.replayed === true;
      if (data.kind === 'opening') { next.kind = 'opening'; }
      effects.push({ type: 'meta', seq: next.seq, replayed: next.replayed });
    } else if (event.event === 'token') {
      var chunk = text(data.text);
      if (chunk !== '') {
        next.received += chunk.length;
        effects.push({ type: 'push', text: chunk });
      }
    } else if (event.event === 'transition') {
      var line = text(data.text);
      if (line === '' || line.length > TRANSITION_MAX) { line = labels.historyLine('blocked'); }
      next.received = 0;
      next.bubble += 1;
      next.willRetry = data.will_retry === true;
      effects.push({ type: 'discard' });                                             // 丟棄目前尚未完成的氣泡
      effects.push({ type: 'transition', text: line, willRetry: next.willRetry });
    } else if (event.event === 'state') {
      var world = pickWorld(data);
      if (world) { effects.push({ type: 'world', world: world }); }
      var notices = pickNotices(data.notices);
      if (notices.length > 0) {
        effects.push({
          type: 'notices',
          notices: notices.map(function (notice) { return { kind: notice.kind, name: notice.name, before: notice.before, after: notice.after, text: noticeText(notice) }; })
        });
      }
    } else if (event.event === 'done') {
      var status = has(labels.DONE_STATUS_LABELS, data.status) ? data.status : 'unknown';
      next.finished = true;
      next.status = status;
      next.turnNumber = isCount(data.turn_number) ? data.turn_number : null;
      next.coins = isCount(data.coins) ? data.coins : null;
      next.ended = status === 'ended';
      next.openingState = has(OPENING_STATES, data.opening_state) ? data.opening_state : null;
      if (status === 'complete' || status === 'ended') {
        effects.push({ type: 'finish' });
      } else {
        next.received = 0;
        effects.push({ type: 'discard' });                                           // 被擋或狀態不明：不保留任何文字
        if (status === 'blocked') { effects.push({ type: 'hint', text: labels.blockedHint() }); }
      }
      if (next.coins !== null) { effects.push({ type: 'coins', balance: next.coins }); }
      effects.push({ type: 'done', status: status, turnNumber: next.turnNumber, ended: next.ended, openingState: next.openingState });
    } else if (event.event === 'error') {
      next.failed = true;
      next.received = 0;
      effects.push({ type: 'discard' });
      effects.push({ type: 'failure', failure: describeStreamFailure({ outcome: 'error_event', failure: { code: data.code } }) });
    }
    return { turn: next, effects: effects };                                         // ping 與不認得的事件沒有任何效果
  }

  /* 串流客戶端回報結果之後（`turn-stream.run` 的結果）的收尾：`done` 已由事件處理；其他結果丟棄尚未完成的氣泡並說明。
     被擋與中斷的回合不保存任何文字；前端中止不代表後端中止，所以失敗的 `catchUp` 為真時，呼叫端要以 since_seq 補齊。 */
  function applyStreamEnd(turn, result) {
    var next = copy(turn);
    var effects = [];
    var outcome = isObject(result) ? result.outcome : undefined;
    if (next.finished || outcome === 'done') { return { turn: next, effects: effects }; }
    next.finished = true;
    next.received = 0;
    next.status = outcome === 'rejected' ? 'rejected' : 'interrupted';
    effects.push({ type: 'discard' });
    var alreadyShown = next.failed && outcome === 'error_event';                     // `error` 事件已經產生過一次 failure
    var described = describeStreamFailure(result);
    if (described && !alreadyShown) { effects.push({ type: 'failure', failure: described }); }
    return { turn: next, effects: effects };
  }

  // ---- 回合、第 0 幕與時間軸 --------------------------------------------------------------------------

  var TURN_STATUSES = { streaming: true, complete: true, blocked: true, interrupted: true };

  /* 伺服器的回合（`turn_view`）整理成固定欄位。只有 `complete` 帶行動與劇情；其他狀態一律沒有文字（被擋的文字永遠不離開伺服器，
     中斷的片段也不保存）；不認得的狀態當成 `interrupted`。沒有整數序號的資料回傳 null。 */
  function normalizeTurn(raw) {
    if (!isObject(raw) || !isCount(raw.seq)) { return null; }
    var status = has(TURN_STATUSES, raw.status) ? raw.status : 'interrupted';
    var complete = status === 'complete';
    return {
      seq: raw.seq,
      client_msg_id: text(raw.client_msg_id).slice(0, 200),
      turn_number: isCount(raw.turn_number) ? raw.turn_number : null,
      status: status,
      action: complete ? text(raw.action) : '',
      story: complete ? text(raw.story) : ''
    };
  }

  /* 合併本機快取與後端的回合：以 `seq` 為準，同序號後端覆寫本機；結果依 `seq` 遞增。傳入的陣列不被修改。 */
  function mergeTurns(local, remote) {
    var bySeq = Object.create(null);
    [local, remote].forEach(function (group) {
      list(group).forEach(function (raw) {
        var turn = normalizeTurn(raw);
        if (turn) { bySeq[turn.seq] = turn; }
      });
    });
    return Object.keys(bySeq).map(function (key) { return bySeq[key]; }).sort(function (a, b) { return a.seq - b.seq; });
  }

  /* 這些回合裡有沒有該 `client_msg_id` 的完整回合（在途回合是否已經完成）。 */
  function hasCompleteTurn(turns, clientMsgId) {
    if (typeof clientMsgId !== 'string' || clientMsgId === '') { return false; }
    return list(turns).some(function (turn) { return isObject(turn) && turn.status === 'complete' && turn.client_msg_id === clientMsgId; });
  }

  function noteKey(turn) { return 'note:' + (turn.client_msg_id || 'seq-' + turn.seq); }

  /* 時間軸：第 0 幕、依 `seq` 遞增的回合、尚未確認完成的在途回合。
     項目 `{ key, kind, … }`：`opening`（text）、`turn`（完整回合：action、story）、`note`（未完成的回合那一行說明，tone 為 transition／interrupted／busy）、
     `inflight`（在途回合的行動，供「重送／放棄」）。完整回合的 key 是 `turn:<client_msg_id>`，與串流中的氣泡同一個 key，所以補齊時就地取代而不閃爍。 */
  function timelineFrom(opening, turns, inflight) {
    var items = [];
    var seen = Object.create(null);
    function add(item) {
      if (seen[item.key]) { return; }
      seen[item.key] = true;
      items.push(item);
    }
    var scene = isObject(opening) ? opening : null;
    if (scene && typeof scene.text === 'string' && scene.text !== '') {
      add({ key: 'opening', kind: 'opening', text: scene.text, state: has(OPENING_STATES, scene.state) ? scene.state : null });
    }
    var normalized = mergeTurns(turns, []);
    normalized.forEach(function (turn) {
      if (turn.status === 'complete') {
        add({
          key: 'turn:' + (turn.client_msg_id || 'seq-' + turn.seq), kind: 'turn', seq: turn.seq, clientMsgId: turn.client_msg_id,
          turnNumber: turn.turn_number, action: turn.action, story: turn.story, streaming: false
        });
        return;
      }
      var tone = turn.status === 'blocked' ? 'transition' : turn.status === 'streaming' ? 'busy' : 'interrupted';
      add({ key: noteKey(turn), kind: 'note', tone: tone, seq: turn.seq, text: labels.historyLine(turn.status) });
    });
    if (isObject(inflight) && typeof inflight.client_msg_id === 'string' && inflight.client_msg_id !== '' && !hasCompleteTurn(normalized, inflight.client_msg_id)) {
      add({ key: 'inflight:' + inflight.client_msg_id, kind: 'inflight', clientMsgId: inflight.client_msg_id, action: text(inflight.action) });
    }
    return items;
  }

  // ---- 記事簿 -----------------------------------------------------------------------------------------

  /* 記事簿檢視（`GET`／`PUT …/notebook`、衝突時的 `notebook`）整理成固定欄位；不是記事簿的形狀回傳 null。 */
  function notebookView(raw) {
    if (!isObject(raw) || !isCount(raw.version) || typeof raw.text !== 'string') { return null; }
    return {
      text: raw.text, version: raw.version, length: isCount(raw.length) ? raw.length : notebookLength(raw.text),
      capacity: isCount(raw.capacity) ? raw.capacity : 0, reserve: isCount(raw.reserve) ? raw.reserve : 0, full: raw.full === true
    };
  }

  // ---- 送出行動 ---------------------------------------------------------------------------------------

  /* 目前不能送出的原因（可以送出回傳空字串）。失敗時關閉：狀態缺漏、第 0 幕未明確完成都視為不能送出。
     state：{ gate, ended, offline, openingReady, inflight, busy }。順序固定：閘門、已完結、離線、第 0 幕、在途回合、進行中。 */
  function sendBlocker(state) {
    if (!isObject(state)) { return 'opening'; }
    if (state.gate) { return 'gate'; }
    if (state.ended === true) { return 'ended'; }
    if (state.offline === true) { return 'offline'; }
    if (state.openingReady !== true) { return 'opening'; }
    if (state.inflight) { return 'inflight'; }
    if (state.busy === true) { return 'busy'; }
    return '';
  }

  function canSend(state) { return sendBlocker(state) === ''; }

  /* 行動文字的即時檢查：空白或超過上限回傳固定說明，沒問題回傳空字串。長度以 NFC 之後的 Unicode 字元計，頭尾空白不算；
     `max` 不是正整數時不檢查長度（上限由伺服器為準）。 */
  function actionProblem(value, max) {
    var trimmed = typeof value === 'string' ? value.replace(/^\s+|\s+$/g, '') : '';
    if (trimmed === '') { return '請輸入你的行動。'; }
    if (isCount(max) && max > 0) {
      var count = characterCount(trimmed);
      if (count > max) { return '行動最多 ' + max + ' 字（目前 ' + count + ' 字）。'; }
    }
    return '';
  }

  return {
    characterCount: characterCount,
    notebookLength: notebookLength,
    describeHttpFailure: describeHttpFailure,
    describeRequestFailure: describeRequestFailure,
    describeStreamFailure: describeStreamFailure,
    describeNotebookFailure: describeNotebookFailure,
    describeCheckinFailure: describeCheckinFailure,
    worldView: worldView,
    mergeWorld: mergeWorld,
    noticeText: noticeText,
    newStreamTurn: newStreamTurn,
    applyStreamEvent: applyStreamEvent,
    applyStreamEnd: applyStreamEnd,
    normalizeTurn: normalizeTurn,
    mergeTurns: mergeTurns,
    hasCompleteTurn: hasCompleteTurn,
    timelineFrom: timelineFrom,
    notebookView: notebookView,
    sendBlocker: sendBlocker,
    canSend: canSend,
    actionProblem: actionProblem
  };
});
