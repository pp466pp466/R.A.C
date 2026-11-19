/* 帳號頁「安全模式」區塊的純函式與狀態機（與框架無關，Node 可以直接測試；見 account-safe-mode-setting 規格）。
 *
 * 在瀏覽器中，載入後會把內容放到全域的 `Auth.safeMode`。這裡只決定「怎麼顯示、什麼時候送出請求、失敗怎麼還原」；
 * 模式規則（生效模式、同意版本、緊急煞車）永遠由伺服器判斷，前端只拿到列舉值與布林：
 *   { effective: 'safe' | 'adult', confirmed: 布林, forced: 布林, returnPath: 已驗證的站內路徑或空值 }
 * 初始狀態不含生日、年齡區間、同意時間戳記或內部編號。所有文字都是固定的繁體中文純文字，頁面一律以 {{ }} 插值當作純文字顯示，不解析為 HTML。
 *
 * 狀態機（createController）：
 *   - 開 → 關：已在目前版本同意 → 直接送出；尚未同意 → 先開同意 Modal（勾選之前不能確認，取消與 Esc 不送出任何請求）；
 *   - 關 → 開：直接送出，不需要確認；
 *   - 緊急煞車啟用時顯示為開並禁用，任何操作都不送出請求；請求期間禁用，無論成功、失敗或中斷都在 finally 恢復；
 *   - 請求失敗時開關維持原狀（不樂觀更新）並顯示說明；帶有效返回位置時，成功修改後短暫顯示 Toast 再導向該位置。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Auth = root.Auth || {};
    root.Auth.safeMode = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MODE_SAFE = 'safe';                      // 安全模式開（預設）
  var MODE_ADULT = 'adult';                    // 安全模式關
  var ENDPOINT = '/api/account/content-mode';
  var RETURN_PREFIX = '/game/';
  var MAX_RETURN_LENGTH = 200;
  var NAVIGATE_DELAY_MS = 900;                 // 讓玩家看到 Toast 之後再導向

  /* 固定文案：伺服器不提供任何文字，模型與玩家的文字也不會進到這裡。 */
  var COPY = {
    title: '安全模式',
    onLabel: '開',
    offLabel: '關',
    stateOn: '安全模式開：劇情不會出現性內容，也不會有露骨的暴力血腥描寫。這是預設值。',
    stateOff: '安全模式關：成年角色之間合意的露骨內容可能出現；絕對禁止的內容仍然不允許。是否出現取決於模型，不保證。',
    boundNote: '綁定安全模式的劇本，不論這個設定，一律以安全模式遊玩。',
    forcedNote: '目前系統暫時只提供安全模式。',
    returnHint: '改完之後會回到您剛才選的劇本。',
    returnLink: '完成並返回',
    modalTitle: '關閉安全模式',
    modalCode: 'SAFE_MODE_OFF',
    modalIntro: '關閉之前，請先閱讀下列安全規範：',
    rulesPoints: [
      '本服務含露骨內容：關閉安全模式後，成年角色之間合意的露骨內容可能出現在劇情中；是否產出取決於模型，不保證。',
      '無論是否關閉，下列內容一律禁止，並由伺服器的審查層擋下：涉及未成年（含看似未成年的角色）的性內容、性暴力、非自願的性情節，以及以真人名義的性內容。',
      '敏感內容可能被模型供應商留存或人工審閱，請勿輸入真實個資。',
      '您可以隨時回到安全模式；綁定安全模式的劇本一律以安全模式遊玩，不受這個設定影響。'
    ],
    agreeLabel: '我同意安全規範',
    confirm: '確認關閉安全模式',
    cancel: '取消',
    busy: '處理中…'
  };

  var MESSAGES = {
    switchedOff: '安全模式已關閉。',
    switchedOn: '安全模式已開啟。',
    returning: '，正在回到劇本…',
    forced: '目前系統暫時只提供安全模式，無法關閉。',
    needAgreement: '請勾選同意安全規範。',
    ageRequired: '請先完成年齡確認，再調整安全模式。',
    malformed: '無法確認目前的設定，請重新整理頁面後再試。'
  };

  // ---- 返回位置 ----------------------------------------------------------------

  /* 與伺服器端 `app/security/redirects.py` 的 `safe_return_path` 使用同一份規則（兩邊的測試共用同一張案例表）：
   * 只有以 `/game/` 開頭的站內路徑有效；含 `//`、反斜線、控制字元、`..` 段落、`://`，或超過 200 字的一律丟棄。
   * 連同百分比編碼解開後再檢查一次（`%2f%2f`、`%5c`、`%0d` 不能繞過）。回傳驗證過的字串，無效時回傳 null。 */
  function decodeAscii(value) {
    // 只解開 ASCII 範圍的 %XX（其餘保留）：不會因格式錯誤的編碼而丟出例外，也與 Python 的 unquote 在這些字元上的結果一致
    return value.replace(/%([0-9a-fA-F]{2})/g, function (match, hex) {
      var code = parseInt(hex, 16);
      return code < 0x80 ? String.fromCharCode(code) : match;
    });
  }

  function safeReturnPath(value) {
    if (typeof value !== 'string' || !value || value.length > MAX_RETURN_LENGTH) { return null; }
    if (value.indexOf(RETURN_PREFIX) !== 0) { return null; }
    var candidates = [value, decodeAscii(value)];
    for (var i = 0; i < candidates.length; i += 1) {
      var candidate = candidates[i];
      if (candidate.indexOf('\\') >= 0 || candidate.indexOf('//') >= 0) { return null; }
      if (/[\u0000-\u001f\u007f]/.test(candidate)) { return null; }
      var pathOnly = candidate.split('?')[0].split('#')[0];
      if (pathOnly.indexOf('://') >= 0) { return null; }
      if (pathOnly.split('/').indexOf('..') >= 0) { return null; }
    }
    return value;
  }

  // ---- 狀態 --------------------------------------------------------------------

  /* 把伺服器給的初始狀態整理成頁面用的欄位。缺少或不合法（例如管理員尚未完成年齡確認）時 `available` 為 false，區塊不顯示。
   * `safe` 是畫面上的開關：緊急煞車啟用時一律為開（玩家保存的設定在伺服器端保留，煞車解除後恢復）。 */
  function normalize(raw) {
    var valid = raw && typeof raw === 'object' && (raw.effective === MODE_SAFE || raw.effective === MODE_ADULT);
    if (!valid) {
      return { available: false, safe: true, confirmed: false, forced: false, returnPath: null };
    }
    var forced = raw.forced === true;
    return {
      available: true,
      safe: forced || raw.effective !== MODE_ADULT,
      confirmed: raw.confirmed === true,
      forced: forced,
      returnPath: safeReturnPath(raw.returnPath)
    };
  }

  /* 切換開關時要做什麼（純函式，不改變狀態）：
   *   none    — 忙碌中、緊急煞車、區塊不可用：沒有任何動作
   *   confirm — 開 → 關且尚未在目前版本同意：先開同意 Modal
   *   send    — 直接送出請求（body 是請求本文）
   */
  function planToggle(view) {
    if (!view || !view.available || view.busy || view.forced) { return { action: 'none' }; }
    if (view.safe) {
      return view.confirmed ? { action: 'send', body: { mode: MODE_ADULT } } : { action: 'confirm' };
    }
    return { action: 'send', body: { mode: MODE_SAFE } };
  }

  /* 同意 Modal 的送出本文：必須明確勾選，才會帶 confirm_rules（伺服器只接受布林的 true）。 */
  function confirmBody(agreed) {
    return agreed === true ? { mode: MODE_ADULT, confirm_rules: true } : null;
  }

  /* 把失敗的回應轉成要顯示的繁體中文說明（`info` 來自 Auth.describeError）。 */
  function describeFailure(info) {
    var fields = (info && info.fields) || {};
    if (info && info.code === 'safe_mode_forced') { return MESSAGES.forced; }
    if (info && info.code === 'age_confirmation_required') { return MESSAGES.ageRequired; }
    if (fields.confirm_rules) { return MESSAGES.needAgreement; }
    return (info && info.message) || '發生錯誤，請稍後再試。';
  }

  // ---- 狀態機 ------------------------------------------------------------------

  /* options：
   *   initial       伺服器的初始狀態（見 normalize）
   *   request(body) 送出 PUT，回傳 axios 風格的 Promise（成功時 response.data.content_mode 是新的狀態）
   *   describeError 失敗時整理錯誤（Auth.describeError）
   *   toast(message, settings) 提示；navigate(path) 導向；later(fn, ms) 延後執行（預設 setTimeout）
   *   reactive      讓 view 成為響應式（Vue.reactive），預設原樣
   *   onError(info) 失敗之後的額外處理（例如 401 回登入頁、鎖定的申訴捷徑）
   */
  function createController(options) {
    options = options || {};
    var reactive = options.reactive || function (value) { return value; };
    var toast = options.toast || function () {};
    var navigate = options.navigate || function () {};
    var later = options.later || function (fn, ms) { return setTimeout(fn, ms); };
    var describe = options.describeError || function (error) {
      return { kind: 'server', message: (error && error.message) || '', fields: {}, code: '', status: 0 };
    };

    var view = reactive(Object.assign(normalize(options.initial), { busy: false, modalOpen: false, agreed: false, error: '' }));

    function apply(contentMode) {
      var next = contentMode && typeof contentMode === 'object' ? contentMode : null;
      if (!next || (next.effective !== MODE_SAFE && next.effective !== MODE_ADULT)) { return false; }
      view.forced = next.forced === true;
      view.confirmed = next.confirmed === true;
      view.safe = view.forced || next.effective !== MODE_ADULT;
      return true;
    }

    function closeModal() {
      view.modalOpen = false;
      view.agreed = false;
    }

    function fail(info) {
      view.error = describeFailure(info);
      if (info && info.code === 'safe_mode_forced') {        // 伺服器說煞車已啟用：畫面跟上，Modal 不再有意義
        view.forced = true;
        view.safe = true;
        closeModal();
      }
      if (options.onError) { options.onError(info); }
    }

    function send(body) {
      view.busy = true;
      view.error = '';
      return Promise.resolve()
        .then(function () { return options.request(body); })
        .then(function (response) {
          if (!apply(response && response.data && response.data.content_mode)) {
            view.error = MESSAGES.malformed;
            return false;
          }
          closeModal();
          var message = body.mode === MODE_ADULT ? MESSAGES.switchedOff : MESSAGES.switchedOn;
          var target = view.returnPath;
          if (target) {
            toast(message.replace(/。$/, '') + MESSAGES.returning);
            later(function () { navigate(target); }, NAVIGATE_DELAY_MS);
          } else {
            toast(message);
          }
          return true;
        }, function (error) {
          var info = describe(error);
          if (info.kind === 'canceled') { return false; }
          fail(info);
          return false;
        })
        .finally(function () { view.busy = false; });         // 無論成功、失敗或中斷，一定恢復
    }

    /* 玩家按下開關。 */
    function toggle() {
      var plan = planToggle(view);
      if (plan.action === 'confirm') {
        view.error = '';
        view.agreed = false;
        view.modalOpen = true;
        return Promise.resolve(false);
      }
      if (plan.action === 'send') { return send(plan.body); }
      return Promise.resolve(false);
    }

    /* 同意 Modal 的「確認」：沒勾選或忙碌中不送出。 */
    function confirm() {
      var body = confirmBody(view.agreed);
      if (!body || view.busy || view.forced || !view.modalOpen) { return Promise.resolve(false); }
      return send(body);
    }

    /* 同意 Modal 的「取消」、Esc 與遮罩：不送出任何請求，開關維持原狀。忙碌中不能取消（請求還在進行）。 */
    function cancel() {
      if (view.busy) { return; }
      closeModal();
      view.error = '';
    }

    return { view: view, toggle: toggle, confirm: confirm, cancel: cancel };
  }

  return {
    MODE_SAFE: MODE_SAFE,
    MODE_ADULT: MODE_ADULT,
    ENDPOINT: ENDPOINT,
    RETURN_PREFIX: RETURN_PREFIX,
    MAX_RETURN_LENGTH: MAX_RETURN_LENGTH,
    NAVIGATE_DELAY_MS: NAVIGATE_DELAY_MS,
    COPY: COPY,
    MESSAGES: MESSAGES,
    safeReturnPath: safeReturnPath,
    normalize: normalize,
    planToggle: planToggle,
    confirmBody: confirmBody,
    describeFailure: describeFailure,
    createController: createController
  };
});
