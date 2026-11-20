/* 公開目錄的搜尋、篩選與游標分頁的狀態（純函式，與框架無關，Node 可以直接測試；見 rpg-script-catalog 規格「目錄畫面的搜尋與篩選」與 rpg-script-publishing 的 design D6、D7）。
 *
 * 每個函式都對**呼叫端給的狀態物件**操作（入口頁傳進 `Vue.reactive(blank())`，測試傳普通物件），本檔不碰 Vue、DOM、axios 與任何瀏覽器儲存：
 * 取得資料的函式（`fetchPage`、`fetchTags`）由呼叫端傳入（入口頁傳 `Game.api.catalog`、`Game.api.catalogTags`），所以請求序號與接續邏輯可以在 Node 完整驗證。
 *
 * 狀態（`blank()`）：
 *   items、hasMore、nextCursor、loading、loaded、error   目前已載入的卡片與分頁狀態（`loaded` 為真才算「載完第一頁」，空清單的說明才會出現）
 *   q、tag、audience                                      **目前套用中**的條件（搜尋框裡還沒按 Enter 的文字不在這裡，是頁面自己的暫存）
 *   seq                                                   請求序號：條件改變（`reset`）就加一
 *   tags、tagsLoaded、tagsLoading                         常用標籤（只是方便點選的晶片，載入失敗就不顯示，不影響搜尋）
 *
 * 規則：
 * - **條件改變即清空並從第一頁重載**（`reset`）：清掉清單、游標與錯誤，序號加一，載入中的旗標歸零，讓新請求可以立刻送出。
 * - **請求序號避免過時回應覆蓋**：`load` 在送出時記下序號，回應抵達時序號已變就整個丟掉（不改清單、不改錯誤、不放開新請求的載入旗標）。
 * - **載入更多沿用條件與游標**（`requestOptions`）：請求帶目前的 `q`、`tag`、`audience` 與 `nextCursor`；沒有下一頁時 `nextCursor` 為空字串。
 *   `hasMore` 要同時有 `has_more: true`、非空的 `next_cursor` 與這一頁至少一筆才為真（壞回應不會造成無止境的載入）。
 * - **失敗保留已載入的內容**：錯誤只設 `error`，清單、游標與 `loaded` 不動，重試（再呼叫 `load`）取的是同一頁；取消（`canceled`）不顯示錯誤。
 * - 條件的驗證與正規化以伺服器為準（`catalog_query`）；這裡只做不會改變語意的整理（字串、去頭尾空白、頻道限三個值）。
 * 不寫任何瀏覽器儲存：條件只在頁面記憶體，重新整理就回到沒有篩選。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Game = root.Game || {};
    root.Game.catalogState = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var AUDIENCE_VALUES = ['male', 'female', 'all'];      // 讀者頻道的篩選值（空字串是「不篩選」）；與後端 `story_preference.AUDIENCES` 對照由測試守住
  var Q_MAX_CHARS = 50;                                 // 關鍵字整體上限，與後端 `catalog_query.Q_MAX_CHARS` 一致（搜尋框的 maxlength）
  var TAGS_MAX = 30;                                    // 常用標籤至多幾個，與後端 `CATALOG_TAGS_LIMIT` 一致
  var DEFAULT_ERROR = '無法載入，請稍後再試。';

  function text(value) { return typeof value === 'string' ? value : ''; }

  function blank() {
    return {
      items: [], hasMore: false, nextCursor: '', loading: false, loaded: false, error: '',
      q: '', tag: '', audience: '', seq: 0,
      tags: [], tagsLoaded: false, tagsLoading: false
    };
  }

  /* 把輸入整理成 `{ q, tag, audience }`：字串、去頭尾空白（關鍵字內的連續空白併成一個）、頻道只接受三個值（其他是空字串＝不篩選）。 */
  function normalizeFilters(raw) {
    var source = raw && typeof raw === 'object' ? raw : {};
    return {
      q: text(source.q).replace(/\s+/g, ' ').trim(),
      tag: text(source.tag).trim(),
      audience: AUDIENCE_VALUES.indexOf(source.audience) >= 0 ? source.audience : ''
    };
  }

  function filtersOf(state) { return { q: state.q, tag: state.tag, audience: state.audience }; }
  function hasFilters(state) { return state.q !== '' || state.tag !== '' || state.audience !== ''; }

  /* 標籤的比對不分大小寫（常用標籤是小寫，卡片上的標籤保留作者寫的大小寫）。 */
  function sameTag(left, right) {
    return typeof left === 'string' && typeof right === 'string' && left !== '' && left.toLowerCase() === right.toLowerCase();
  }

  /* 下面三個回傳「改了一項之後的整組條件」，不改狀態；呼叫端再交給 `reset`。 */
  function withQuery(state, q) { var next = filtersOf(state); next.q = text(q); return next; }
  function withAudience(state, audience) { var next = filtersOf(state); next.audience = text(audience); return next; }
  /* 點選標籤：已選取的同一個標籤再點就取消。 */
  function withTag(state, tag) {
    var next = filtersOf(state);
    next.tag = sameTag(state.tag, tag) ? '' : text(tag);
    return next;
  }

  /* 套用新的條件：清空清單、游標與錯誤，序號加一，載入中的旗標歸零（讓新請求立刻能送出；舊請求的回應會因序號不符被丟掉）。回傳新的序號。 */
  function reset(state, filters) {
    var next = normalizeFilters(filters);
    state.q = next.q;
    state.tag = next.tag;
    state.audience = next.audience;
    state.items = [];
    state.hasMore = false;
    state.nextCursor = '';
    state.loading = false;
    state.loaded = false;
    state.error = '';
    state.seq += 1;
    return state.seq;
  }

  /* 請求的選項：只有非空的條件，加上接續用的游標（第一頁沒有）。 */
  function requestOptions(state) {
    var options = {};
    if (state.q !== '') { options.q = state.q; }
    if (state.tag !== '') { options.tag = state.tag; }
    if (state.audience !== '') { options.audience = state.audience; }
    if (state.nextCursor !== '') { options.cursor = state.nextCursor; }
    return options;
  }

  /* 把一頁資料接在清單後面，以 id 去重（兩次請求之間若有劇本上架或恢復，同一筆可能再出現）。 */
  function append(state, rows) {
    var known = Object.create(null);
    state.items.forEach(function (item) { known[item.id] = true; });
    rows.forEach(function (row) {
      if (row && typeof row === 'object' && typeof row.id === 'string' && !known[row.id]) {
        known[row.id] = true;
        state.items.push(row);
      }
    });
  }

  /* 載入目前條件的下一頁（第一頁與重試也是同一個函式：失敗時游標沒有前進，所以重試的是同一頁）。載入中再呼叫不會再送一次請求。
     `hooks.onPage(data)`：套用中的序號收到成功回應之後（例如讀取 `content_mode`）；`hooks.onFailure(info)`：任何失敗（含過時的請求）——
     登入過期與受限登入狀態是整個帳號的事實，與哪一組條件無關。回傳是否載入成功（過時的回應回傳 false）。 */
  function load(state, fetchPage, hooks) {
    if (state.loading) { return Promise.resolve(false); }
    var seq = state.seq;
    state.loading = true;
    state.error = '';
    return fetchPage(requestOptions(state)).then(function (data) {
      if (seq !== state.seq) { return false; }
      var rows = data && Array.isArray(data.scripts) ? data.scripts : [];
      var cursor = data && typeof data.next_cursor === 'string' ? data.next_cursor : '';
      append(state, rows);
      state.hasMore = !!data && data.has_more === true && rows.length > 0 && cursor !== '';
      state.nextCursor = state.hasMore ? cursor : '';
      state.loaded = true;
      if (hooks && typeof hooks.onPage === 'function') { hooks.onPage(data); }
      return true;
    }, function (info) {
      if (hooks && typeof hooks.onFailure === 'function') { hooks.onFailure(info); }
      if (seq !== state.seq) { return false; }
      state.error = info && info.kind === 'canceled' ? '' : ((info && info.message) || DEFAULT_ERROR);
      return false;
    }).finally(function () {
      if (seq === state.seq) { state.loading = false; }
    });
  }

  /* 空清單的種類：還沒載完或有錯誤時是空字串；載完而沒有任何卡片時，有條件是 'filtered'（沒有符合的劇本），沒有條件是 'none'（目前沒有可玩的劇本）。 */
  function emptyKind(state) {
    if (!state.loaded || state.error !== '' || state.items.length > 0) { return ''; }
    return hasFilters(state) ? 'filtered' : 'none';
  }

  /* 常用標籤的回應 `{ tags: [{ tag, count }] }` 整理成清單：只收字串標籤（不重複、至多 `TAGS_MAX` 個），計數不是數字的當 0。 */
  function parseTags(data) {
    var rows = data && Array.isArray(data.tags) ? data.tags : [];
    var seen = Object.create(null);
    var tags = [];
    rows.forEach(function (row) {
      if (tags.length >= TAGS_MAX || !row || typeof row !== 'object' || typeof row.tag !== 'string' || row.tag === '' || seen[row.tag]) { return; }
      seen[row.tag] = true;
      tags.push({ tag: row.tag, count: typeof row.count === 'number' && isFinite(row.count) ? row.count : 0 });
    });
    return tags;
  }

  /* 載入常用標籤（只載一次）。失敗是安靜的：不顯示晶片，其餘功能照常（回傳 false）。 */
  function loadTags(state, fetchTags) {
    if (state.tagsLoading || state.tagsLoaded) { return Promise.resolve(false); }
    state.tagsLoading = true;
    return fetchTags().then(function (data) {
      state.tags = parseTags(data);
      state.tagsLoaded = true;
      return true;
    }, function () {
      return false;
    }).finally(function () { state.tagsLoading = false; });
  }

  return {
    AUDIENCE_VALUES: AUDIENCE_VALUES, Q_MAX_CHARS: Q_MAX_CHARS, TAGS_MAX: TAGS_MAX,
    blank: blank, normalizeFilters: normalizeFilters, filtersOf: filtersOf, hasFilters: hasFilters, sameTag: sameTag,
    withQuery: withQuery, withAudience: withAudience, withTag: withTag, reset: reset, requestOptions: requestOptions,
    load: load, emptyKind: emptyKind, parseTags: parseTags, loadTags: loadTags
  };
});
