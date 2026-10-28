/* 信箱驗證頁：權杖來自網址片段，讀取後立即從網址列移除；載入時不送出任何請求，按下確認才送出。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var token = Auth.readFragmentToken(window);

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({});
      var done = Vue.ref('');

      function confirm() {
        return form.run(function () {
          return axios.post('/api/auth/verify-email', { token: token }).then(function (response) {
            token = '';
            done.value = response.data.message;
          });
        });
      }

      return { form: form, done: done, token: token, confirm: confirm };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
