/* 重設密碼頁：權杖來自網址片段，讀取後立即從網址列移除；使用者輸入新密碼並按下按鈕才送出。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var token = Auth.readFragmentToken(window);

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ new_password: '', confirm_password: '' });
      var done = Vue.ref('');

      function submit() {
        if (form.values.new_password !== form.values.confirm_password) {
          form.clearErrors();
          form.fieldErrors.confirm_password = '兩次輸入的密碼不一致。';
          return Promise.resolve(false);
        }
        return form.run(function () {
          return axios.post('/api/auth/reset-password', { token: token, new_password: form.values.new_password }).then(function (response) {
            token = '';
            form.values.new_password = '';
            form.values.confirm_password = '';
            done.value = response.data.message;
          });
        });
      }

      return { form: form, done: done, token: token, submit: submit };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
