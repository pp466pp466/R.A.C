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
      var form = Auth.useForm({ email: '', display_name: '', password: '', accept_terms: false });
      var google = Auth.useForm({});
      var done = Vue.ref('');

      function submit() {
        return form.run(function () {
          return axios.post('/api/auth/register', {
            email: form.values.email,
            display_name: form.values.display_name,
            password: form.values.password,
            accept_terms: form.values.accept_terms
          }).then(function (response) {
            form.values.password = '';
            done.value = response.data.message;
          });
        }, { onError: function () { form.values.password = ''; } });
      }

      // Google 註冊與上方的同意勾選連動：沒勾選就不離開本站
      function signUpWithGoogle() {
        google.clearErrors();
        if (!form.values.accept_terms) {
          google.fieldErrors.accept_terms = '請先勾選同意服務條款與隱私權政策。';
          return Promise.resolve(false);
        }
        return google.run(function () {
          return axios.post('/api/auth/google/start', { accept_terms: true, next: state.next }).then(function (response) {
            goToProvider(response.data.authorization_url);
          });
        });
      }

      return {
        form: form,
        google: google,
        done: done,
        googleEnabled: !!state.googleEnabled,
        submit: submit,
        signUpWithGoogle: signUpWithGoogle
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
