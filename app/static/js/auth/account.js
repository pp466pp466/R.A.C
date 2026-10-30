/* 帳號設定頁（Vue 3 全域變數模式）。Modal 與 Toast 的顯示完全由 Vue 狀態接管。
 *
 * 兩種畫面共用這支腳本：一般會員（變更密碼、匯出、刪除帳號……），以及帳號正在等待刪除的人（只有預定刪除時間、撤銷與登出；
 * 伺服器渲染的是另一個樣板，所以多餘的區塊根本不在 HTML 裡）。純函式在 account-lifecycle.js（Auth.lifecycle）。
 */
(function () {
  'use strict';
  var Vue = window.Vue;
  var state = Auth.readInitialState(document);
  var lifecycle = Auth.lifecycle;
  var TIER_LABELS = { free: '一般', plus: '進階', vip: '尊榮' };

  var app = Vue.createApp({
    setup: function () {
      var user = state.user || {};
      var actions = Auth.useForm({});
      var passwordForm = Auth.useForm({ current_password: '', new_password: '' });
      var ageForm = Auth.useForm({ birth_date: '', age_range: '' });
      var ageConfirmed = Vue.ref(!!user.age_confirmed);
      var ageGroup = Vue.ref(user.age_group || null);
      var logoutAllOpen = Vue.ref(false);
      var changePasswordOpen = Vue.ref(false);
      var deleteOpen = Vue.ref(false);
      var deleteForm = Auth.useForm({ password: '', confirm_text: '' });
      var exportForm = Auth.useForm({});
      var exportingFormat = Vue.ref('');
      var exportController = null;
      var confirmText = (state.deletion && state.deletion.confirmText) || '';

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

      // 補填生日與年齡區間：沒填就不送出請求；成功後以 Toast 回報並更新狀態（補填區塊隨之消失，未成年者看到安全模式的提示）
      function verifyAge() {
        ageForm.clearErrors();
        if (!ageForm.values.birth_date) { ageForm.fieldErrors.birth_date = '請輸入生日。'; }
        if (!ageForm.values.age_range) { ageForm.fieldErrors.age_range = '請選擇年齡區間。'; }
        if (Object.keys(ageForm.fieldErrors).length) {
          return Promise.resolve(false);
        }
        return ageForm.run(function () {
          return axios.post('/api/auth/verify-age', {
            birth_date: ageForm.values.birth_date,
            age_range: ageForm.values.age_range
          }).then(function (response) {
            ageConfirmed.value = true;
            ageGroup.value = response.data.user.age_group || null;
            ageForm.values.birth_date = '';
            ageForm.values.age_range = '';
            UI.toast('年齡確認完成。');
          });
        }, {
          onError: function (info) {
            // 對不起來（常是輸入太快）或不符合資格：清空兩個欄位，可以立刻重新填寫
            if (info.code === 'age_mismatch' || info.code === 'age_not_eligible') {
              ageForm.values.birth_date = '';
              ageForm.values.age_range = '';
            }
            onError(info);
          }
        });
      }

      // ---- 匯出我的資料：POST 後以 blob 下載（不使用外部程式庫）；逾時 60 秒，離開頁面時中止 ----
      function saveFile(blob, name) {
        var url = window.URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = name;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        window.setTimeout(function () { window.URL.revokeObjectURL(url); }, 1000);
      }

      function downloadExport(format) {
        var spec = lifecycle.EXPORT_FORMATS[format];
        if (!spec || exportForm.state.submitting) { return Promise.resolve(false); }
        exportingFormat.value = format;
        exportController = typeof window.AbortController === 'function' ? new window.AbortController() : null;
        return exportForm.run(function () {
          return axios.post('/api/account/export', { format: format }, {
            responseType: 'blob',
            timeout: lifecycle.EXPORT_TIMEOUT_MS,
            signal: exportController ? exportController.signal : undefined
          }).then(function (response) {
            saveFile(response.data, lifecycle.filenameFrom(response.headers && response.headers['content-disposition'], spec.fallbackName));
            UI.toast(spec.label + ' 已匯出，檔案開始下載。');
          }, lifecycle.unwrapBlobError);
        }, { onError: onError }).finally(function () {
          exportingFormat.value = '';
          exportController = null;      // 無論成功、失敗或中斷，一定恢復按鈕與狀態
        });
      }

      window.addEventListener('pagehide', function () {
        if (exportController) { exportController.abort(); }
      });

      // ---- 刪除帳號：確認前不送出請求；成功後所有裝置登出，回到登入頁並說明 7 天寬限期 ----
      function resetDeleteForm() {
        deleteForm.clearErrors();
        deleteForm.values.password = '';
        deleteForm.values.confirm_text = '';
      }

      function deleteAccount() {
        deleteForm.clearErrors();
        var options = {
          hasPassword: !!user.has_password, password: deleteForm.values.password,
          confirmText: deleteForm.values.confirm_text, expectedText: confirmText
        };
        if (!lifecycle.canSubmitDeletion(options)) {
          if (user.has_password) { deleteForm.fieldErrors.password = '請輸入您的密碼。'; }
          else { deleteForm.fieldErrors.confirm_text = '請輸入「' + confirmText + '」以確認。'; }
          return Promise.resolve(false);
        }
        return deleteForm.run(function () {
          return axios.post('/api/account/delete', lifecycle.deletionBody(options)).then(function () {
            leave('/login?notice=deletion_requested');
          });
        }, {
          onError: function (info) {
            deleteForm.values.password = '';
            if (info.kind === 'server' && !Object.keys(info.fields).length) {
              deleteForm.state.formError = lifecycle.describeLifecycleError(info);
            }
            onError(info);
          }
        });
      }

      // ---- 等待刪除：撤銷後重新載入，讓頁面以完整的帳號設定呈現 ----
      var scheduledText = lifecycle.formatScheduledAt(user.deletion_scheduled_at);

      function cancelDeletion() {
        return actions.run(function () {
          return axios.post('/api/account/cancel-deletion').then(function (response) {
            UI.toast(response.data.message);
            window.setTimeout(function () { leave('/account'); }, 1200);
          });
        }, {
          onError: function (info) {
            if (info.kind === 'server') { actions.state.formError = lifecycle.describeLifecycleError(info); }
            onError(info);
          }
        });
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
        ageForm: ageForm,
        ageConfirmed: ageConfirmed,
        ageGroup: ageGroup,
        ageRules: state.ageRules,
        logoutAllOpen: logoutAllOpen,
        changePasswordOpen: changePasswordOpen,
        deleteOpen: deleteOpen,
        deleteForm: deleteForm,
        exportForm: exportForm,
        exportingFormat: exportingFormat,
        confirmText: confirmText,
        scheduledText: scheduledText,
        tierLabel: TIER_LABELS[user.member_tier] || '一般',
        methods: methods.length ? methods.join('、') : '—',
        verificationRequired: !!state.verificationRequired,
        logout: logout,
        logoutAll: logoutAll,
        resend: resend,
        verifyAge: verifyAge,
        changePassword: changePassword,
        resetPasswordForm: resetPasswordForm,
        downloadExport: downloadExport,
        resetDeleteForm: resetDeleteForm,
        deleteAccount: deleteAccount,
        cancelDeletion: cancelDeletion
      };
    }
  });
  UI.install(app);
  app.mount('#app');
})();
