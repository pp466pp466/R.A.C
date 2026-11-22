/* 劇本詳情頁的檢舉（見 script-reports 規格「劇本詳情頁提供檢舉入口」與 rpg-script-reports 的 design D9）：固定的理由清單、驗證與請求本文、失敗對應的畫面文字。
 *
 * 純函式與純資料，與頁面無關，Node 可以直接測試：
 * - `REASONS`／`reasons()`：七個固定理由（值與順序和後端的 `REPORT_REASONS` 相同，也是畫面上的順序）與中文標籤；`reasons()` 回傳複本，呼叫端改動不影響下一次。
 * - `reasonLabel(value)`：理由的中文標籤；不認得的值退回固定文字，不把原始值顯示給玩家。
 * - `stateOf(value)`：詳情回應的 `report_state` 整理成三個列舉之一（`available`、`reported`、`unavailable`）；其他一律當成 `unavailable`（失敗時關閉：不顯示入口）。
 * - `countText(text)`：自寫文字的字數（Unicode 字元，不是 UTF-16 單元），先照伺服器的規則整理（NFC、去除控制字元與頭尾空白）再計，所以與伺服器的判斷一致。
 * - `validate(selected, text)`：回傳 `{ ok, reasons, text, length, errors }`。`reasons` 是依 `REASONS` 順序排列的已選值（不認得的值與重複值不算已選）；
 *   沒有任何已選理由時 `ok` 為 false；文字超過 200 字時 `ok` 為 false 並在 `errors.text` 說明。伺服器仍會重新驗證，這裡只決定確認鈕能不能按。
 * - `requestBody(selected, text)`：`{ reasons: […] }`，文字非空才多一個 `text`（整理後的文字）。
 * - `stateAfter(failure)`：送出之後檢舉入口的狀態。`failure` 為 `null`（成功）或失敗物件（`{ status, code }`）：成功與 409 `already_reported` 是 `reported`；
 *   403 `report_own_script` 與 404 是 `unavailable`（這個版本不可再檢舉）；其他失敗（400、429、401、網路）回傳 `null`，表示狀態不變、玩家可以再試。
 * - `failureText(failure)`：失敗對應的畫面文字（固定的繁體中文，不顯示伺服器訊息；400 例外，沿用伺服器指出的欄位說明）。
 * - `copy()`：按鈕、Modal 與 Toast 的固定文字。文字不來自伺服器、劇本或玩家。
 * 這裡不送請求、不寫任何瀏覽器儲存、不碰 `window`；自寫文字只存在於呼叫端的表單狀態與請求本文，不記錄。所有文字都是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.scriptReport = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var TEXT_MAX = 200;
  var STATES = ['available', 'reported', 'unavailable'];
  /* 值與順序與後端 `app/models/script_report.py` 的 `REPORT_REASONS` 相同（守門測試比對）。 */
  var REASONS = [
    { value: 'minor_sexual', label: '涉及未成年的性內容' },
    { value: 'sexual_violence', label: '性暴力或非自願情節' },
    { value: 'real_person_sexual', label: '以真人名義的性內容' },
    { value: 'tier_mismatch', label: '內容分級不符' },
    { value: 'plagiarism', label: '抄襲或侵權' },
    { value: 'privacy_harassment', label: '洩漏個資或騷擾' },
    { value: 'other', label: '其他' }
  ];
  var UNKNOWN_LABEL = '其他';

  var COPY = {
    code: 'REPORT_SCRIPT',
    openButton: '檢舉',
    reportedButton: '已檢舉',
    title: '檢舉這個劇本',
    intro: '請勾選檢舉的原因（可複選）。檢舉不會自動下架，只會提高管理員審核的優先度；您不會收到處理結果的通知。',
    reasonsLegend: '檢舉原因',
    textLabel: '補充說明（選填）',
    textHint: '純文字，至多 200 字。請不要填寫個人資料；內容只供管理員檢視，不會送入模型。',
    confirm: '送出檢舉',
    sending: '送出中…',
    cancel: '取消',
    close: '關閉',
    success: '已送出檢舉，謝謝您的回報。'
  };

  function copy() {
    var result = {};
    Object.keys(COPY).forEach(function (key) { result[key] = COPY[key]; });
    return result;
  }

  function reasons() { return REASONS.map(function (item) { return { value: item.value, label: item.label }; }); }

  function reasonLabel(value) {
    for (var i = 0; i < REASONS.length; i += 1) { if (REASONS[i].value === value) { return REASONS[i].label; } }
    return UNKNOWN_LABEL;
  }

  function stateOf(value) { return STATES.indexOf(value) >= 0 ? value : 'unavailable'; }

  /* 與伺服器的 `clean_single_line` 同一個規則：NFC、去除控制字元（含換行與 Tab）、去頭尾空白。 */
  function cleanText(text) {
    var value = typeof text === 'string' ? text : '';
    if (typeof value.normalize === 'function') { value = value.normalize('NFC'); }
    return value.replace(/[\u0000-\u001f\u007f-\u009f]/g, '').trim();
  }

  function countText(text) { return Array.from(cleanText(text)).length; }

  function chosen(selected) {
    var list = Array.isArray(selected) ? selected : [];
    return REASONS.map(function (item) { return item.value; }).filter(function (value) { return list.indexOf(value) >= 0; });
  }

  function validate(selected, text) {
    var picked = chosen(selected);
    var cleaned = cleanText(text);
    var length = Array.from(cleaned).length;
    var errors = {};
    if (picked.length === 0) { errors.reasons = '請至少選擇一個檢舉原因。'; }
    if (length > TEXT_MAX) { errors.text = '補充說明至多 ' + TEXT_MAX + ' 字，目前 ' + length + ' 字。'; }
    return { ok: Object.keys(errors).length === 0, reasons: picked, text: cleaned, length: length, errors: errors };
  }

  function requestBody(selected, text) {
    var body = { reasons: chosen(selected) };
    var cleaned = cleanText(text);
    if (cleaned !== '') { body.text = cleaned; }
    return body;
  }

  function stateAfter(failure) {
    if (failure === null || failure === undefined) { return 'reported'; }
    if (failure.status === 409 && failure.code === 'already_reported') { return 'reported'; }
    if (failure.status === 403 && failure.code === 'report_own_script') { return 'unavailable'; }
    if (failure.status === 404) { return 'unavailable'; }
    return null;
  }

  function failureText(failure) {
    var status = failure && typeof failure.status === 'number' ? failure.status : 0;
    var code = failure && typeof failure.code === 'string' ? failure.code : '';
    if (status === 429 && code === 'report_limit') { return '您今天的檢舉次數已用完，明天（台灣時間 00:00 之後）再試。'; }
    if (status === 409 && code === 'already_reported') { return '您已經檢舉過這個版本了。'; }
    if (status === 404) { return '找不到這個劇本，它可能已經下架。'; }
    if (status === 403 && code === 'report_own_script') { return '不能檢舉自己的劇本。'; }
    if (status === 403 && code === 'age_confirmation_required') { return '請先完成年齡確認，才能檢舉劇本。'; }
    if (status === 400) {
      var fields = failure && failure.fields && typeof failure.fields === 'object' ? failure.fields : {};
      var named = typeof fields.reasons === 'string' && fields.reasons ? fields.reasons : (typeof fields.text === 'string' && fields.text ? fields.text : '');
      return named || '檢舉的內容不符合規定，請檢查後再試。';
    }
    if (failure && (failure.kind === 'network' || failure.kind === 'timeout')) { return '連線不穩，這次沒有送出，請稍後再試。'; }
    return '暫時無法送出檢舉，請稍後再試。';
  }

  return {
    TEXT_MAX: TEXT_MAX,
    REASONS: REASONS,
    reasons: reasons,
    reasonLabel: reasonLabel,
    stateOf: stateOf,
    countText: countText,
    validate: validate,
    requestBody: requestBody,
    stateAfter: stateAfter,
    failureText: failureText,
    copy: copy
  };
});
