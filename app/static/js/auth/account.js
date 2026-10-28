/* 帳號設定頁（Vue 3 全域變數模式）。Modal 與 Toast 的顯示完全由 Vue 狀態接管。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);
  var TIER_LABELS = { free: '一般', plus: '進階', vip: '尊榮' };

  var app = Vue.createApp({
    setup: function () {
      var user = state.user || {};
      var actions = Auth.useForm({});
      var passwordForm = Auth.useForm({ current_password: '', new_password: '' });
      var logoutAllOpen = Vue.ref(false);
      var changePasswordOpen = Vue.ref(false);

      var methods = [];
      if (user.has_password) { methods.push('信箱與密碼'); }
      if (user.google_linked) { methods.push('Google'); }

      // 登入已失效（例如在別的裝置按了「登出所有裝置」）：回到登入頁並保留位置
      function onError(info) {
        if (info.status === 401) { window.location.assign(Auth.loginUrlFor('/account')); }
      }

      function leave(path) {
        window.location.assign(path);
      }

      function logout() {
        return actions.run(function () {
          return axios.post('/api/auth/logout').then(function () { leave('/login'); });
        }, { onError: onError });
      }

      function logoutAll() {
        return actions.run(function () {
          return axios.post('/api/auth/logout-all').then(function () { leave('/login'); });
        }, { onError: onError });
      }

      function resend() {
        return actions.run(function () {
          return axios.post('/api/auth/resend-verification').then(function (response) {
            UI.toast(response.data.message);
          });
        }, { onError: onError });
      }

      function resetPasswordForm() {
        passwordForm.clearErrors();
        passwordForm.values.current_password = '';
        passwordForm.values.new_password = '';
      }

      function changePassword() {
        return passwordForm.run(function () {
          return axios.post('/api/auth/change-password', {
            current_password: passwordForm.values.current_password,
            new_password: passwordForm.values.new_password
          }).then(function (response) {
            changePasswordOpen.value = false;      // 關閉時會觸發 resetPasswordForm，清掉兩個密碼欄位
            UI.toast(response.data.message);
          });
        }, { onError: function (info) { passwordForm.values.current_password = ''; onError(info); } });
      }

      return {
        user: user,
        actions: actions,
        passwordForm: passwordForm,
        logoutAllOpen: logoutAllOpen,
        changePasswordOpen: changePasswordOpen,
        tierLabel: TIER_LABELS[user.member_tier] || '一般',
        methods: methods.length ? methods.join('、') : '—',
        verificationRequired: !!state.verificationRequired,
        logout: logout,
        logoutAll: logoutAll,
        resend: resend,
        changePassword: changePassword,
        resetPasswordForm: resetPasswordForm
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
