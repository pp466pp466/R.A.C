/* 共用 UI 元件：自製 Modal 與 Toast（Vue 3 全域變數模式，見 AGENTS.md 第 2 節）。
 *
 * 規則：
 *  - 顯示／隱藏完全由 Vue 的響應式狀態（v-if／v-for）接管；
 *  - 只借用 Bootstrap 的 CSS，不使用它的 JavaScript，也不使用 data-bs-* 互動屬性；
 *  - 所有文字一律當作純文字（{{ }} 插值），不使用 v-html；
 *  - 元件內建文字為繁體中文。
 *
 * 用法（在頁面的 <script> 內）：
 *   const { createApp, ref } = Vue;
 *   const app = createApp({ ... });
 *   UI.install(app);                 // 註冊 <ui-modal>、<ui-toast-region>、<ui-restriction-notice> 與 <ui-wallet-balance>
 *   UI.toast('已儲存');              // 顯示提示（等同 UI.useToast().show）
 *
 * 全站的遊戲幣餘額標籤：`<ui-wallet-balance></ui-wallet-balance>`（指向 /wallet 的連結；見 shared-wallet-balance 規格）。
 * 標籤掛載時呼叫全站單例 `UI.wallet.load()`（wallet-store.js；同一頁只請求一次、不快取、不寫瀏覽器儲存）；頁面自己的錢包邏輯共用同一個 `UI.wallet`。
 */
