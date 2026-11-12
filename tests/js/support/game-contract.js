'use strict';
/* 遊戲主畫面的樣板與 game.js 的約定（tests/js/game-template.test.js 與 tests/js/game.test.js 共用，不是測試檔）：
 * 樣板用到的每個名稱，也是 game.js 的 setup() 必須回傳的名稱；兩邊的測試用同一份清單，所以不會各改各的。 */
const CONTRACT = {
  state: [
    'phase', 'loadFailure', 'restriction', 'title', 'playerName', 'tierLabel', 'modeNote', 'gateNotice', 'ended', 'offline', 'notice', 'openingNotice',
    'items', 'hasEarlier', 'jumpVisible', 'scroller', 'input', 'draft', 'actionError', 'draftCount', 'draftOver', 'actionMax', 'sending', 'sendDisabled',
    'inputDisabled', 'blockerText', 'world', 'locationLine', 'balanceText', 'lowBalanceText', 'sidebarOpen', 'sidebarMode', 'sideTab', 'modal', 'coinModalOpen',
    'coinModalText', 'checkinBusy', 'checkinPreview', 'checkinMessage', 'checkinFinished',
    // 記事簿面板（game-notebook.js）
    'notebook', 'notebookLoading', 'notebookError', 'notebookEmpty', 'notebookUsage', 'notebookReserveNote', 'notebookFull', 'notebookFullText', 'notebookEmptyText',
    'notebookStale', 'notebookEditing', 'notebookDraft', 'notebookDraftCount', 'notebookOver', 'notebookOverNote', 'notebookEditHint', 'notebookSaveHint', 'notebookCanEdit',
    'notebookCanSave', 'notebookLocked', 'notebookSaving', 'notebookNotice', 'notebookConflict', 'notebookDiscardOpen',
  ],
  methods: [
    'retryLoad', 'retryOpening', 'showEarlier', 'jumpToLatest', 'onScroll', 'send', 'cancel', 'dismissNotice', 'resendInflight', 'askAbandon',
    'confirmAbandon', 'leaveClick', 'confirmLeave', 'toggleSidebar', 'closeSidebar', 'selectSideTab', 'claimCheckin', 'confirmOverflow', 'cancelOverflow',
    'closeCoinModal', 'openNotebook', 'reloadNotebook', 'startNotebookEdit', 'saveNotebook', 'cancelNotebookEdit', 'confirmNotebookDiscard',
  ],
};
const CONTRACT_NAMES = [].concat(CONTRACT.state, CONTRACT.methods).sort();

module.exports = { CONTRACT, CONTRACT_NAMES };
