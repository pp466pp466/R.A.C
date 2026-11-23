/* 登入後設定的三步精靈（`/age`，Vue 3 全域變數模式；onboarding-wizard 規格）。玩家首次以 Google 登入之後唯一能做的事。
 * 步驟、進度、各步可前進的條件、錯誤回到哪一步與一次送出的流程都在 onboarding.js（純函式，Node 測試）；這裡只負責 Vue 接線：
 * 步驟索引、欄位、使用守則 Modal、步驟切換後的焦點。
 * 送出生日、年齡區間與暱稱（同一個請求，全有或全無；暱稱不合格時回到第 1 步，欄位旁顯示說明，生日與區間保持原樣）
 *   -> 成功之後才在偏好不是預設值時存故事偏好（失敗不阻擋）-> 回到登入前要去的位置；
 *   錯誤代碼的處理集中在 auth-flows.js：age_not_eligible（未滿最低年齡，帳號已抹除）-> 只顯示說明與回登入頁的連結；
 *   age_mismatch -> 回第 2 步、通用說明並清空兩個欄位；登入逾時 -> 登入頁。處理中禁用、無論成功或失敗都在 finally 恢復（Auth.useForm）。
 * 這裡不寫任何瀏覽器儲存（生日是個資）：重新整理就從第 1 步重新開始。「已確認使用守則」只是這次畫面上的狀態，不送出、不保存。 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);

  // 第 3 步的取向選項：值與伺服器的列舉一致（all、male、female），文字與帳號設定頁相同
  var PREFERENCE_OPTIONS = [
    { value: 'all', label: '全讀者' },
    { value: 'male', label: '男頻' },
    { value: 'female', label: '女頻' }
  ];

  var app = Vue.createApp({
    setup: function () {
      var form = Auth.useForm({ birthDate: '', ageRange: '', nickname: '', storyPreference: Onboarding.DEFAULT_PREFERENCE });
      var finished = Vue.ref(null);
      var index = Vue.ref(0);
      var acknowledged = Vue.ref(false);
      var guideOpen = Vue.ref(false);
      var heading = Vue.ref(null);

      var progress = Vue.computed(function () { return Onboarding.progress(index.value, Onboarding.STEPS); });
      var gate = Vue.computed(function () { return Onboarding.canAdvance(progress.value.step, form.values, acknowledged.value); });

      var flow = Onboarding.createOnboarding({
        http: { post: function (url, body) { return axios.post(url, body); }, put: function (url, body) { return axios.put(url, body); } },
        navigate: function (to) { window.location.assign(to); },
        destination: Auth.safeDestination(state.next, '/game')
      });

      // 步驟切換之後把焦點移到該步的標題（標題以 tabindex="-1" 可被聚焦），輔助科技會讀出新的標題與「第 N 步，共 M 步」
      function focusHeading() {
        Vue.nextTick(function () { if (heading.value && typeof heading.value.focus === 'function') { heading.value.focus(); } });
      }

      function goTo(position) {
        if (position === index.value) { return; }
        index.value = position;
        focusHeading();
      }

      function goNext() {
        if (!gate.value.ok) { return; }
        goTo(Onboarding.nextIndex(index.value, Onboarding.STEPS));
      }

      function goBack() { goTo(Onboarding.previousIndex(index.value, Onboarding.STEPS)); }

      function goToStep(stepId) {
        for (var i = 0; i < Onboarding.STEPS.length; i += 1) {
          if (Onboarding.STEPS[i].id === stepId) { goTo(i); return; }
        }
      }

      function applyOutcome(outcome) {
        (outcome.clear || []).forEach(function (name) { form.values[name] = ''; });
        if (outcome.action === 'final') { finished.value = { message: outcome.message, to: outcome.to }; }
        if (outcome.action === 'redirect') { window.location.assign(outcome.to); }
        var step = Onboarding.stepForFailure(outcome, Onboarding.STEPS);
        if (step) { goToStep(step); }
      }

      function submit() {
        form.clearErrors();
        var problems = Auth.validateAge(form.values, { nicknameRequired: true });
        if (Object.keys(problems).length) {
          Object.assign(form.fieldErrors, problems);
          var step = Onboarding.stepForFailure({ action: 'fields', fields: problems }, Onboarding.STEPS);
          if (step) { goToStep(step); }
          return Promise.resolve(false);
        }
        return form.run(function () {
          return flow.submit(form.values);
        }, { onError: function (info) { applyOutcome(Auth.ageOutcome(info)); } });
      }

      // Enter 與主要按鈕：最後一步送出，其餘步驟前進（不可前進時什麼都不做，原因顯示在按鈕上方）
      function onPrimary() {
        if (form.state.submitting) { return Promise.resolve(false); }
        if (progress.value.isLast) { return submit(); }
        goNext();
        return Promise.resolve(true);
      }

      function openGuide() { guideOpen.value = true; }
      function confirmGuide() { acknowledged.value = true; }     // 只有按「我已知悉」才算；用 Esc 或「關閉」都不算

      function logout() {
        return axios.post('/api/auth/logout').then(function () {}, function () {}).finally(function () {
          window.location.assign('/login');
        });
      }

      return {
        form: form, finished: finished, ageRules: state.ageRules, progress: progress, gate: gate, acknowledged: acknowledged, guideOpen: guideOpen,
        heading: heading, preferenceOptions: PREFERENCE_OPTIONS, onPrimary: onPrimary, goBack: goBack, openGuide: openGuide,
        confirmGuide: confirmGuide, logout: logout
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
