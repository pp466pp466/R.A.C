/* 打字機緩衝區（Typewriter Buffer）。
 *
 * 接收陸續到達的文字片段（RPG 的串流片段），或一次到達的完整文字（客服的 reply_text），
 * 以平穩的節奏逐字顯示。
 *
 *  - 以「字素」為單位（Intl.Segmenter），中文、表情符號、ZWJ 組合符號、組合重音都不會被切開。
 *  - 積壓的文字越多，顯示越快；呼叫 finish() 後會加速在短時間內補完，不必等很久。
 *  - 輸出一律是「純文字」字串。請用 Vue 的 {{ }} 或 textContent 顯示，絕不要用 v-html／innerHTML。
 *  - 與框架無關：計時器可注入，因此可在 Node 內以假時鐘測試（tests/js/typewriter.test.js）。
 *
 * 用法：
 *   const tw = new UI.TypewriterBuffer({ onUpdate: (text) => (view.value = text), onDone: () => ... });
 *   tw.push(片段); ...; tw.finish();      // 串流
 *   tw.play(完整文字);                    // 一次到達（等同 push + finish）
 *
 * 注意：為了避免把一個字素切成兩半，最後一個字素會等到下一個片段或 finish() 才釋出。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UI = root.UI || {};
    root.UI.TypewriterBuffer = api.TypewriterBuffer;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULTS = {
    baseCharsPerSecond: 40,   // 沒有積壓時的基本速度
    accelerationPerChar: 0.2, // 每多一個積壓字元，速度增加的字元／秒（一次到達的 58 字約 1.3 秒、300 字約 2.7 秒）
    maxCharsPerSecond: 600,   // 一般狀態下的速度上限
    finishSeconds: 3,         // finish() 之後，剩餘文字最晚在這個秒數內顯示完（一般長度的回覆以正常速度就趕得上，只有超長文字才會被加速）
    maxFrameMs: 200           // 單次更新最多採計的經過時間（分頁在背景後回來時避免一次噴出大量文字）
  };

  function defaultScheduler() {
    var hasRaf = typeof requestAnimationFrame === 'function';
    var clock = typeof performance !== 'undefined' && performance.now ? function () { return performance.now(); } : Date.now;
    return {
      now: clock,
      request: function (callback) {
        return hasRaf ? requestAnimationFrame(callback) : setTimeout(function () { callback(clock()); }, 16);
      },
      cancel: function (handle) {
        if (hasRaf) { cancelAnimationFrame(handle); } else { clearTimeout(handle); }
      }
    };
  }

  function createSegmenter() {
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {
      var segmenter = new Intl.Segmenter('zh-Hant', { granularity: 'grapheme' });
      return function (text) {
        var parts = [];
        var iterator = segmenter.segment(text)[Symbol.iterator]();
        for (var step = iterator.next(); !step.done; step = iterator.next()) { parts.push(step.value.segment); }
        return parts;
      };
    }
    return function (text) { return Array.from(text); }; // 後備：至少以 code point 為單位，不會切開代理對
  }

  function TypewriterBuffer(options) {
    options = options || {};
    var settings = {};
    for (var key in DEFAULTS) { settings[key] = options[key] !== undefined ? options[key] : DEFAULTS[key]; }
    this._settings = settings;
    this._onUpdate = options.onUpdate || function () {};
    this._onDone = options.onDone || function () {};
    this._scheduler = options.scheduler || defaultScheduler();
    this._segment = options.segmenter || createSegmenter();
    this.reset();
  }

  TypewriterBuffer.prototype.reset = function () {
    this._cancelFrame();
    this._queue = [];
    this._head = 0;
    this._raw = '';
    this._displayed = '';
    this._credit = 0;
    this._last = null;
    this._handle = null;
    this._finishing = false;
    this._deadline = 0;
    this._done = false;
    this._cancelled = false;
  };

  Object.defineProperty(TypewriterBuffer.prototype, 'text', { get: function () { return this._displayed; } });
  Object.defineProperty(TypewriterBuffer.prototype, 'pending', {
    get: function () { return this._queue.length - this._head + (this._raw ? 1 : 0); }
  });
  Object.defineProperty(TypewriterBuffer.prototype, 'isDone', { get: function () { return this._done; } });

  TypewriterBuffer.prototype.push = function (text) {
    if (this._finishing) { throw new Error('已呼叫 finish()，不能再加入文字；請先 reset()。'); }
    if (this._cancelled || !text) { return; }
    var segments = this._segment(this._raw + String(text));
    this._raw = segments.length ? segments.pop() : ''; // 最後一個字素可能被下一個片段延伸，先保留
    for (var i = 0; i < segments.length; i++) { this._queue.push(segments[i]); }
    this._ensureRunning();
  };

  TypewriterBuffer.prototype.finish = function () {
    if (this._finishing || this._cancelled) { return; }
    this._finishing = true;
    this._deadline = this._scheduler.now() + this._settings.finishSeconds * 1000;
    var rest = this._raw ? this._segment(this._raw) : [];
    this._raw = '';
    for (var i = 0; i < rest.length; i++) { this._queue.push(rest[i]); }
    this._ensureRunning();
  };

  TypewriterBuffer.prototype.play = function (text) {
    this.push(text);
    this.finish();
  };

  /* 立刻顯示全部剩餘文字（例如使用者按「略過動畫」）。 */
  TypewriterBuffer.prototype.flush = function () {
    if (this._cancelled) { return; }
    this._cancelFrame();
    if (!this._finishing) { this.finish(); this._cancelFrame(); }
    this._emit(this._queue.length - this._head);
    this._complete();
  };

  /* 取消：不再顯示任何文字，也不會觸發 onDone。 */
  TypewriterBuffer.prototype.cancel = function () {
    this._cancelled = true;
    this._cancelFrame();
    this._queue = [];
    this._head = 0;
    this._raw = '';
  };

  TypewriterBuffer.prototype._backlog = function () { return this._queue.length - this._head; };

  TypewriterBuffer.prototype._rate = function (now) {
    var s = this._settings;
    var backlog = this._backlog();
    var rate = Math.min(s.maxCharsPerSecond, s.baseCharsPerSecond + backlog * s.accelerationPerChar);
    if (this._finishing) {
      // 速度 = 剩餘量 ÷ 距離截止時間還有多久：保證在期限內補完（若用固定秒數去除剩餘量，會越剩越慢、永遠到不了）
      var secondsLeft = Math.max(this._deadline - now, 16) / 1000;
      rate = Math.max(rate, backlog / secondsLeft);
    }
    return rate;
  };

  TypewriterBuffer.prototype._emit = function (count) {
    if (count <= 0) { return; }
    var end = Math.min(this._queue.length, this._head + count);
    var chunk = '';
    for (var i = this._head; i < end; i++) { chunk += this._queue[i]; }
    this._head = end;
    if (this._head > 1024 && this._head * 2 > this._queue.length) { // 定期壓縮，避免陣列無限成長
      this._queue = this._queue.slice(this._head);
      this._head = 0;
    }
    this._displayed += chunk;
    this._onUpdate(this._displayed);
  };

  TypewriterBuffer.prototype._complete = function () {
    if (this._done || this._cancelled) { return; }
    this._done = true;
    this._onDone();
  };

  TypewriterBuffer.prototype._cancelFrame = function () {
    if (this._handle !== null && this._handle !== undefined) { this._scheduler.cancel(this._handle); }
    this._handle = null;
    this._last = null;
  };

  TypewriterBuffer.prototype._ensureRunning = function () {
    if (this._handle !== null || this._cancelled || this._done) { return; }
    if (this._backlog() === 0 && !this._finishing) { return; }
    var self = this;
    this._handle = this._scheduler.request(function (timestamp) { self._tick(timestamp); });
  };

  TypewriterBuffer.prototype._tick = function (timestamp) {
    this._handle = null;
    if (this._cancelled) { return; }
    var now = typeof timestamp === 'number' ? timestamp : this._scheduler.now();
    var elapsed = this._last === null ? 0 : Math.min(this._settings.maxFrameMs, now - this._last);
    this._last = now;

    this._credit += this._rate(now) * (elapsed / 1000);
    var count = Math.floor(this._credit);
    if (count > 0) {
      this._credit -= count;
      this._emit(count);
    }

    if (this._backlog() > 0) {
      this._ensureRunning();
    } else if (this._finishing) {
      this._complete();
    } else {
      this._last = null; // 暫時沒有文字：停止排程，等下一次 push
    }
  };

  return { TypewriterBuffer: TypewriterBuffer };
});
