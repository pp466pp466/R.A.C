/* 表單的共用狀態（Vue 3 全域變數模式）：送出中、欄位錯誤、一般錯誤。
 *
 * 規則（AGENTS.md 第 5 節的精神）：無論成功、失敗或中斷，一定在 finally 恢復可操作狀態。
 * 錯誤分流：欄位錯誤顯示在欄位旁；網路、逾時、頁面過期以 Toast 提示；其他伺服器錯誤顯示在表單上方。
 * 所有文字都當作純文字交給 {{ }} 插值。必須在 Vue、ui-components.js 與 auth-common.js 之後載入。
 */
(function (root) {
  'use strict';
  var Vue = root.Vue;
  var Auth = root.Auth;
  if (!Vue || !Auth) { throw new Error('auth-form.js 必須在 Vue 與 auth-common.js 之後載入。'); }

  Auth.useForm = function useForm(initial) {
    var values = Vue.reactive(Object.assign({}, initial));
    var fieldErrors = Vue.reactive({});
    var state = Vue.reactive({ submitting: false, formError: '' });

    function clearErrors() {
      Object.keys(fieldErrors).forEach(function (name) { delete fieldErrors[name]; });
      state.formError = '';
    }

    /* 執行一次送出。重複點擊（送出中）會被忽略。回傳 Promise，成功為 true、失敗為 false。 */
    function run(task, hooks) {
      hooks = hooks || {};
      if (state.submitting) { return Promise.resolve(false); }
      state.submitting = true;
      clearErrors();
      return Promise.resolve()
        .then(task)
        .then(
          function () { return true; },
          function (error) {
            var info = Auth.describeError(error);
            if (info.kind === 'canceled') { return false; }
            var fieldNames = Object.keys(info.fields);
            fieldNames.forEach(function (name) { fieldErrors[name] = info.fields[name]; });
            if (info.kind === 'network' || info.kind === 'timeout' || info.kind === 'csrf') {
              if (root.UI) { root.UI.toast(info.message, { type: 'error' }); }
            } else if (fieldNames.length === 0) {
              state.formError = info.message;
            }
            if (hooks.onError) { hooks.onError(info); }
            return false;
          }
        )
        .finally(function () { state.submitting = false; });
    }

    return { values: values, fieldErrors: fieldErrors, state: state, run: run, clearErrors: clearErrors };
  };
})(typeof self !== 'undefined' ? self : this);
