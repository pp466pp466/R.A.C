/* 帳號申訴頁（Vue 3 全域變數模式）。被鎖定的玩家唯一能用的頁面：向管理員說明情況、查看自己申訴的處理狀態。
 * 申訴內容是個資：只以純文字（{{ }}）顯示，不寫入任何瀏覽器儲存體。字數、錯誤代碼的處理集中在 auth-flows.js（可在 Node 測試）。
 * 沒有被鎖定（例如管理員剛解除）時 API 回 404，頁面導向預設頁面。處理中禁用、無論成功或失敗都在 finally 恢復（Auth.useForm）。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);
  var limits = state.appeal || { maxChars: 1000, hourlyLimit: 3 };

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ content: '' });
      var appeals = Vue.ref([]);
      var loadError = Vue.ref('');
      var counter = Vue.computed(function () { return Auth.appealCounter(form.values.content, limits.maxChars); });
      var hasPending = Vue.computed(function () {
        return appeals.value.some(function (item) { return item.status === 'pending'; });
      });

      function timeText(iso) {
        var date = new Date(iso);
        return isNaN(date.getTime()) ? '' : date.toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false });
      }

      function applyOutcome(outcome) {
        if (outcome.action === 'redirect') { window.location.assign(outcome.to); }
      }

      function load() {
        loadError.value = '';
        return axios.get('/api/appeals/mine').then(function (response) {
          appeals.value = (response.data && response.data.appeals) || [];
        }, function (error) {
          var info = Auth.describeError(error);
          var outcome = Auth.appealOutcome(info);
          if (outcome.action === 'redirect') { applyOutcome(outcome); } else { loadError.value = outcome.message || info.message; }
        });
      }

      function submit() {
        form.clearErrors();
        var problems = Auth.validateAppeal(form.values.content, limits.maxChars);
        if (Object.keys(problems).length) {
          Object.assign(form.fieldErrors, problems);
          return Promise.resolve(false);
        }
        return form.run(function () {
          return axios.post('/api/appeals', { content: form.values.content }).then(function (response) {
            form.values.content = '';
            appeals.value = [response.data.appeal].concat(appeals.value);
            UI.toast('申訴已送出，請等待管理員處理。');
          });
        }, { onError: function (info) { applyOutcome(Auth.appealOutcome(info)); } });
      }

      function refresh() { window.location.reload(); }

      function logout() {
        return axios.post('/api/auth/logout').then(function () {}, function () {}).finally(function () {
          window.location.assign('/login');
        });
      }

      Vue.onMounted(load);

      return {
        form: form, appeals: appeals, loadError: loadError, counter: counter, hasPending: hasPending, limits: limits,
        statusLabel: Auth.appealStatusLabel, timeText: timeText, submit: submit, refresh: refresh, logout: logout
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
