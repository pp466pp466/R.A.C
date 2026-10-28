/* 忘記密碼頁。不論信箱是否存在，伺服器回應都相同，頁面也只顯示那則通用訊息。 */
(function () {
  'use strict';
  var Vue = window.Vue;

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ email: '' });
      var done = Vue.ref('');

      function submit() {
        return form.run(function () {
          return axios.post('/api/auth/forgot-password', { email: form.values.email }).then(function (response) {
            done.value = response.data.message;
          });
        });
      }

      return { form: form, done: done, submit: submit };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
