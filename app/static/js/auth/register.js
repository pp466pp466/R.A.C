/* 註冊頁（Vue 3 全域變數模式）。成功後只顯示伺服器的通用訊息，不暗示信箱是否已存在。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  function goToProvider(url) {
    var parsed = new URL(url, window.location.href);
    if (parsed.protocol !== 'https:') { throw new Error('授權網址不是 https'); }
    window.location.assign(parsed.href);
  }

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ email: '', display_name: '', password: '', accept_terms: false, birth_date: '', age_range: '' });
      var google = Auth.useForm({});
      var done = Vue.ref('');

      function submit() {
        return form.run(function () {
          return axios.post('/api/auth/register', {
            email: form.values.email,
            display_name: form.values.display_name,
            password: form.values.password,
            accept_terms: form.values.accept_terms,
            birth_date: form.values.birth_date,
            age_range: form.values.age_range
          }).then(function (response) {
            form.values.password = '';
            done.value = response.data.message;
          });
        }, { onError: onRegisterError });
      }

      // 失敗後一律清空密碼。生日與區間對不起來（常是輸入太快）或不符合註冊資格時，回到註冊頁的初始狀態：
      // 清空生日與區間，保留電子郵件與顯示名稱，使用者可以立刻以同一個信箱重新填寫，不需要換信箱。
      function onRegisterError(info) {
        form.values.password = '';
        resetAgeOnRejection(info);
      }

      function resetAgeOnRejection(info) {
        if (info.code === 'age_mismatch' || info.code === 'age_not_eligible') {
          form.values.birth_date = '';
          form.values.age_range = '';
        }
      }

      // Google 註冊與上方的欄位連動（同意條款、生日、年齡區間各自獨立）：任一項沒填就不離開本站，缺哪項就提示哪項。
      // 生日與區間的對照由伺服器在發起時完成：不一致或未滿最低年齡都會直接回 400，使用者不會被導向 Google。
      function signUpWithGoogle() {
        google.clearErrors();
        if (!form.values.accept_terms) {
          google.fieldErrors.accept_terms = '請先勾選同意服務條款與隱私權政策。';
        }
        if (!form.values.birth_date) {
          google.fieldErrors.birth_date = '請輸入生日。';
        }
        if (!form.values.age_range) {
          google.fieldErrors.age_range = '請選擇年齡區間。';
        }
        if (Object.keys(google.fieldErrors).length) {
          return Promise.resolve(false);
        }
        return google.run(function () {
          return axios.post('/api/auth/google/start', {
            accept_terms: true,
            birth_date: form.values.birth_date,
            age_range: form.values.age_range,
            next: state.next
          }).then(function (response) {
            goToProvider(response.data.authorization_url);
          });
        }, { onError: resetAgeOnRejection });
      }

      return {
        form: form,
        google: google,
        done: done,
        googleEnabled: !!state.googleEnabled,
        ageRules: state.ageRules,
        submit: submit,
        signUpWithGoogle: signUpWithGoogle
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
