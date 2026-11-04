/* 里程碑預覽（純函式，與框架無關，Node 可以直接測試；見 script-authoring 規格「里程碑預覽」）。
 *
 * 純前端，不呼叫任何 API：把編輯中的里程碑依觸發排序，讓作者一眼看出事件在故事中的先後。
 * 排序規則：玩家年齡的觸發排在遊戲內年份的觸發之前（兩種觸發沒有共同的時間軸——年齡是相對於角色，年份是相對於曆法），
 * 各自依數值由小到大，數值相同時保持原本的順序。觸發不完整（沒填種類或數值不是非負整數）的里程碑不列入預覽，
 * 作者在編輯器裡會看到伺服器對該欄位的錯誤說明。
 * 有曆法的起始年時，年份觸發另外註明「開局後第 N 年」。所有文字是純文字，頁面以 {{ }} 插值顯示。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.Scripts = root.Scripts || {};
    Object.keys(api).forEach(function (key) { root.Scripts[key] = api[key]; });
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var KIND_ORDER = { age: 0, year: 1 };

  function parseValue(value) {
    var raw = typeof value === 'number' ? String(value) : (typeof value === 'string' ? value.trim() : '');
    return /^[0-9]+$/.test(raw) ? Number(raw) : null;
  }

  function startYearOf(calendar) {
    if (!calendar || typeof calendar !== 'object') { return null; }
    return parseValue(calendar.start_year !== undefined ? calendar.start_year : calendar.calendar_start_year);
  }

  /* milestones：編輯器的列（{ kind, value, title, note }）或伺服器的內容（{ trigger: { kind, value }, title, note }）都可以。 */
  function previewMilestones(milestones, calendar) {
    var start = startYearOf(calendar);
    var items = [];
    (Array.isArray(milestones) ? milestones : []).forEach(function (m, index) {
      if (!m || typeof m !== 'object') { return; }
      var trigger = m.trigger && typeof m.trigger === 'object' ? m.trigger : m;
      var kind = trigger.kind;
      var value = parseValue(trigger.value);
      if (!Object.prototype.hasOwnProperty.call(KIND_ORDER, kind) || value === null) { return; }
      var title = typeof m.title === 'string' && m.title.trim() ? m.title.trim() : '（未命名）';
      var label = kind === 'age' ? '玩家 ' + value + ' 歲時' : '遊戲內第 ' + value + ' 年';
      if (kind === 'year' && start !== null && value >= start) { label += '（開局後第 ' + (value - start) + ' 年）'; }
      items.push({ index: index, kind: kind, value: value, title: title, label: label, note: typeof m.note === 'string' ? m.note.trim() : '' });
    });
    items.sort(function (a, b) {
      return KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.value - b.value || a.index - b.index;
    });
    return items;
  }

  return { previewMilestones: previewMilestones };
});
