/* 登入頁（Vue 3 全域變數模式）。邏輯只有：送出帳密、啟動 Google 登入、顯示錯誤。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  // 導向前先確認是 https（本機開發的假供應商不會走到這裡）；其他協定一律不導向
  function goToProvider(url) {
    var parsed = new URL(url, window.location.href);
    if (parsed.protocol !== 'https:') { throw new Error('授權網址不是 https'); }
    window.location.assign(parsed.href);
  }

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ email: '', password: '' });
      var google = Auth.useForm({ acceptTerms: false, confirmAdult: false });

      function submit() {
        return form.run(function () {
          return axios.post('/api/auth/login', { email: form.values.email, password: form.values.password }).then(function () {
            window.location.assign(state.next || '/account');     // next 已由伺服器驗證過，只會是站內路徑
          });
        }, { onError: function () { form.values.password = ''; } });
      }

      function signInWithGoogle() {
        google.clearErrors();
        // 兩個確認各自獨立；任一項未勾選都不離開本站（避免被帶到 Google 才被拒絕），缺哪項就提示哪項
        if (!google.values.acceptTerms) {
          google.fieldErrors.accept_terms = '請先勾選同意服務條款與隱私權政策。';
        }
        if (!google.values.confirmAdult) {
          google.fieldErrors.confirm_adult = '請先確認您已年滿 18 歲。';
        }
        if (!google.values.acceptTerms || !google.values.confirmAdult) {
          return Promise.resolve(false);
        }
        return google.run(function () {
          return axios.post('/api/auth/google/start', { accept_terms: true, confirm_adult: true, next: state.next }).then(function (response) {
            goToProvider(response.data.authorization_url);
          });
        });
      }

      return {
        form: form,
        google: google,
        notice: state.notice || '',
        googleEnabled: !!state.googleEnabled,
        verificationRequired: !!state.verificationRequired,
        submit: submit,
        signInWithGoogle: signInWithGoogle
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
