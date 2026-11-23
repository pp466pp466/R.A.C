/* 登入後設定的分步精靈（`/age`）：步驟、進度、各步可前進的條件、錯誤回到對應步驟，以及一次送出的流程（onboarding-wizard 規格）。
 *
 * 純函式加依賴注入的流程控制器，與框架無關，Node 可以直接測試；頁面腳本（age.js）只負責 Vue 接線（步驟索引、欄位、Modal、焦點）。
 * 必須在 auth-flows.js 之後載入（瀏覽器）；對外的全域名稱是 `Onboarding`。
 *
 * - **步驟是資料驅動的清單**（`STEPS`）：進度條段數、「第 N 步，共 M 步」、上一步與「下一步／開始」都由清單長度與索引算出，
 *   日後新增步驟（例如喜歡的類型）不必改動這裡的前進、後退與進度邏輯。
 * - **不檢查暱稱格式**：長度、網址與電話樣式、署名只由伺服器在最後送出時判斷（前後端不各寫一份規則）；格式錯誤由
 *   `stepForFailure` 帶回第 1 步補償。
 * - **一次送出**：`createOnboarding().submit` 先 `POST /api/auth/verify-age`（生日、區間、暱稱，全有或全無；契約不變），成功之後才在
 *   所選偏好不是預設的 `all` 時 `PUT /api/account/story-preference`（待確認年齡的玩家不能呼叫它，所以一定在確認通過之後）；偏好請求
 *   失敗或逾時一律只吞掉，不影響導向。`verify-age` 失敗時拒絕（rejects）原本的錯誤，讓 `Auth.useForm` 照舊處理欄位錯誤與提示。
 * - **不碰任何瀏覽器儲存**（含生日，個資）：這個檔案沒有 localStorage、sessionStorage、IndexedDB 或 cookie 的存取，測試以讀原始碼證明。
 * 所有文字都是繁體中文純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。 */
