/* 管理員的密碼驗證頁（Vue 3 全域變數模式）。管理員每次登入都先通過 Google，再到這裡輸入密碼，兩者都通過才建立登入狀態。
 * 密碼只存在輸入框的響應式欄位：送出的當下立即清空，不寫入任何瀏覽器儲存體。錯誤代碼的處理集中在 auth-flows.js（可在 Node 測試）：
 *   登入逾時（沒有或已過期的中間狀態）-> 回登入頁重新 Google；密碼錯誤與鎖定共用同一則通用說明；
 *   「忘記密碼」只在伺服器說有寄信設定時顯示，按下後寄重設信（回應不洩漏其他資訊）。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ password: '' });
      var forgot = Auth.useForm({});
      var forgotAvailable = Vue.ref(!!state.forgotAvailable);
      var forgotMessage = Vue.ref('');

      function submit() {
        form.clearErrors();
        var problems = Auth.validateAdminPassword(form.values.password);
        if (Object.keys(problems).length) {
          Object.assign(form.fieldErrors, problems);
          return Promise.resolve(false);
        }
        var password = form.values.password;
        form.values.password = '';                           // 送出的當下立即清空，不論結果
        return form.run(function () {
          return axios.post('/api/auth/admin-password', { password: password }).then(function (response) {
            window.location.assign(Auth.safeDestination(response.data && response.data.next, '/cs'));
          });
        }, {
          onError: function (info) {
            var outcome = Auth.adminPasswordOutcome(info);
            if (outcome.action === 'redirect') { window.location.assign(outcome.to); }
          }
        });
      }

      function forgotPassword() {
        forgotMessage.value = '';
        return forgot.run(function () {
          return axios.post('/api/auth/admin-password/forgot', {}).then(function (response) {
            forgotMessage.value = (response.data && response.data.message) || '';
          });
        }, {
          onError: function (info) {
            var outcome = Auth.adminForgotOutcome(info);
            forgotMessage.value = outcome.message;
            if (outcome.hideButton) { forgotAvailable.value = false; }
          }
        });
      }

      return { form: form, forgot: forgot, forgotAvailable: forgotAvailable, forgotMessage: forgotMessage, submit: submit, forgotPassword: forgotPassword };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
