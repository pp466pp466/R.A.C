'use strict';
/* 遊戲主畫面的樣板與 game.js 的約定（tests/js/game-template.test.js 與 tests/js/game.test.js 共用，不是測試檔）：
 * 樣板用到的每個名稱，也是 game.js 的 setup() 必須回傳的名稱；兩邊的測試用同一份清單，所以不會各改各的。 */
const CONTRACT = {
  state: [
    'phase', 'loadFailure', 'restriction', 'title', 'playerName', 'modeLabel', 'modeNote', 'gateNotice', 'ended', 'offline', 'notice', 'safeHint', 'openingNotice',
    'items', 'hasEarlier', 'scroller', 'input', 'draft', 'actionError', 'draftCount', 'draftOver', 'actionMax', 'sending', 'sendDisabled',
    'inputDisabled', 'blockerText', 'world', 'locationLine', 'lowBalanceText', 'sidebarOpen', 'sidebarMode', 'sideTab', 'modal', 'coinModalOpen',
    'coinModalText', 'checkinBusy', 'checkinPreview', 'checkinMessage', 'checkinFinished',
    // 劇情卡、行動建議與直接推進劇情（game-session.js，rpg-story-view）
    // 劇情朗讀（rpg-story-tts）：speech 是朗讀器的狀態；voiceMenuButton 與 voiceList 是元素參照（選單按鈕、選單裡的選項清單）
    'speech', 'speakDisabled', 'speakHint', 'voiceMenuButton', 'voiceList',
    'card', 'suggestOpen', 'suggestItems', 'suggestBusy', 'suggestError', 'suggestDisabled', 'suggestHint', 'advanceDisabled', 'advanceHint',
    // 記事簿面板（game-notebook.js）
    'notebook', 'notebookLoading', 'notebookError', 'notebookEmpty', 'notebookUsage', 'notebookReserveNote', 'notebookFull', 'notebookFullText', 'notebookEmptyText',
    'notebookStale', 'notebookEditing', 'notebookDraft', 'notebookDraftCount', 'notebookOver', 'notebookOverNote', 'notebookEditHint', 'notebookSaveHint', 'notebookCanEdit',
    'notebookCanSave', 'notebookLocked', 'notebookSaving', 'notebookNotice', 'notebookConflict', 'notebookDiscardOpen',
    // 重新生成與編輯最後一輪（game-revision.js）
    'revisionBusy', 'reviseKey', 'reviseDisabled', 'reviseHint', 'regenerateNote', 'editNote', 'regenerateOpen', 'regenerateOptions', 'regenerateCategory',
    'regenerateReason', 'regenerateRule', 'regenerateCount', 'regenerateError', 'regenerateCanSubmit', 'regenerateCostLine', 'editOpen', 'editDraft', 'editCount',
    'editHint', 'editError', 'editCanSubmit', 'editInsufficient', 'editCostLine',
  ],
  methods: [
    'retryLoad', 'retryOpening', 'showEarlier', 'onScroll', 'send', 'cancel', 'dismissNotice', 'dismissSafeHint', 'resendInflight', 'askAbandon',
    'confirmAbandon', 'leaveClick', 'confirmLeave', 'toggleSidebar', 'closeSidebar', 'selectSideTab', 'claimCheckin', 'confirmOverflow', 'cancelOverflow',
    'closeCoinModal', 'openNotebook', 'reloadNotebook', 'startNotebookEdit', 'saveNotebook', 'cancelNotebookEdit', 'confirmNotebookDiscard',
    'speakCurrent', 'openVoiceMenu', 'closeVoiceMenu', 'chooseAutoVoice', 'chooseVoiceByKey', 'previewVoice',
    'nextPage', 'prevPage', 'advance', 'toggleSuggestions', 'pickSuggestion', 'openHistory', 'closeHistory',
    'openRegenerate', 'openEdit', 'closeRegenerate', 'closeEdit', 'selectRegenerateCategory', 'submitRegenerate', 'submitEdit', 'cancelRevision',
  ],
};
const CONTRACT_NAMES = [].concat(CONTRACT.state, CONTRACT.methods).sort();

module.exports = { CONTRACT, CONTRACT_NAMES };