(function (root) {
  'use strict';

  var Vue = root.Vue;
  if (!Vue) { throw new Error('ui-components.js 必須在 Vue 載入之後才能載入。'); }
  var ref = Vue.ref;
  var watch = Vue.watch;
  var nextTick = Vue.nextTick;
  var onMounted = Vue.onMounted;
  var onBeforeUnmount = Vue.onBeforeUnmount;

  root.UI = root.UI || {};

  // ---- Toast 狀態 ----------------------------------------------------------
  var toasts = root.UI.createToastStore({ reactive: Vue.reactive });
  root.UI.toasts = toasts;
  root.UI.useToast = function () { return { show: toasts.show, dismiss: toasts.dismiss, clear: toasts.clear }; };
  root.UI.toast = function (message, settings) { return toasts.show(message, settings); };

  // ---- 遊戲幣狀態庫（全站單例）與餘額標籤 ---------------------------------------
  // 預設的 fetcher 是 wallet-api.js 的 `wallet()`（axios 在呼叫時才取用）；`load()` 永不拋出，失敗時標籤顯示「—」。
  var wallet = root.UI.createWalletStore({ reactive: Vue.reactive, fetcher: function (settings) { return root.UI.walletApi.wallet(settings); } });
  root.UI.wallet = wallet;

  // 標籤：顯示合計餘額（載入中與失敗為「—」）；手機寬度由 CSS 隱藏「遊戲」二字而成為「幣 N」；管理員附「不消耗」。文字一律 {{ }} 純文字插值。
  var WalletBalance = {
    name: 'UiWalletBalance',
    setup: function () {
      onMounted(function () { wallet.load(); });
      return { label: Vue.computed(function () { return root.UI.walletLabelOf(wallet.state); }) };
    },
    template:
      '<a class="ui-wallet-balance" href="/wallet" :aria-label="label.aria">' +
      '<span class="ui-wallet-balance__word" aria-hidden="true">遊戲</span>' +
      '<span class="ui-wallet-balance__coin" aria-hidden="true">幣</span>' +
      '<strong class="ui-wallet-balance__amount" aria-hidden="true">{{ label.amount }}</strong>' +
      '<span v-if="label.exempt" class="ui-wallet-balance__note" aria-hidden="true">不消耗</span>' +
      '</a>'
  };

  // ---- Toast 容器元件 ------------------------------------------------------
  var ToastRegion = {
    name: 'UiToastRegion',
    props: { closeLabel: { type: String, default: '關閉提示' } },
    setup: function () { return { store: toasts }; },
    template:
      '<transition-group name="ui-toast" tag="div" class="ui-toast-region" aria-live="polite" aria-relevant="additions">' +
      '  <div v-for="item in store.state.items" :key="item.id" class="ui-toast" :role="item.type === \'error\' ? \'alert\' : \'status\'">' +
      '    <span class="ui-toast__message">{{ item.message }}</span>' +
      '    <button type="button" class="ui-toast__close" :aria-label="closeLabel" @click="store.dismiss(item.id)">×</button>' +
      '  </div>' +
      '</transition-group>'
  };

  // ---- Modal 元件 ----------------------------------------------------------
  var FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
  var modalCounter = 0;

  var Modal = {
    name: 'UiModal',
    props: {
      modelValue: { type: Boolean, default: false },
      statusCode: { type: String, default: 'SYSTEM_PROMPT' },   // 左上角的系統狀態碼
      title: { type: String, default: '' },
      closable: { type: Boolean, default: true },               // 是否可用 Esc／點遮罩關閉
      showCancel: { type: Boolean, default: true },
      confirmText: { type: String, default: '確認' },
      cancelText: { type: String, default: '取消' },
      closeOnConfirm: { type: Boolean, default: true }
    },
    emits: ['update:modelValue', 'confirm', 'cancel', 'close'],
    setup: function (props, context) {
      var dialog = ref(null);
      var titleId = 'ui-modal-title-' + (++modalCounter);
      var previouslyFocused = null;

      function close(reason) {
        context.emit('update:modelValue', false);
        context.emit('close', reason);
      }

      function focusableElements() {
        if (!dialog.value) { return []; }
        return Array.prototype.filter.call(dialog.value.querySelectorAll(FOCUSABLE), function (el) {
          return el.offsetParent !== null || el === document.activeElement;
        });
      }

      function onKeydown(event) {
        if (event.key === 'Escape' && props.closable) {
          event.stopPropagation();
          close('escape');
          return;
        }
        if (event.key !== 'Tab') { return; }
        var items = focusableElements();
        if (items.length === 0) { event.preventDefault(); if (dialog.value) { dialog.value.focus(); } return; }
        var first = items[0];
        var last = items[items.length - 1];
        var active = document.activeElement;
        if (event.shiftKey && (active === first || active === dialog.value)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
      }

      // 即使焦點意外跑到對話框外，Esc 仍然可以關閉
      function onDocumentKeydown(event) {
        if (event.key === 'Escape' && props.closable && dialog.value && !dialog.value.contains(event.target)) { close('escape'); }
      }

      function onOverlayMousedown(event) {
        if (props.closable && event.target === event.currentTarget) { close('overlay'); }
      }

      function confirm() {
        context.emit('confirm');
        if (props.closeOnConfirm) { close('confirm'); }
      }

      function cancel() {
        context.emit('cancel');
        close('cancel');
      }

      function activate() {
        previouslyFocused = document.activeElement;
        document.body.classList.add('ui-modal-open');
        document.addEventListener('keydown', onDocumentKeydown);
        nextTick(function () {
          var items = focusableElements();
          var target = items.length ? items[items.length - 1] : dialog.value; // 預設聚焦在主要動作按鈕
          if (target) { target.focus(); }
        });
      }

      function release() {
        document.body.classList.remove('ui-modal-open');
        document.removeEventListener('keydown', onDocumentKeydown);
        if (previouslyFocused && typeof previouslyFocused.focus === 'function') { previouslyFocused.focus(); }
        previouslyFocused = null;
      }

      watch(function () { return props.modelValue; }, function (open) {
        if (open) { activate(); } else { release(); }
      });

      onMounted(function () {
        if (props.modelValue) { activate(); } // 一掛載就是開啟狀態
      });

      onBeforeUnmount(function () {
        if (props.modelValue) { release(); }
      });

      return { dialog: dialog, titleId: titleId, onKeydown: onKeydown, onOverlayMousedown: onOverlayMousedown, confirm: confirm, cancel: cancel };
    },
    template:
      '<teleport to="body">' +
      '  <transition name="ui-fade">' +
      '    <div v-if="modelValue" class="ui-modal-overlay" @mousedown="onOverlayMousedown">' +
      '      <div class="ui-modal" ref="dialog" role="dialog" aria-modal="true" tabindex="-1"' +
      '           :aria-labelledby="title ? titleId : null" @keydown="onKeydown">' +
      '        <span class="ui-modal__code" aria-hidden="true">[ {{ statusCode }} ]</span>' +
      '        <h2 v-if="title" :id="titleId" class="ui-modal__title">{{ title }}</h2>' +
      '        <div class="ui-modal__body"><slot></slot></div>' +
      '        <div class="ui-modal__actions">' +
      '          <slot name="actions">' +
      '            <button v-if="showCancel" type="button" class="ui-btn" @click="cancel">{{ cancelText }}</button>' +
      '            <button type="button" class="ui-btn ui-btn--primary" @click="confirm">{{ confirmText }}</button>' +
      '          </slot>' +
      '        </div>' +
      '      </div>' +
      '    </div>' +
      '  </transition>' +
      '</teleport>'
  };

  // ---- 申訴捷徑（appeal-shortcut 規格）------------------------------------
  // 被鎖定或被暫停發佈時，在所在的頁面顯示說明與「前往申訴」按鈕。`kind` 由 UI.restrictionOf 判定（只依伺服器的回應）；
  // 按鈕是指向 /appeal 的一般連結（沒有 data-bs-*、沒有查詢字串）。文字純文字，不使用 v-html。
  var RestrictionNotice = {
    name: 'UiRestrictionNotice',
    props: { kind: { type: String, default: null } },
    setup: function (props) {
      return { notice: Vue.computed(function () { return props.kind ? root.UI.restrictionNoticeOf(props.kind) : null; }) };
    },
    template:
      '<div v-if="notice" class="ui-restriction" :class="\'ui-restriction--\' + kind" role="alert">' +
      '  <p class="ui-restriction__text">{{ notice.text }}</p>' +
      '  <a class="ui-btn ui-btn--primary ui-restriction__link" :href="notice.href">{{ notice.linkText }}</a>' +
      '</div>'
  };

  root.UI.Modal = Modal;
  root.UI.ToastRegion = ToastRegion;
  root.UI.RestrictionNotice = RestrictionNotice;
  root.UI.WalletBalance = WalletBalance;
  root.UI.install = function (app) {
    app.component('ui-modal', Modal);
    app.component('ui-toast-region', ToastRegion);
    app.component('ui-restriction-notice', RestrictionNotice);
    app.component('ui-wallet-balance', WalletBalance);
  };
})(typeof self !== 'undefined' ? self : this);