(function (root, factory) {
  'use strict';
  var flows = (typeof module === 'object' && module.exports) ? require('./auth-flows.js') : root.Auth;
  var api = factory(flows);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Onboarding = api;
  }
})(typeof self !== 'undefined' ? self : this, function (Flows) {
  'use strict';

  var VERIFY_AGE_PATH = '/api/auth/verify-age';
  var STORY_PREFERENCE_PATH = '/api/account/story-preference';
  var DEFAULT_PREFERENCE = 'all';                 // 與伺服器的預設（缺值視為全讀者）一致：選預設值時不發請求
  var PREFERENCE_TIMEOUT_MS = 5000;               // 偏好請求最多等這麼久，逾時照樣導向

  var STEP_WELCOME = 'welcome';
  var STEP_AGE = 'age';
  var STEP_PREFERENCE = 'preference';
  var STEPS = [
    { id: STEP_WELCOME, title: '歡迎' },
    { id: STEP_AGE, title: '年齡確認' },
    { id: STEP_PREFERENCE, title: '故事偏好' }
  ];

  var TEXT = {
    needNickname: Flows.TEXT.needNickname,
    needAcknowledge: '請先閱讀並確認使用守則。',
    needBirthDate: Flows.TEXT.needBirthDate,
    needAgeRange: Flows.TEXT.needAgeRange
  };

  // 欄位錯誤對應到哪一步（伺服器的欄位名稱）；同時有多個時回到最前面的那一步
  var FIELD_STEP = { nickname: STEP_WELCOME, birth_date: STEP_AGE, age_range: STEP_AGE };
  // `age_mismatch` 的處理結果是清空這兩個欄位（`Auth.ageOutcome`）：它們在第 2 步
  var CLEARED_STEP = { birthDate: STEP_AGE, ageRange: STEP_AGE };

  function isString(value) { return typeof value === 'string'; }

  function clampIndex(index, steps) {
    var last = steps.length - 1;
    if (typeof index !== 'number' || index !== index) { return 0; }
    return Math.max(0, Math.min(last, Math.floor(index)));
  }

  /* 進度：`segments` 是進度條每一段是否已填滿（目前步驟與之前的步驟為真），`label` 給輔助科技讀。 */
  function progress(index, steps) {
    steps = steps || STEPS;
    var i = clampIndex(index, steps);
    var total = steps.length;
    return {
      step: steps[i].id,
      index: i,
      current: i + 1,
      total: total,
      label: '第 ' + (i + 1) + ' 步，共 ' + total + ' 步',
      segments: steps.map(function (step, position) { return position <= i; }),
      isFirst: i === 0,
      isLast: i === total - 1,
      primaryLabel: i === total - 1 ? '開始' : '下一步'
    };
  }

  function nextIndex(index, steps) { steps = steps || STEPS; return clampIndex(clampIndex(index, steps) + 1, steps); }
  function previousIndex(index, steps) { steps = steps || STEPS; return clampIndex(clampIndex(index, steps) - 1, steps); }

  function filled(value) { return isString(value) && value.trim() !== ''; }

  /* 各步的「下一步」是否可用。`reason` 是固定的繁體中文說明（按鈕不可用時顯示，文字，不只靠顏色）。
     暱稱只檢查有沒有填；格式由伺服器判斷。沒有定義條件的步驟（含日後新增的）一律可以前進。 */
  function canAdvance(stepId, values, acknowledged) {
    var v = values || {};
    if (stepId === STEP_WELCOME) {
      if (!filled(v.nickname)) { return { ok: false, reason: TEXT.needNickname }; }
      if (acknowledged !== true) { return { ok: false, reason: TEXT.needAcknowledge }; }
      return { ok: true, reason: '' };
    }
    if (stepId === STEP_AGE) {
      if (!filled(v.birthDate)) { return { ok: false, reason: TEXT.needBirthDate }; }
      if (!filled(v.ageRange)) { return { ok: false, reason: TEXT.needAgeRange }; }
      return { ok: true, reason: '' };
    }
    return { ok: true, reason: '' };
  }

  /* `verify-age` 失敗後要回到哪一步（`outcome` 是 `Auth.ageOutcome` 的結果）；不需要換步驟時回傳 null（留在目前這一步）。
     暱稱的欄位錯誤 -> 第 1 步；生日或區間的欄位錯誤、`age_mismatch`（清空生日與區間）-> 第 2 步。 */
  function stepForFailure(outcome, steps) {
    steps = steps || STEPS;
    if (!outcome || typeof outcome !== 'object') { return null; }
    var wanted = [];
    if (outcome.action === 'fields' && outcome.fields && typeof outcome.fields === 'object') {
      Object.keys(outcome.fields).forEach(function (name) { if (FIELD_STEP[name]) { wanted.push(FIELD_STEP[name]); } });
    }
    if (Array.isArray(outcome.clear)) {
      outcome.clear.forEach(function (name) { if (CLEARED_STEP[name]) { wanted.push(CLEARED_STEP[name]); } });
    }
    for (var i = 0; i < steps.length; i += 1) {
      if (wanted.indexOf(steps[i].id) !== -1) { return steps[i].id; }
    }
    return null;
  }

  function defaultDelay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  /* 一次送出的流程。依賴都由外部注入，所以 Node 測試不需要瀏覽器：
     - `http.post(path, body)`、`http.put(path, body)`：回傳 Promise（瀏覽器端是 axios）；
     - `navigate(path)`：導向（瀏覽器端是 `window.location.assign`）；
     - `destination`：成功後要去的位置（已經過 `Auth.safeDestination`）；
     - `delay(ms)`：逾時用的計時器（預設 setTimeout），測試可替換。
     `submit(values)` 回傳 Promise：`'done'`（確認成功並已導向）、`'busy'`（上一次還在處理，沒有送出任何請求）；
     `verify-age` 失敗時拒絕原本的錯誤。處理中旗標在無論成功或失敗後都會恢復。 */
  function createOnboarding(deps) {
    var inFlight = false;
    var delay = deps.delay || defaultDelay;

    function savePreference(value) {
      if (!isString(value) || value === DEFAULT_PREFERENCE || (value !== 'male' && value !== 'female')) {
        return Promise.resolve();
      }
      var request = Promise.resolve()
        .then(function () { return deps.http.put(STORY_PREFERENCE_PATH, { story_preference: value }); })
        .then(function () {}, function () {});                       // 偏好失敗不影響確認結果：之後可在 /account 設定
      var timeout = Promise.resolve(delay(PREFERENCE_TIMEOUT_MS)).then(function () {});
      return Promise.race([request, timeout]);
    }

    function submit(values) {
      if (inFlight) { return Promise.resolve('busy'); }
      inFlight = true;
      return Promise.resolve()
        .then(function () { return deps.http.post(VERIFY_AGE_PATH, Flows.ageBody(values)); })
        .then(function () { return savePreference(values.storyPreference); })
        .then(function () {
          deps.navigate(deps.destination);
          return 'done';
        })
        .finally(function () { inFlight = false; });
    }

    return { submit: submit, isSubmitting: function () { return inFlight; } };
  }

  return {
    VERIFY_AGE_PATH: VERIFY_AGE_PATH,
    STORY_PREFERENCE_PATH: STORY_PREFERENCE_PATH,
    DEFAULT_PREFERENCE: DEFAULT_PREFERENCE,
    PREFERENCE_TIMEOUT_MS: PREFERENCE_TIMEOUT_MS,
    STEP_WELCOME: STEP_WELCOME,
    STEP_AGE: STEP_AGE,
    STEP_PREFERENCE: STEP_PREFERENCE,
    STEPS: STEPS,
    TEXT: TEXT,
    progress: progress,
    nextIndex: nextIndex,
    previousIndex: previousIndex,
    canAdvance: canAdvance,
    stepForFailure: stepForFailure,
    createOnboarding: createOnboarding
  };
});
