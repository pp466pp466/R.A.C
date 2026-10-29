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
      var google = Auth.useForm({ acceptTerms: false, birthDate: '', ageRange: '' });

      function submit() {
        return form.run(function () {
          return axios.post('/api/auth/login', { email: form.values.email, password: form.values.password }).then(function () {
            window.location.assign(state.next || '/account');     // next 已由伺服器驗證過，只會是站內路徑
          });
        }, { onError: function () { form.values.password = ''; } });
      }

      function signInWithGoogle() {
        google.clearErrors();
        // 同意條款、生日、年齡區間各自獨立；任一項沒填都不離開本站（避免被帶到 Google 才被拒絕），缺哪項就提示哪項。
        // 生日與區間的對照由伺服器在發起時完成：不一致或未滿最低年齡都直接回 400，使用者不會被導向 Google。
        if (!google.values.acceptTerms) {
          google.fieldErrors.accept_terms = '請先勾選同意服務條款與隱私權政策。';
        }
        if (!google.values.birthDate) {
          google.fieldErrors.birth_date = '請輸入生日。';
        }
        if (!google.values.ageRange) {
          google.fieldErrors.age_range = '請選擇年齡區間。';
        }
        if (Object.keys(google.fieldErrors).length) {
          return Promise.resolve(false);
        }
        return google.run(function () {
          return axios.post('/api/auth/google/start', {
            accept_terms: true,
            birth_date: google.values.birthDate,
            age_range: google.values.ageRange,
            next: state.next
          }).then(function (response) {
            goToProvider(response.data.authorization_url);
          });
        }, {
          // 對不起來（常是輸入太快）或不符合資格：清空生日與區間，使用者可立刻重新填寫
          onError: function (info) {
            if (info.code === 'age_mismatch' || info.code === 'age_not_eligible') {
              google.values.birthDate = '';
              google.values.ageRange = '';
            }
          }
        });
      }

      return {
        form: form,
        google: google,
        notice: state.notice || '',
        ageRules: state.ageRules,
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
