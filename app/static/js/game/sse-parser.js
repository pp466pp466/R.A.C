/* Server-Sent Events 的解析器（純函式，只吃字串，與框架、網路、計時器無關，Node 可以直接測試；見 rpg-stream-client 規格與 rpg-game-ui 的 design D2）。
 *
 * `createParser()` 回傳 `{ feed(text) }`：把 `TextDecoder({stream: true})` 解碼後的字串依序餵進來，每次回傳這一塊完成的事件陣列 `[{ event, data }]`。
 * - 事件以空行分隔（`\n\n`，容許 `\r\n\r\n` 與單獨的 `\r`；`\r` 與 `\n` 被切在兩塊之間也能正確合併）；一個事件的每一行是 `欄位: 值`。
 * - 只取 `event:` 與 `data:`：多行 `data:` 以換行串接（SSE 標準）；`:` 開頭的行是註解（例如心跳）；其他欄位（`id:`、`retry:`）忽略。
 * - `data` 必須是 JSON 物件（後端的每個事件都是單行 JSON 物件）；JSON 解析失敗、不是物件、或缺 `event` 名稱的事件整個丟棄，後續事件照常處理。
 *   未知的事件名稱照樣回傳，由呼叫端決定要不要處理（規格：未知事件被忽略而不中斷串流）。
 * - 不完整的尾端留在緩衝，等下一塊補齊；串流結束時的殘餘不會被當成事件（完整接收的唯一依據是 `done`，不是連線結束）。
 * 這裡**不記錄、不輸出任何內容**（事件本文可能是玩家的劇情）；`feed` 不會拋出例外。
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

  /* 一個事件區塊（不含結尾的空行）轉成 { event, data }；不成立回傳 null。 */
  function parseBlock(block) {
    var eventName = '';
    var dataLines = [];
    var lines = block.split('\n');
    for (var index = 0; index < lines.length; index += 1) {
      var line = lines[index];
      if (line === '' || line.charAt(0) === ':') { continue; }            // 註解與空行
      var colon = line.indexOf(':');
      var field = colon < 0 ? line : line.slice(0, colon);
      var value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.charAt(0) === ' ') { value = value.slice(1); }            // 標準只去掉冒號後的一個空格
      if (field === 'event') { eventName = value; }
      else if (field === 'data') { dataLines.push(value); }
    }
    if (!eventName || dataLines.length === 0) { return null; }
    var data;
    try { data = JSON.parse(dataLines.join('\n')); } catch (error) { return null; }
    if (data === null || typeof data !== 'object' || Array.isArray(data)) { return null; }
    return { event: eventName, data: data };
  }

  function createParser() {
    var buffer = '';
    var heldCarriageReturn = false;       // 上一塊以 \r 結尾：它可能是 \r\n 的前半，等下一塊再決定

    function feed(text) {
      var chunk = typeof text === 'string' ? text : '';
      if (heldCarriageReturn) { chunk = '\r' + chunk; heldCarriageReturn = false; }
      if (chunk.charAt(chunk.length - 1) === '\r') { heldCarriageReturn = true; chunk = chunk.slice(0, -1); }
      buffer += chunk.replace(/\r\n|\r/g, '\n');
      var events = [];
      var boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        var parsed = parseBlock(buffer.slice(0, boundary));
        if (parsed) { events.push(parsed); }
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf('\n\n');
      }
      return events;
    }

    return { feed: feed };
  }

  return { createParser: createParser };
});
