/* 劇情分段（純函式，只吃字串，與框架、計時器無關，Node 可以直接測試；見 rpg-story-pages 規格與 rpg-story-view 的 design D1）。
 *
 * `paginate(text, { done })` 回傳 `{ pages, pending }`：
 *   - `pages`：已經「釋出」的段（字串陣列，每段已去除頭尾空白，不含換行）。**已釋出的段不會因為後續文字到達而改變**
 *     （性質：對任何前綴 P 與完整文字 T，`paginate(P).pages` 是 `paginate(T, { done: true }).pages` 的前綴）。
 *   - `pending`：尚未釋出的尾端（`done` 為真時一定是空字串）。
 *
 * 分段規則：
 *   - 句界：句末標點 `。！？…`（連續者視為一組）及其後緊接的收尾引號、括號與空白屬於前一句；引號（「」『』“”）內的句末標點不算句界
 *     （引號內的話整段算同一句，引號闔上之後才判斷）；換行是硬分界（段不跨換行，也重設引號深度）。
 *     只有標點、沒有內容的「句」（例如開頭的「……」）不獨立成句，併入後面的句子。
 *   - 一段至多 3 句（`PAGE_MAX_SENTENCES`），且加入下一句會使累計超過約 150 字（`PAGE_MAX_CHARS`）時，在句界之前切段。
 *   - 單一句超過 150 字：自己成為獨立的數段，先於逗號、分號、頓號處切（盡量湊滿但不超過 150 字），
 *     仍無處可切才在 150 字處切，並且只在字素邊界切（不切斷表情符號與組合字）。
 *   - 各段依序串接，扣除分界處的換行與空白之後等於原文（不丟字、不增字）；空文字或只有空白為零段。
 *
 * 串流（`done` 不為真）：一段在「湊滿」時才釋出——已有 3 個完整句、其後已出現換行、或再加下一句（哪怕只收到它的開頭）就會超過字數上限。
 *   尚未湊滿的尾端暫不釋出。**一個句子要等到它後面出現下一個有內容的字元（或換行、或 `done`）才算完整**：
 *   「……」可能分成兩個片段到達，若句末標點一到就釋出，已釋出的段之後就會被改寫。
 *   所以文字剛好停在句末標點時，最後那一句還不算完整，等下一個字元或 `done`。
 *
 * 這裡不記錄、不輸出任何內容（輸入是玩家的劇情）；`paginate` 不會拋出例外。
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

  var PAGE_MAX_SENTENCES = 3;
  var PAGE_MAX_CHARS = 150;

  var TERMINATORS = '。！？…';
  var OPEN_QUOTES = '「『“';
  var CLOSE_QUOTES = '」』”';
  var CLOSE_BRACKETS = '）)';
  var CLAUSE_MARKS = '，,；;、';
  var SPACE = /\s/;

  function trim(text) { return text.replace(/^\s+|\s+$/g, ''); }
  function measure(text) { return text.length; }     // 含收尾空白：一段的長度不會超過各句長度的總和
  function isMember(set, char) { return set.indexOf(char) >= 0; }

  /* ---- 字素（只在單句超過字數上限而需要硬切時才用）------------------------------------------------------------------- */

  var segmenter = null;
  function graphemes(text) {
    if (segmenter === null) {
      segmenter = (typeof Intl !== 'undefined' && Intl.Segmenter) ? new Intl.Segmenter('zh-Hant', { granularity: 'grapheme' }) : false;
    }
    if (!segmenter) { return Array.from(text); }          // 後備：至少以 code point 為單位，不會切開代理對
    var parts = [];
    var iterator = segmenter.segment(text)[Symbol.iterator]();
    for (var step = iterator.next(); !step.done; step = iterator.next()) { parts.push(step.value.segment); }
    return parts;
  }

  /* 在字素邊界把過長的文字切成不超過上限的數塊（單一字素本身超過上限時整個保留，不切斷）。 */
  function hardCut(text) {
    if (text.length <= PAGE_MAX_CHARS) { return [text]; }
    var pieces = [];
    var current = '';
    graphemes(text).forEach(function (part) {
      if (current && current.length + part.length > PAGE_MAX_CHARS) { pieces.push(current); current = ''; }
      current += part;
    });
    if (current) { pieces.push(current); }
    return pieces;
  }

  /* ---- 單一句超過上限：先於逗號、分號、頓號處切，仍無處可切才硬切 ------------------------------------------------------- */

  function splitClauses(text) {
    var clauses = [];
    var start = 0;
    var index = 0;
    while (index < text.length) {
      if (isMember(CLAUSE_MARKS, text.charAt(index))) {
        var end = index + 1;
        while (end < text.length && (isMember(CLAUSE_MARKS, text.charAt(end)) || SPACE.test(text.charAt(end)))) { end += 1; }
        clauses.push(text.slice(start, end));
        start = end;
        index = end;
      } else {
        index += 1;
      }
    }
    if (start < text.length) { clauses.push(text.slice(start)); }
    return clauses;
  }

  function splitLong(sentence) {
    var pieces = [];
    var current = '';
    splitClauses(trim(sentence)).forEach(function (clause) {
      if (clause.length > PAGE_MAX_CHARS) {                // 這一小句自己就超過上限：沒有逗號可切，只能硬切；最後一塊留著，後面的小句還可以接上去
        if (current) { pieces.push(current); current = ''; }
        var cut = hardCut(clause);
        current = cut.pop();
        cut.forEach(function (piece) { pieces.push(piece); });
      } else if (current && current.length + clause.length > PAGE_MAX_CHARS) {
        pieces.push(current);
        current = clause;
      } else {
        current += clause;
      }
    });
    if (current) { pieces.push(current); }
    return pieces.map(trim).filter(function (piece) { return piece !== ''; });
  }

  /* ---- 句界：掃描一個區塊（不含換行）--------------------------------------------------------------------------------------- */

  /* 回傳 { sentences, tail }：`sentences` 是已確定完整的句子（原文片段，含收尾引號與句後空白，不含下一句的任何字元），
   * `tail` 是其後尚未確定完整的尾端（可能是沒有句末標點的句子，也可能是剛好停在句末標點、還不知道後面會不會接收尾引號的句子）。 */
  function scanBlock(block) {
    var sentences = [];
    var start = 0;
    var depth = 0;            // 引號深度；大於 0 時句末標點不算句界
    var afterEnd = false;     // 目前這個位置前面是句末標點（含其後的收尾引號、括號與空白）
    var hasContent = false;   // 這一句已經有標點以外的內容
    for (var index = 0; index < block.length; index += 1) {
      var char = block.charAt(index);
      if (isMember(TERMINATORS, char)) { afterEnd = true; continue; }
      if (isMember(CLOSE_QUOTES, char)) { if (depth > 0) { depth -= 1; } continue; }
      if (isMember(CLOSE_BRACKETS, char) || SPACE.test(char)) { continue; }
      if (afterEnd && depth === 0 && hasContent) {
        sentences.push(block.slice(start, index));
        start = index;
      }
      afterEnd = false;
      hasContent = true;
      if (isMember(OPEN_QUOTES, char)) { depth += 1; }
    }
    return { sentences: sentences, tail: block.slice(start) };
  }

  /* ---- 把一個區塊的句子湊成段 ------------------------------------------------------------------------------------------------ */

  /* `closed` 為真（換行已出現或 `done`）時所有句子都已定案，段全部釋出；否則最後一個未滿的段保留為 pending。 */
  function packBlock(sentences, tail, closed, pages) {
    var current = [];
    var currentLength = 0;

    function flush() {
      if (current.length) {
        var page = trim(current.join(''));
        if (page) { pages.push(page); }
      }
      current = [];
      currentLength = 0;
    }

    sentences.forEach(function (sentence) {
      var length = measure(sentence);
      if (length > PAGE_MAX_CHARS) {                       // 超長的句子不與鄰句合併：先收掉目前的段，自己成為數段
        flush();
        splitLong(sentence).forEach(function (piece) { pages.push(piece); });
        return;
      }
      if (current.length && (current.length >= PAGE_MAX_SENTENCES || currentLength + length > PAGE_MAX_CHARS)) { flush(); }
      current.push(sentence);
      currentLength += length;
    });

    if (closed) { flush(); return ''; }

    // 還沒有定案的尾端只用來判斷「下一句會不會放不進去」：尾端只會越來越長，所以現在就放不進去的，之後也一樣。
    if (current.length >= PAGE_MAX_SENTENCES || (current.length && currentLength + measure(tail) > PAGE_MAX_CHARS)) { flush(); }
    return trim(current.join('') + tail);
  }

  /* ---- 對外 ------------------------------------------------------------------------------------------------------------------ */

  function paginate(text, options) {
    var done = !!(options && options.done === true);
    var source = typeof text === 'string' ? text : '';
    var blocks = source.replace(/\r\n?/g, '\n').split('\n');
    var pages = [];
    var pending = '';
    for (var index = 0; index < blocks.length; index += 1) {
      var closed = done || index < blocks.length - 1;      // 後面還有換行的區塊一定已經結束
      var scanned = scanBlock(blocks[index]);
      var sentences = scanned.sentences;
      var tail = scanned.tail;
      if (closed) {
        if (trim(tail)) { sentences.push(tail); }
        tail = '';
      }
      pending = packBlock(sentences, tail, closed, pages);
    }
    return { pages: pages, pending: pending };
  }

  return { paginate: paginate, PAGE_MAX_SENTENCES: PAGE_MAX_SENTENCES, PAGE_MAX_CHARS: PAGE_MAX_CHARS };
});
