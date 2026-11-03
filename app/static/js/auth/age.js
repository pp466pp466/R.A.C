/* 年齡確認頁（Vue 3 全域變數模式）。玩家首次以 Google 登入之後唯一能做的事。
 * 送出生日與年齡區間 -> 成功就回到登入前要去的位置；錯誤代碼的處理集中在 auth-flows.js（可在 Node 測試）：
 *   age_not_eligible（未滿最低年齡，帳號已抹除）-> 只顯示說明與回登入頁的連結；age_mismatch -> 通用說明並清空兩個欄位；
 *   登入逾時 -> 登入頁。處理中禁用、無論成功或失敗都在 finally 恢復（Auth.useForm）。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ birthDate: '', ageRange: '' });
      var finished = Vue.ref(null);

      function applyOutcome(outcome) {
        (outcome.clear || []).forEach(function (name) { form.values[name] = ''; });
        if (outcome.action === 'final') { finished.value = { message: outcome.message, to: outcome.to }; }
        if (outcome.action === 'redirect') { window.location.assign(outcome.to); }
      }

      function submit() {
        form.clearErrors();
        var problems = Auth.validateAge(form.values);
        if (Object.keys(problems).length) {
          Object.assign(form.fieldErrors, problems);
          return Promise.resolve(false);
        }
        return form.run(function () {
          return axios.post('/api/auth/verify-age', Auth.ageBody(form.values)).then(function () {
            window.location.assign(Auth.safeDestination(state.next, '/cs'));
          });
        }, { onError: function (info) { applyOutcome(Auth.ageOutcome(info)); } });
      }

      function logout() {
        return axios.post('/api/auth/logout').then(function () {}, function () {}).finally(function () {
          window.location.assign('/login');
        });
      }

      return { form: form, finished: finished, ageRules: state.ageRules, submit: submit, logout: logout };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
