/* 登入頁（Vue 3 全域變數模式）。畫面只有 Google 登入與兩個勾選框：「同意服務條款與隱私權政策」與「我確認已滿（最低年齡）歲」。
 * 年齡數字只來自伺服器的年齡規則（state.ageRules），這支腳本與範本都不寫死。
 * 邏輯只有：按下按鈕 -> 向伺服器發起 Google 登入（只送兩個勾選旗標與登入後要去的位置）-> 導向 Google。
 * 按鈕在勾選框沒勾選時仍可按：回訪的使用者不必勾選，新使用者沒勾由伺服器擋下（回到本頁提示）。
 * 處理中禁用按鈕、無論成功或失敗都在 finally 恢復（Auth.useForm）。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  var app = Vue.createApp({
    setup: function () {
      var google = Auth.useForm({ acceptTerms: false, declareAdult: false });

      function signInWithGoogle() {
        return google.run(function () {
          var body = Auth.googleStartBody(google.values.acceptTerms, google.values.declareAdult, state.next);
          return axios.post('/api/auth/google/start', body).then(function (response) {
            // 導向前先確認是 https；其他協定一律不導向
            var url = response.data && response.data.authorization_url;
            if (!Auth.isSafeProviderUrl(url, window.location.href)) { throw new Error('授權網址不是 https'); }
            window.location.assign(new URL(url, window.location.href).href);
          });
        });
      }

      return {
        google: google,
        notice: state.notice || '',
        info: state.info || '',
        googleEnabled: !!state.googleEnabled,
        ageRules: state.ageRules,                  // 年齡數字只來自伺服器的年齡規則，頁面不寫死
        signInWithGoogle: signInWithGoogle
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
