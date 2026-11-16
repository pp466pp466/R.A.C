/* RPG 入口畫面的顯示對照與路徑組裝（純函式與純資料，與框架無關，Node 可以直接測試；見 rpg-entry-pages 規格與 rpg-entry-ui 的 design D5）。
 *
 * 後端只回列舉值（`safe`、`male`、`native`…），顯示用的繁體中文集中在這裡，沿用 `script-draft.js` 的做法。
 * - 所有 `*Label(value)` 對**未知值**一律退回固定的預設文字，不把原始值顯示給玩家（也避免把 `__proto__`、`constructor` 這類鍵當成對照）。
 * - `gateNotice(gate)`：帳號閘門（`AccessPolicy.chat_gate` 的結果）的提示文字與連結；未知的非空代碼一律當成「不可開始」（失敗時關閉），不放行。
 * - `contentModeNotice(mode)`：說明「為什麼沒看到某些劇本」。只有說明，沒有任何改變模式的動作：成年玩家的模式較保守時只提醒可到帳號設定調整，
 *   不自動調高；未滿成年門檻者固定為安全模式，只說明原因。
 * - `startPath(id)`、`sessionPath(id)`：站內路徑，識別碼經 encodeURIComponent。
 * - `formatTaipeiTime(iso)`：伺服器的 UTC 時間（`2026-10-27T12:00:00Z`）轉成台灣時間 `YYYY-MM-DD HH:mm`；台灣沒有日光節約時間，固定加 8 小時，不依賴瀏覽器的時區。
 * 遊戲主畫面（rpg-game-ui）另加：回合與串流結束狀態的名稱、轉場與中斷說明、串流 `error` 事件的訊息、歷史裡未完成回合的那一行說明、
 * 記事簿與簽到用的固定文字。這些文字**不含任何玩家文字**；不認得的值一律退回固定文字（失敗時關閉：不當成有內容的回合）。
 * 重新生成與編輯最後一輪（rpg-turn-revision）另加：五個回饋分類的名稱、按鈕旁的免費次數與幣價說明、兩個 Modal 的費用說明、不能修訂的各種原因與結果通知
 * （一律說明「已保留原本的回應」）；同樣不含任何玩家文字（行動、理由、劇情都不會被帶進來）。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    Object.keys(api).forEach(function (key) { root.Game[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MEMBER_LABELS = { free: '一般', plus: '進階', vip: '尊榮' };
  var TIER_LABELS = { safe: '安全模式', standard: '一般模式', adult: '成人模式' };
  var AUDIENCE_LABELS = { all: '全讀者', male: '男頻', female: '女頻' };
  var ORIGIN_LABELS = { native: '本地人', transmigrator: '穿越者' };
  var STATUS_LABELS = { active: '進行中', ended: '已完結' };
  /* 審查類別：後端只回固定的列舉識別字（不含內容的位置或文字）。前四項是會被直接拒絕的紅線，後四項只會進人工。 */
  var CATEGORY_LABELS = {
    minor_sexual: '涉及未成年角色的性內容',
    sexual_violence: '性暴力',
    non_consensual: '非自願的性情節',
    real_person_sexual: '以真人名義的性內容',
    minor_suspected: '疑似涉及未成年角色',
    real_person_suspected: '疑似涉及真人',
    tier_mismatch: '內容與宣告的內容模式不符',
    other_concern: '其他疑慮'
  };

  var DEFAULTS = {
    member: '一般', tier: '未標示', audience: '全讀者', origin: '本地人', status: '進行中', category: '其他疑慮'
  };

  var ACCOUNT_PATH = '/account';
  var ACCOUNT_LINK_TEXT = '前往帳號設定';
  var GATE_NOTICES = {
    age_confirmation_required: { text: '請先到帳號設定填寫生日並選擇年齡區間，才能開始冒險。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT },
    age_restricted: { text: '您的年齡目前不符合使用這項功能的規定，所以無法開始冒險。', link: null, linkText: '' },
    forbidden: { text: '您的帳號目前無法開始冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT }
  };
  var GATE_FALLBACK = { text: '目前無法開始冒險，請到帳號設定確認帳號狀態。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT };

  /* ---- 遊戲主畫面（rpg-game-ui）：串流結束狀態、轉場與中斷說明、記事簿與簽到的固定文字 ----
     這些文字只來自這裡，**不含任何玩家文字**（行動、劇情、記事簿內容、角色名都不會被帶進來）；數字只有字數、幣數這類整數。
     與後端列舉的鍵對照由 tests/test_game_pages.py 守住（回合狀態、串流錯誤代碼）。 */

  /* 回合的狀態（`turn_view.status`）。 */
  var TURN_STATUS_LABELS = { streaming: '進行中', complete: '已完成', blocked: '已轉場', interrupted: '已中斷' };
  /* `done` 事件的結束狀態。 */
  var DONE_STATUS_LABELS = { complete: '已完成', blocked: '劇情轉了個彎', ended: '故事已完結' };
  /* 串流客戶端的結果（turn-stream.js 的 outcome）。 */
  var STREAM_OUTCOME_LABELS = {
    done: '已完成', error_event: '生成失敗', interrupted: '連線中斷', timeout_idle: '等待逾時', timeout_total: '總時間逾時', aborted: '已取消', rejected: '無法開始'
  };
  var SYNC_REMINDER = '這一輪可能已在伺服器完成，已為你重新同步；遊戲幣與歷史以伺服器為準。';
  /* 連線在 `done` 之前結束的說明：前端中止不代表後端中止，所以每一則都提醒「可能已在伺服器完成」。 */
  var STREAM_OUTCOME_NOTICES = {
    interrupted: '連線在這一輪完成之前中斷了。' + SYNC_REMINDER,
    timeout_idle: '等了太久都沒有收到伺服器的回應，已中止這一輪。' + SYNC_REMINDER,
    timeout_total: '這一輪花的時間比預期久，已中止。' + SYNC_REMINDER,
    aborted: '已取消這一輪。' + SYNC_REMINDER
  };
  /* 歷史裡沒有完成的回合只顯示一行說明（不含任何部分內容）。 */
  var HISTORY_LINES = {
    blocked: '劇情在這裡轉了個方向，這一輪沒有留下內容。',
    interrupted: '這一輪在完成之前中斷了，沒有留下內容。',
    streaming: '另一個回合正在進行中，完成後會出現在這裡。'
  };
  /* 串流中的 `error` 事件（後端 `ERROR_MESSAGES` 的代碼）。 */
  var STREAM_ERROR_MESSAGES = {
    generation_timeout: '故事生成逾時了，這一輪沒有扣除遊戲幣，請再試一次。',
    generation_failed: '故事暫時無法生成，這一輪沒有扣除遊戲幣，請稍後再試。',
    turn_superseded: '這個回合已經被新的嘗試取代，已為你重新同步。',
    opening_superseded: '故事的開場已經由另一次嘗試完成，已為你重新同步。',
    internal_error: '發生了預期之外的問題，這一輪沒有扣除遊戲幣，請稍後再試。'
  };
  var STREAM_ERROR_FALLBACK = '故事暫時無法生成，這一輪沒有扣除遊戲幣，請稍後再試。';
  var STREAM_UNKNOWN_NOTICE = '這一輪沒有順利完成。' + SYNC_REMINDER;
  var BLOCKED_HINT = '這一輪沒有繼續下去，沒有扣除遊戲幣。換個行動再試試看。';

  var NOTEBOOK_TEXTS = {
    empty: '這本記事簿還是空的。每完成 10 輪，AI 會把故事的重點記在這裡。',
    full: '記事簿已滿：之後的設定不再被記住，AI 可能會遺忘先前的內容。可以整理內容，或升級會員以取得更大的容量。',
    full_edit: '目前的長度已超過容量，須一次刪到容量以內才能儲存，AI 才會恢復更新。',
    reserve: '預備空間只供 AI 寫入，你自己編輯時不能使用。',
    conflict: 'AI 或另一個裝置剛更新了記事簿，已顯示最新內容；你剛才編輯的內容還保留著，可以對照後再儲存。',
    ended: '這個存檔已經完結，記事簿成為唯讀。',
    network: '連線不穩，記事簿沒有儲存；你編輯的內容還在，請稍後再試。',
    load_network: '連線不穩，讀不到記事簿，請稍後再試。',
    unchanged: '還沒有修改內容，沒有需要儲存的變更。',
    conflict_toast: '記事簿剛在別處更新過，請看編輯框下方的最新內容。',
    refreshed: '另一個分頁或裝置剛更新了記事簿，編輯框已換成最新內容。',
    not_found:'找不到這個存檔，它可能已被刪除。',
    rejected: '記事簿的內容未通過內容規範，無法儲存。請修改後再試。',
    invalid: '記事簿的內容有問題，請檢查後再儲存。',
    saved: '記事簿已儲存。',
    unavailable: '記事簿暫時無法使用，請稍後再試。'
  };
  var NOTEBOOK_FALLBACK = '記事簿暫時無法使用，請稍後再試。';

  var CHECKIN_TEXTS = {
    done: '簽到成功，今天的遊戲幣已入帳。',
    already_default: '今天已經簽到過了，明天 00:00（台灣時間）後可以再簽到。',
    unavailable: '簽到暫時無法使用，請稍後再試。'
  };

  /* ---- 重新生成與編輯最後一輪（rpg-turn-revision）：分類、按鈕旁的說明、Modal 與結果通知的固定文字 ----
     這些文字**不含任何玩家文字**（行動、理由、劇情都不會被帶進來）；數字只有次數、幣數、字數這類整數。
     分類的鍵與後端 `REGEN_CATEGORIES` 一致（由 tests/test_game_pages.py 比對）；順序與是否啟用以頁面初始狀態的 `revision_limits.categories` 為準。 */
  var REVISION_CATEGORY_LABELS = {
    setting_mismatch: '不符合設定',
    repetitive_or_dull: '劇情重複或無趣',
    typo_or_quality: '錯字或文字品質',
    blocked_transition: '被轉場擋掉',
    other: '其他'
  };
  var REVISION_CATEGORY_FALLBACK = '其他';
  var REVISION_TEXTS = {
    kept: '已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。',
    kept_blocked: '新的內容沒有通過內容檢查，劇情轉了個彎。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。',
    kept_failed: '新的內容沒有生成成功。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。',
    synced: '伺服器已經完成這次的修改，畫面已換成伺服器上最新的內容。',
    processing: '伺服器還在處理這次的修改；完成後會以伺服器上的內容為準，請稍後重新整理頁面確認。',
    uncertain: '連線在這次修改完成之前中斷了。伺服器可能已經完成，畫面會以伺服器上的內容為準。',
    incomplete: '上次的重新生成或編輯沒有完成，原本的回應已保留。',
    unavailable: '目前無法重新生成或編輯這一輪，請稍後再試。',
    hint_offline: '需要連線才能重新生成或編輯。',
    hint_sending: '新的一輪進行中，完成後才能重新生成或編輯。',
    hint_revising: '正在重新生成或編輯這一輪…',
    hint_inflight: '有一個行動還沒有確認完成，請先在時間軸裡選擇「重送」或「放棄」。',
    hint_in_progress: '故事正在處理中，請稍候再試。',
    hint_no_snapshot: '這一輪太久以前建立，已經不能重新生成或編輯。',
    hint_unknown: '目前無法重新生成或編輯這一輪。',
    category_missing: '請選擇一個分類。',
    edit_unchanged: '行動和原本的相同，請修改後再送出。',
    edit_empty: '請輸入你的行動。'
  };
  var REVISION_NOT_REVISABLE_MESSAGES = {
    ended: '這個存檔已經完結，最後一輪不能再重新生成或編輯。',
    not_last: '只有最後一輪可以重新生成或編輯。',
    in_progress: '故事正在處理中，請稍候再試。',
    no_snapshot: '這一輪太久以前建立，已經不能重新生成或編輯。'
  };
  var REVISION_NOT_REVISABLE_FALLBACK = '這一輪目前不能重新生成或編輯。';
  var REVISION_CONFLICT_MESSAGE = '這一輪已經在另一個地方被修改過，已為你顯示最新的內容。';
  /* 修訂串流中的 `error` 事件：全部說明「保留原本的回應」；不認得的代碼退回通用文字（不顯示伺服器送來的原文）。 */
  var REVISION_ERROR_MESSAGES = {
    generation_timeout: '新的內容生成逾時了。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。',
    generation_failed: '新的內容暫時無法生成。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。',
    turn_superseded: '這次的修改已經被新的嘗試取代，已為你重新同步。',
    internal_error: '發生了預期之外的問題。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。'
  };
  var REVISION_ERROR_FALLBACK = '新的內容暫時無法生成。已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數。';

  function has(map, key) { return typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key); }
  function labelOf(map, value, fallback) { return has(map, value) ? map[value] : fallback; }
  function copy(notice) { return { text: notice.text, link: notice.link, linkText: notice.linkText }; }

  function memberLabel(value) { return labelOf(MEMBER_LABELS, value, DEFAULTS.member); }
  function tierLabel(value) { return labelOf(TIER_LABELS, value, DEFAULTS.tier); }
  function audienceLabel(value) { return labelOf(AUDIENCE_LABELS, value, DEFAULTS.audience); }
  function originLabel(value) { return labelOf(ORIGIN_LABELS, value, DEFAULTS.origin); }
  function statusLabel(value) { return labelOf(STATUS_LABELS, value, DEFAULTS.status); }
  function categoryLabel(value) { return labelOf(CATEGORY_LABELS, value, DEFAULTS.category); }

  /* 閘門為空（可使用）回傳 null；否則回傳 { text, link, linkText }（link 可為 null，例如年齡不符時補填也沒有用）。 */
  function gateNotice(gate) {
    if (typeof gate !== 'string' || gate === '') { return null; }
    return copy(has(GATE_NOTICES, gate) ? GATE_NOTICES[gate] : GATE_FALLBACK);
  }

  /* `mode` 是目錄回應的 `content_mode`（{ effective, is_adult }）。沒有需要說明時回傳 null。 */
  function contentModeNotice(mode) {
    if (!mode || typeof mode !== 'object') { return null; }
    if (mode.is_adult === false) {
      return { text: '未滿 18 歲固定使用安全模式，所以部分劇本不會顯示；滿 18 歲後這項限制會自動解除。', link: null, linkText: '' };
    }
    if (mode.effective === 'safe') {
      return { text: '您目前使用安全模式，所以部分劇本不會顯示。想調整內容模式，可以到帳號設定。', link: ACCOUNT_PATH, linkText: ACCOUNT_LINK_TEXT };
    }
    return null;
  }

  function startPath(versionId) { return '/game/start/' + encodeURIComponent(versionId); }
  function sessionPath(sessionId) { return '/game/' + encodeURIComponent(sessionId); }

  function pad(number) { return number < 10 ? '0' + number : String(number); }

  function formatTaipeiTime(iso) {
    if (typeof iso !== 'string') { return ''; }
    var ms = Date.parse(iso);
    if (!isFinite(ms)) { return ''; }
    var date = new Date(ms + 8 * 60 * 60 * 1000);
    return date.getUTCFullYear() + '-' + pad(date.getUTCMonth() + 1) + '-' + pad(date.getUTCDate()) + ' ' + pad(date.getUTCHours()) + ':' + pad(date.getUTCMinutes());
  }

  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }

  /* 回合狀態的名稱；不認得的值當成「已中斷」（失敗時關閉：不把它當成有內容的回合）。 */
  function turnStatusLabel(value) { return labelOf(TURN_STATUS_LABELS, value, TURN_STATUS_LABELS.interrupted); }
  function doneStatusLabel(value) { return labelOf(DONE_STATUS_LABELS, value, '這一輪已結束'); }
  function streamOutcomeLabel(value) { return labelOf(STREAM_OUTCOME_LABELS, value, '未完成'); }

  /* 串流結束時對玩家的說明：完成（`done`）沒有說明；連線中斷、逾時、取消各有一則（都提醒「可能已在伺服器完成」）；其他一律是通用說明。 */
  function streamOutcomeNotice(outcome) {
    if (outcome === 'done') { return ''; }
    return labelOf(STREAM_OUTCOME_NOTICES, outcome, STREAM_UNKNOWN_NOTICE);
  }

  /* 歷史裡沒有完成的回合那一行說明；不認得的狀態用「中斷」的那一行（不顯示任何部分內容）。 */
  function historyLine(status) { return labelOf(HISTORY_LINES, status, HISTORY_LINES.interrupted); }

  /* `error` 事件的訊息：只認後端列舉的代碼，不認得的代碼退回通用文字（不顯示伺服器送來的原文）。 */
  function streamErrorMessage(code) { return labelOf(STREAM_ERROR_MESSAGES, code, STREAM_ERROR_FALLBACK); }

  function blockedHint() { return BLOCKED_HINT; }

  function notebookText(key) { return labelOf(NOTEBOOK_TEXTS, key, NOTEBOOK_FALLBACK); }

  /* 用量「1800 / 2000」；任一數字不是非負整數時顯示「—」。 */
  function notebookUsage(length, capacity) { return isCount(length) && isCount(capacity) ? length + ' / ' + capacity : '—'; }

  function notebookTooLongNotice(length, capacity) {
    if (!isCount(length) || !isCount(capacity)) { return '記事簿超過容量，請刪減到容量以內再儲存。'; }
    return '記事簿超過容量（' + length + ' / ' + capacity + ' 字），請刪減到容量以內再儲存。';
  }

  /* 預備空間的說明：有預備空間才有（整數大於 0），說明它只供 AI 寫入。 */
  function notebookReserveNote(reserve) {
    if (!isCount(reserve) || reserve <= 0) { return ''; }
    return '預備空間 ' + reserve + ' 字。' + NOTEBOOK_TEXTS.reserve;
  }

  /* 編輯時超過容量：超出的字數（整數大於 0）；其他情況回傳空字串。 */
  function notebookOverNote(overBy) {
    if (!isCount(overBy) || overBy <= 0) { return ''; }
    return '超出容量 ' + overBy + ' 字，須刪到容量以內才能儲存。';
  }

  function checkinText(key) { return labelOf(CHECKIN_TEXTS, key, CHECKIN_TEXTS.unavailable); }

  /* 簽到後免費幣會超過上限：預覽會入帳多少、會作廢多少（整數）。 */
  function checkinOverflowNotice(gain, lose) {
    if (!isCount(gain) || !isCount(lose)) { return '領取後免費遊戲幣會超過上限，超出的部分將作廢。要繼續領取嗎？'; }
    return '領取後免費遊戲幣會超過上限：會入帳 ' + gain + ' 幣，超出上限的 ' + lose + ' 幣將作廢。要繼續領取嗎？';
  }

  /* 今天已簽到：有下次可簽到的時間（UTC 的 ISO 字串）就轉成台灣時間顯示，沒有就用固定說明。 */
  function checkinAlreadyNotice(nextResetAt) {
    var when = formatTaipeiTime(nextResetAt);
    return when ? '今天已經簽到過了，下次可簽到的時間是 ' + when + '（台灣時間）。' : CHECKIN_TEXTS.already_default;
  }

  /* 重新生成的分類名稱；不認得的值退回「其他」（不顯示原始值）。 */
  function revisionCategoryLabel(value) { return labelOf(REVISION_CATEGORY_LABELS, value, REVISION_CATEGORY_FALLBACK); }

  /* 修訂用的固定文字；不認得的鍵退回通用說明。 */
  function revisionText(key) { return labelOf(REVISION_TEXTS, key, REVISION_TEXTS.unavailable); }

  /* 「重新生成」與「編輯」旁的說明（兩者共用同一組免費次數，2026-11-16 規則修訂）：還有免費次數顯示剩餘次數，用完顯示幣價；兩者都不確定時不顯示（空字串）。 */
  function revisionRegenerateNote(freeLeft, cost) {
    if (isCount(freeLeft) && freeLeft > 0) { return '免費還剩 ' + freeLeft + ' 次'; }
    if (isCount(cost) && cost > 0) { return cost + ' 幣'; }
    return '';
  }

  /* 「編輯」旁的說明：與「重新生成」旁相同（共用免費次數，用完顯示一輪的幣價）。 */
  function revisionEditNote(freeLeft, cost) { return revisionRegenerateNote(freeLeft, cost); }

  /* 重新生成 Modal 的費用說明（免費或會消耗多少、目前餘額）。數字不確定的部分不寫出來。 */
  function revisionRegenerateCostLine(freeLeft, cost, balance) {
    if (isCount(freeLeft) && freeLeft > 0) { return '這次重新生成不用遊戲幣（免費還剩 ' + freeLeft + ' 次）。'; }
    var price = isCount(cost) && cost > 0 ? '這次重新生成會消耗 ' + cost + ' 遊戲幣' : '這次重新生成會消耗遊戲幣';
    return price + (isCount(balance) ? '（目前餘額 ' + balance + ' 幣）。' : '。');
  }

  /* 編輯 Modal 的費用說明（與重新生成共用免費次數）：還有免費次數時只說明免費（不提餘額，也不要求餘額）；
     需要付費時提醒會消耗遊戲幣、原本那輪已扣的幣不退，並顯示費用與目前餘額；餘額不足時改說明不足並指向簽到。數字不確定的部分不寫出來。 */
  function revisionEditCostLine(freeLeft, cost, balance, insufficient) {
    if (isCount(freeLeft) && freeLeft > 0) { return '這次編輯免費（免費還剩 ' + freeLeft + ' 次）。'; }
    var price = isCount(cost) && cost > 0 ? cost + ' 幣' : '遊戲幣';
    if (insufficient) {
      return '遊戲幣不足' + (isCount(balance) ? '：目前餘額 ' + balance + ' 幣，' : '，') + '這次編輯需要 ' + price + '。可以先簽到領取免費遊戲幣。';
    }
    return '這次編輯會消耗' + (isCount(cost) && cost > 0 ? ' ' + price : price) + '，原本那輪已扣的幣不退' + (isCount(balance) ? '（目前餘額 ' + balance + ' 幣）。' : '。');
  }

  /* 不能重新生成或編輯的原因（`last_turn.reason` 的列舉）；不認得的退回通用說明。 */
  function revisionNotRevisableMessage(reason) { return labelOf(REVISION_NOT_REVISABLE_MESSAGES, reason, REVISION_NOT_REVISABLE_FALLBACK); }

  function revisionErrorMessage(code) { return labelOf(REVISION_ERROR_MESSAGES, code, REVISION_ERROR_FALLBACK); }

  /* 理由的字數規則說明（例如「請簡短說明原因，至少 5 字、最多 50 字。」）；範圍不明時只說簡短說明。數字來自頁面初始狀態，這裡不寫死。 */
  function revisionReasonRule(min, max) {
    if (isCount(min) && min > 0 && isCount(max) && max >= min) { return '請簡短說明原因，至少 ' + min + ' 字、最多 ' + max + ' 字。'; }
    return '請簡短說明原因。';
  }

  /* 理由的字數提示（例如「12 / 50」）。 */
  function revisionReasonCount(count, max) { return isCount(count) ? (isCount(max) && max > 0 ? count + ' / ' + max : String(count)) : '—'; }

  return {
    REVISION_CATEGORY_LABELS: REVISION_CATEGORY_LABELS,
    REVISION_TEXTS: REVISION_TEXTS,
    REVISION_NOT_REVISABLE_MESSAGES: REVISION_NOT_REVISABLE_MESSAGES,
    REVISION_ERROR_MESSAGES: REVISION_ERROR_MESSAGES,
    REVISION_CONFLICT_MESSAGE: REVISION_CONFLICT_MESSAGE,
    revisionCategoryLabel: revisionCategoryLabel,
    revisionText: revisionText,
    revisionRegenerateNote: revisionRegenerateNote,
    revisionEditNote: revisionEditNote,
    revisionRegenerateCostLine: revisionRegenerateCostLine,
    revisionEditCostLine: revisionEditCostLine,
    revisionNotRevisableMessage: revisionNotRevisableMessage,
    revisionErrorMessage: revisionErrorMessage,
    revisionReasonCount: revisionReasonCount,
    revisionReasonRule: revisionReasonRule,
    TURN_STATUS_LABELS: TURN_STATUS_LABELS,
    DONE_STATUS_LABELS: DONE_STATUS_LABELS,
    STREAM_OUTCOME_LABELS: STREAM_OUTCOME_LABELS,
    STREAM_OUTCOME_NOTICES: STREAM_OUTCOME_NOTICES,
    HISTORY_LINES: HISTORY_LINES,
    STREAM_ERROR_MESSAGES: STREAM_ERROR_MESSAGES,
    NOTEBOOK_TEXTS: NOTEBOOK_TEXTS,
    CHECKIN_TEXTS: CHECKIN_TEXTS,
    turnStatusLabel: turnStatusLabel,
    doneStatusLabel: doneStatusLabel,
    streamOutcomeLabel: streamOutcomeLabel,
    streamOutcomeNotice: streamOutcomeNotice,
    historyLine: historyLine,
    streamErrorMessage: streamErrorMessage,
    blockedHint: blockedHint,
    notebookText: notebookText,
    notebookUsage: notebookUsage,
    notebookTooLongNotice: notebookTooLongNotice,
    notebookReserveNote: notebookReserveNote,
    notebookOverNote: notebookOverNote,
    checkinText: checkinText,
    checkinOverflowNotice: checkinOverflowNotice,
    checkinAlreadyNotice: checkinAlreadyNotice,
    MEMBER_LABELS: MEMBER_LABELS,
    TIER_LABELS: TIER_LABELS,
    AUDIENCE_LABELS: AUDIENCE_LABELS,
    ORIGIN_LABELS: ORIGIN_LABELS,
    STATUS_LABELS: STATUS_LABELS,
    CATEGORY_LABELS: CATEGORY_LABELS,
    memberLabel: memberLabel,
    tierLabel: tierLabel,
    audienceLabel: audienceLabel,
    originLabel: originLabel,
    statusLabel: statusLabel,
    categoryLabel: categoryLabel,
    gateNotice: gateNotice,
    contentModeNotice: contentModeNotice,
    startPath: startPath,
    sessionPath: sessionPath,
    formatTaipeiTime: formatTaipeiTime
  };
});
