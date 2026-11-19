/* 錢包頁 `/wallet` 的純函式與計時器管理（與框架無關，Node 可以直接測試；見 wallet-hub-page 規格與 wallet-hub 的 design D7、D8、D10）。
 *
 * 這個模組只做「資料搬運與對照」，沒有請求、儲存、DOM、主控台；唯一的副作用是 `createTicker` 呼叫注入進來的計時函式：
 * - **分頁與檢視**（D10）：兩個分頁 `free`（免費，預設）與 `topup`（儲值），以及頁內的兩個檢視 `main` 與 `records`（紀錄，沒有獨立網址）。
 *   `selectTab`／`openRecords`／`closeRecords` 回傳新的 `{ tab, view }`（不修改傳入的物件；不認得的值退回預設）；進入紀錄時記住原本的分頁，返回後回到它；
 *   紀錄檢視中不能切換分頁。`tabFromKey(tab, key)`：方向鍵左右（循環）與 Home／End 對應的分頁，其餘按鍵回傳 null（鍵盤操作 `tablist`）。
 * - **頂部餘額** `balanceView(state)`：把全站的錢包狀態庫（`UI.wallet.state`）整理成畫面文字：合計、「付費 0｜贈送 850」、管理員的「不消耗」、載入中與失敗（顯示「—」，
 *   絕不顯示不確定的數字）。**餘額只認非負整數**，型別不對一律當成不知道。
 * - **簽到卡** `checkinCardOf(input)`：依錢包狀態、簽到狀態機的 `finished` 與是否有預覽，決定簽到卡的樣子（`loading`、`failed`、`preview`、`done`、`claimable`、`unavailable`）與說明文字；
 *   `signupGiftText(state)`（註冊贈幣卡：只說明自動發放，沒有數量，管理員改說不發放）與 `freeCapNote(freeCap)`（免費幣上限說明）也在這裡。
 * - **倒數**（D8）：`remainingMs(nextResetAt, nowMs)`（目前時間與下次可簽到時間相減，不是每秒減一，所以背景分頁被節流後恢復仍然正確；時間不合法回傳 null，過了就是 0）、
 *   `formatCountdown(ms)`（`時:分:秒`，向上取整，所以剩 16:43:25.4 顯示 16:43:26，真的歸零才顯示 00:00:00）、`countdownText`。
 *   `shouldReloadAtZero(input)`：倒數歸零時要不要重讀錢包——歸零才讀、進行中不重複、**兩次讀取至少相隔 60 秒**（本機時鐘比伺服器快、重讀後仍不能簽到時，不會無止境地請求）。
 *   `createTicker({ now, setInterval, clearInterval, onTick })`：每秒呼叫 `onTick(現在時間)` 的計時器；`start()` 重複呼叫只有一個計時器、`stop()` 一定清除。
 *   計時器只在「看得到倒數」時啟動；離開簽到卡的檢視與頁面卸載時由呼叫端停止（wallet-hub.js）。
 * - **儲值分頁（示意）**（D9）：`topupViewOf(demo, turnCost)` 把 wallet-demo-packages.js 的資料組成卡片文字（幣數、另贈、約可進行幾輪、價格）；只有 `IS_DEMO === true` 才有卡片，
 *   而且有卡片時一定有「示意，非正式價格」的說明；資料不合格的卡片略過。這個檔案**不寫任何幣數與價格**，數字只在資料模組裡。
 * - **紀錄檢視**（D10）：`entryViewOf`／`entriesPageOf`（帳本一列與一頁的整理：「贈送」「付費」各自的增減量，正數「+」、負數「−」、千分位，0 就是「0」；尚未結算標「處理中」；
 *   形狀不對整頁拒絕）、`mergeEntries`（接在後面並去除重複序號）、`entriesFailureText`（失敗說明）、`recordsViewOf`（loading｜failed｜empty｜list 與載入更多的四種狀態）。
 * - **使用說明** `guideOf(state)`：只陳述已定案的規則，沒有價格、付款方式與退款。
 * 所有文字是純文字，頁面以 {{ }} 插值顯示，絕不使用 v-html。載入順序：無相依（wallet-hub.js 在它之後）。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Wallet = root.Wallet || {};
    Object.keys(api).forEach(function (key) { root.Wallet[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DASH = '—';
  var TABS = ['free', 'topup'];
  var RELOAD_GAP_MS = 60 * 1000;
  var TICK_MS = 1000;

  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function isCount(value) { return typeof value === 'number' && isFinite(value) && value >= 0 && Math.floor(value) === value; }
  function pad(number) { return number < 10 ? '0' + number : String(number); }

  // ---- 分頁與檢視 -------------------------------------------------------------------------------------

  function isTab(name) { return TABS.indexOf(name) !== -1; }

  function normalize(current) {
    var source = isObject(current) ? current : {};
    return { tab: isTab(source.tab) ? source.tab : TABS[0], view: source.view === 'records' ? 'records' : 'main' };
  }

  function initialView() { return { tab: TABS[0], view: 'main' }; }

  /* 切換分頁：只在主檢視有效（紀錄檢視的分頁列是隱藏的）；不認得的名稱不動。 */
  function selectTab(current, name) {
    var view = normalize(current);
    if (view.view !== 'main' || !isTab(name)) { return view; }
    return { tab: name, view: 'main' };
  }

  /* 進入紀錄：記住原本的分頁（tab 不變），分頁列隱藏。 */
  function openRecords(current) { return { tab: normalize(current).tab, view: 'records' }; }

  /* 返回：回到進入紀錄之前的分頁。 */
  function closeRecords(current) { return { tab: normalize(current).tab, view: 'main' }; }

  /* `tablist` 的鍵盤操作：左右方向鍵（循環）、Home、End；其他按鍵回傳 null（不處理）。 */
  function tabFromKey(tab, key) {
    var index = TABS.indexOf(tab);
    if (index === -1) { index = 0; }
    if (key === 'ArrowRight') { return TABS[(index + 1) % TABS.length]; }
    if (key === 'ArrowLeft') { return TABS[(index - 1 + TABS.length) % TABS.length]; }
    if (key === 'Home') { return TABS[0]; }
    if (key === 'End') { return TABS[TABS.length - 1]; }
    return null;
  }

  // ---- 頂部餘額 ---------------------------------------------------------------------------------------

  /* `state` 是錢包狀態庫的 `state`（status、total、free、paid、exempt…）。 */
  function balanceView(state) {
    var source = isObject(state) ? state : {};
    var known = isCount(source.total);
    var exempt = source.exempt === true;
    var failed = source.status === 'failed';
    var aria;
    if (known) { aria = '遊戲幣餘額 ' + source.total + (exempt ? '（不消耗遊戲幣）' : ''); }
    else if (failed) { aria = '遊戲幣餘額無法讀取'; }
    else { aria = '遊戲幣餘額讀取中'; }
    return {
      amountText: known ? String(source.total) : DASH,
      splitText: known && isCount(source.paid) && isCount(source.free) ? '付費 ' + source.paid + '｜贈送 ' + source.free : '',
      exempt: exempt,
      failed: failed,
      loading: source.status !== 'ready' && !failed,
      aria: aria
    };
  }

  // ---- 免費分頁的卡片 ---------------------------------------------------------------------------------

  /* 簽到卡。`input`：{ status（錢包狀態庫）, claimable（錢包說今天能不能簽；布林｜null）, finished（簽到狀態機）, hasPreview（等待玩家選取消或領取）, grant（簽到數） }。 */
  function checkinCardOf(input) {
    var source = isObject(input) ? input : {};
    if (source.status === 'failed') { return { mode: 'failed', headline: '目前無法讀取簽到狀態，請按上方的「重試」。' }; }
    if (source.status !== 'ready') { return { mode: 'loading', headline: '載入中…' }; }
    if (source.hasPreview === true) { return { mode: 'preview', headline: '領取前請先確認。' }; }
    if (source.finished === true || source.claimable === false) { return { mode: 'done', headline: '今天已經簽到過了。' }; }
    if (source.claimable === true) {
      var grant = isCount(source.grant) && source.grant > 0 ? '每日簽到可領取 ' + source.grant + ' 遊戲幣。' : '每日簽到可以領取免費遊戲幣。';
      return { mode: 'claimable', headline: grant };
    }
    return { mode: 'unavailable', headline: '目前無法判斷今天能不能簽到，請稍後再試。' };
  }

  /* 註冊贈幣卡的說明：只說明自動發放，**不寫數量**（錢包 API 沒有這個欄位，畫面不自行寫死）；還沒讀到錢包時是空字串（避免管理員先看到一般玩家的說明）。 */
  function signupGiftText(state) {
    var source = isObject(state) ? state : {};
    if (source.status !== 'ready') { return ''; }
    if (source.exempt === true) { return '管理員帳號不發放註冊贈幣。'; }
    return '完成年齡確認時，系統會自動發放一次註冊贈幣，不需要另外領取。';
  }

  /* 免費幣上限的說明；上限不是正整數時沒有說明（不自行猜數字）。 */
  function freeCapNote(freeCap) {
    if (!isCount(freeCap) || freeCap <= 0) { return ''; }
    return '免費遊戲幣（簽到與贈送）最多持有 ' + freeCap + ' 幣；領取後超過上限的部分會作廢，領取前會先提醒你。';
  }

  // ---- 儲值分頁（示意）--------------------------------------------------------------------------------

  var DEMO_NOTICE = '示意，非正式價格。儲值功能尚未開放，目前無法購買。';
  var NOT_OPEN_NOTICE = '儲值功能尚未開放。';
  var MAX_PACKS = 12;
  var MAX_SAFE = 9007199254740991;
  var MINUS = '−';                                            // U+2212：負號，與「+」在版面上等寬對齊

  function isInt(value) { return typeof value === 'number' && isFinite(value) && Math.floor(value) === value && Math.abs(value) <= MAX_SAFE; }

  /* 非負整數加千分位逗號：1000 → "1,000"（不依賴瀏覽器的語系）。 */
  function groupDigits(value) { return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

  /* 儲值分頁的畫面資料。`demo` 是 wallet-demo-packages.js 匯出的 { IS_DEMO, CURRENCY, PACKAGES }（資料只在那個檔案裡，這裡不寫任何幣數與價格）；`turnCost` 是每輪幣價。
     **只有 `IS_DEMO === true` 才有卡片**，而且有卡片時一定有「示意，非正式價格」的說明（`notice`）；沒有示意資料就只說「尚未開放」，絕不顯示沒有標示的價格。
     每張卡片的資料不合格（幣數不是正整數、贈送不是非負整數、價格不是正整數、id 空白或重複）就略過，最多 12 張。 */
  function topupViewOf(demo, turnCost) {
    var source = isObject(demo) ? demo : {};
    if (source.IS_DEMO !== true || !Array.isArray(source.PACKAGES)) { return { demo: false, notice: NOT_OPEN_NOTICE, cards: [] }; }
    var currency = typeof source.CURRENCY === 'string' ? source.CURRENCY : '';
    var cost = isCount(turnCost) && turnCost > 0 ? turnCost : 0;
    var seen = [];
    var cards = [];
    source.PACKAGES.forEach(function (pack) {
      if (cards.length >= MAX_PACKS || !isObject(pack)) { return; }
      if (typeof pack.id !== 'string' || pack.id === '' || seen.indexOf(pack.id) !== -1) { return; }
      if (!isCount(pack.coins) || pack.coins < 1 || !isCount(pack.bonus) || !isCount(pack.price) || pack.price < 1) { return; }
      seen.push(pack.id);
      var turns = cost > 0 ? Math.floor((pack.coins + pack.bonus) / cost) : 0;
      cards.push({
        id: pack.id,
        coinsText: groupDigits(pack.coins) + ' 幣',
        bonusText: pack.bonus > 0 ? '另贈 ' + groupDigits(pack.bonus) + ' 幣' : '',
        turnsText: turns > 0 ? '約可進行 ' + turns + ' 輪冒險' : '',
        priceText: currency + groupDigits(pack.price)
      });
    });
    return { demo: true, notice: DEMO_NOTICE, cards: cards };
  }

  // ---- 紀錄檢視 ---------------------------------------------------------------------------------------

  /* 增減量的文字：正數帶「+」、負數用「−」、千分位；0 是「0」（管理員的零金額預扣會出現）；不是整數也當成 0（呼叫端已先驗證，這裡只是保底）。 */
  function signedDelta(value) {
    if (!isInt(value) || value === 0) { return '0'; }
    return value > 0 ? '+' + groupDigits(value) : MINUS + groupDigits(-value);
  }

  /* 帳本的一列（`GET /api/wallet/entries` 的一筆）整理成畫面用的形狀；欄位不合格回傳 null。`formatTime(iso)` 由呼叫端注入（台灣時間，例如 Game.formatTaipeiTime）。
     增減量分成「贈送」與「付費」兩個部分，只列出不是 0 的；兩者都是 0 就只有「0」。尚未結算的預扣（`pending`）另標「處理中」。文字都是純文字。 */
  function entryViewOf(raw, formatTime) {
    if (!isObject(raw) || !isCount(raw.seq) || !isInt(raw.free_delta) || !isInt(raw.paid_delta)) { return null; }
    var parts = [];
    if (raw.free_delta !== 0) { parts.push('贈送 ' + signedDelta(raw.free_delta)); }
    if (raw.paid_delta !== 0) { parts.push('付費 ' + signedDelta(raw.paid_delta)); }
    if (parts.length === 0) { parts.push('0'); }
    var label = typeof raw.label === 'string' ? raw.label.trim() : '';
    var time = typeof formatTime === 'function' ? formatTime(raw.created_at) : '';
    return { key: raw.seq, label: label === '' ? '其他' : label, pending: raw.pending === true, timeText: typeof time === 'string' ? time : '', parts: parts };
  }

  /* 一頁帳本：{ items, nextBefore }；回應的形狀不對（不是陣列、任何一列不合格）整頁回傳 null——寧可顯示錯誤，也不顯示缺了幾筆的帳本。
     `nextBefore` 只認非負整數，而且這一頁沒有任何列時一律是 null（不會無止境地要下一頁）。 */
  function entriesPageOf(response, formatTime) {
    if (!isObject(response) || !Array.isArray(response.entries)) { return null; }
    var items = [];
    for (var index = 0; index < response.entries.length; index += 1) {
      var item = entryViewOf(response.entries[index], formatTime);
      if (item === null) { return null; }
      items.push(item);
    }
    return { items: items, nextBefore: items.length > 0 && isCount(response.next_before) ? response.next_before : null };
  }

  /* 把新的一頁接在後面；同一個序號只留一列（兩次請求的範圍重疊也不會重複顯示）。不修改傳入的陣列。 */
  function mergeEntries(current, incoming) {
    var merged = Array.isArray(current) ? current.slice() : [];
    var known = {};
    merged.forEach(function (item) { known[item.key] = true; });
    (Array.isArray(incoming) ? incoming : []).forEach(function (item) {
      if (isObject(item) && isCount(item.key) && known[item.key] !== true) { known[item.key] = true; merged.push(item); }
    });
    return merged;
  }

  /* 紀錄清單讀取失敗的說明（`info` 是 UI.walletApi 整理過的失敗物件）。 */
  function entriesFailureText(info) {
    var source = isObject(info) ? info : {};
    if (source.kind === 'network') { return '目前無法連線，請檢查網路後再試。'; }
    if (source.kind === 'timeout') { return '連線逾時，請稍後再試。'; }
    if (source.status === 429 || source.status === 502 || source.status === 503 || source.status === 504) { return '系統目前較忙碌，請稍後再試。'; }
    return '紀錄暫時無法讀取，請稍後再試。';
  }

  /* 紀錄檢視的畫面資料。`input`：{ phase（idle｜loading｜ready｜failed：第一頁的狀態）, items, error（第一頁失敗的說明）, hasMore, moreBusy, moreError }。
     mode：loading（還沒有結果）｜failed（第一頁失敗，沒有清單）｜empty（讀到了但沒有紀錄）｜list（有清單）。
     moreMode（只有 list 時有意義）：more（可以載入更多）｜loading（正在載入）｜error（載入更多失敗，已載入的清單仍保留）｜end（沒有更舊的了）。 */
  function recordsViewOf(input) {
    var source = isObject(input) ? input : {};
    var items = Array.isArray(source.items) ? source.items : [];
    var base = { mode: 'loading', items: [], errorText: '', moreMode: 'end' };
    if (source.phase === 'failed') { base.mode = 'failed'; base.errorText = typeof source.error === 'string' ? source.error : ''; return base; }
    if (source.phase !== 'ready') { return base; }
    if (items.length === 0) { base.mode = 'empty'; return base; }
    base.mode = 'list';
    base.items = items;
    if (source.moreBusy === true) { base.moreMode = 'loading'; }
    else if (typeof source.moreError === 'string' && source.moreError !== '') { base.moreMode = 'error'; base.errorText = source.moreError; }
    else if (source.hasMore === true) { base.moreMode = 'more'; }
    return base;
  }

  // ---- 使用說明 ---------------------------------------------------------------------------------------

  /* 使用說明的每一行：只陳述已定案的規則（每輪幣數、先扣贈送幣再扣付費幣、不過期、贈送幣上限），沒有價格、付款方式、退款與訂閱。
     每輪幣數與上限取自錢包（`state` 是全站的錢包狀態庫的 `state`），還沒讀到時不寫數字；管理員改說不消耗。 */
  function guideOf(state) {
    var source = isObject(state) ? state : {};
    var ready = source.status === 'ready';
    var lines = [];
    if (ready && source.exempt === true) { lines.push('管理員帳號進行冒險時不消耗遊戲幣。'); }
    else if (ready && isCount(source.turnCost) && source.turnCost > 0) { lines.push('每一輪冒險消耗 ' + source.turnCost + ' 遊戲幣。'); }
    lines.push('扣款時先扣贈送幣，再扣付費幣。');
    lines.push('遊戲幣不會過期，贈送幣與付費幣都一樣。');
    if (ready && isCount(source.freeCap) && source.freeCap > 0) { lines.push('贈送幣有持有上限，目前是 ' + source.freeCap + ' 幣；領取時超過上限的部分會作廢。'); }
    return lines;
  }

  // ---- 倒數 -------------------------------------------------------------------------------------------

  /* 距離下次可簽到的毫秒數：`nextResetAt` 是伺服器給的 UTC ISO 字串，`nowMs` 是目前時間；時間不合法回傳 null，已經過了回傳 0。 */
  function remainingMs(nextResetAt, nowMs) {
    if (typeof nextResetAt !== 'string' || typeof nowMs !== 'number' || !isFinite(nowMs)) { return null; }
    var target = Date.parse(nextResetAt);
    if (!isFinite(target)) { return null; }
    return Math.max(0, target - nowMs);
  }

  /* `時:分:秒`（各兩位）。向上取整到秒：還剩 0.4 秒仍顯示 00:00:01，真的歸零才顯示 00:00:00。 */
  function formatCountdown(ms) {
    var total = typeof ms === 'number' && isFinite(ms) && ms > 0 ? Math.ceil(ms / 1000) : 0;
    return pad(Math.floor(total / 3600)) + ':' + pad(Math.floor((total % 3600) / 60)) + ':' + pad(total % 60);
  }

  /* 倒數的文字；時間不合法時是空字串（簽到卡不顯示倒數）。 */
  function countdownText(nextResetAt, nowMs) {
    var remaining = remainingMs(nextResetAt, nowMs);
    return remaining === null ? '' : formatCountdown(remaining);
  }

  /* 倒數歸零時要不要重讀錢包。`input`：{ remaining（remainingMs 的結果）, claimable（錢包說的）, reloading（正在重讀）, lastReloadAt（上次開始重讀的時間，沒有就是 null）, nowMs }。
     只有「剛好歸零、沒有正在重讀、錢包還沒說可以簽、距離上次重讀已經 60 秒以上」才重讀。 */
  function shouldReloadAtZero(input) {
    var source = isObject(input) ? input : {};
    if (source.remaining !== 0 || source.reloading === true || source.claimable === true) { return false; }
    if (source.lastReloadAt === null || source.lastReloadAt === undefined) { return true; }
    return typeof source.lastReloadAt === 'number' && typeof source.nowMs === 'number' && source.nowMs - source.lastReloadAt >= RELOAD_GAP_MS;
  }

  /* 每秒呼叫 `onTick(現在時間)` 的計時器。`deps`：{ now(), setInterval(fn, ms), clearInterval(handle), onTick(nowMs) }。
     `start()` 先立即呼叫一次（畫面不必等一秒才有數字），重複呼叫不會多開計時器；`stop()` 清除計時器（沒有在跑就什麼都不做）。 */
  function createTicker(deps) {
    var handle = null;
    var running = false;
    function tick() { deps.onTick(deps.now()); }
    function start() {
      if (running) { return; }
      running = true;
      handle = deps.setInterval(tick, TICK_MS);
      tick();
    }
    function stop() {
      if (!running) { return; }
      running = false;
      deps.clearInterval(handle);
      handle = null;
    }
    return { start: start, stop: stop, isRunning: function () { return running; } };
  }

  return {
    TABS: TABS, RELOAD_GAP_MS: RELOAD_GAP_MS, TICK_MS: TICK_MS,
    isTab: isTab, initialView: initialView, selectTab: selectTab, openRecords: openRecords, closeRecords: closeRecords, tabFromKey: tabFromKey,
    balanceView: balanceView, checkinCardOf: checkinCardOf, signupGiftText: signupGiftText, freeCapNote: freeCapNote,
    DEMO_NOTICE: DEMO_NOTICE, NOT_OPEN_NOTICE: NOT_OPEN_NOTICE, MAX_PACKS: MAX_PACKS,
    groupDigits: groupDigits, topupViewOf: topupViewOf,
    signedDelta: signedDelta, entryViewOf: entryViewOf, entriesPageOf: entriesPageOf, mergeEntries: mergeEntries, entriesFailureText: entriesFailureText, recordsViewOf: recordsViewOf,
    guideOf: guideOf,
    remainingMs: remainingMs, formatCountdown: formatCountdown, countdownText: countdownText, shouldReloadAtZero: shouldReloadAtZero, createTicker: createTicker
  };
});
