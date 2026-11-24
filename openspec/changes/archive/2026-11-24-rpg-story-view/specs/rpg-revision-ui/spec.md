# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: 最後一輪的氣泡下方才有重新生成與編輯`
- TO: `### Requirement: 最後一輪的劇情卡下方才有重新生成與編輯`

- FROM: `### Requirement: 修訂進行中的氣泡與失敗還原`
- TO: `### Requirement: 修訂進行中的劇情卡與失敗還原`

## MODIFIED Requirements

### Requirement: 最後一輪的劇情卡下方才有重新生成與編輯
劇情卡 SHALL 只在當前顯示的是最後一個完成的回合（依存檔快照的最後一輪資訊）時，於卡片下方顯示「重新生成」與「編輯」；其他回合、第 0 幕、被擋與中斷的轉場，以及「歷史」中的任何內容 SHALL NOT 有這兩個按鈕。存檔已完結、離線、正在串流或修訂中、後端回報不可修訂時，按鈕 SHALL 隱藏或停用並說明原因。按鈕的顯示與停用 SHALL 由 Vue 的響應式變數接管。

#### Scenario: 只有最後一輪
- **WHEN** 存檔有三個完成的回合，主畫面顯示第三輪
- **THEN** 劇情卡下方有兩個按鈕；歷史中的各輪沒有

#### Scenario: 第 0 幕
- **WHEN** 尚無完成的回合，主畫面顯示第 0 幕
- **THEN** 沒有這兩個按鈕

#### Scenario: 串流中
- **WHEN** 新的一輪正在串流
- **THEN** 最後一輪的按鈕不可用

#### Scenario: 已完結
- **WHEN** 存檔已完結
- **THEN** 沒有這兩個按鈕，畫面照常說明存檔已完結

#### Scenario: 離線
- **WHEN** 瀏覽器離線
- **THEN** 按鈕停用並說明需要連線

### Requirement: 修訂進行中的劇情卡與失敗還原
修訂串流期間，劇情卡 SHALL 以新文字從第一段開始逐字顯示（見 `rpg-story-pages`），舊內容與原本所在的段 SHALL 保留在記憶體中。`done` 為 `complete`／`ended` 時 SHALL 以後端內容覆寫；`done` 為 `blocked`、串流出錯、逾時、中斷或玩家取消時 SHALL 還原舊內容，並以通知說明「已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數」。無論結果，`finally` SHALL 恢復按鈕與輸入狀態。被擋的轉場文字 SHALL 與新回合相同地以一行顯示後消失。

#### Scenario: 成功
- **WHEN** 重新生成成功
- **THEN** 劇情卡顯示新內容的第一段，剩餘免費次數與餘額依 `done` 更新

#### Scenario: 被擋
- **WHEN** 重新生成最終被擋
- **THEN** 劇情卡還原為舊內容與原本所在的段並顯示說明，按鈕恢復可用

#### Scenario: 中途離開頁面
- **WHEN** 串流中離開頁面
- **THEN** 請求被中止，下次進入時畫面以後端內容為準

### Requirement: 修訂介面遵守前端規範
修訂的 Modal、通知與按鈕狀態 SHALL 由 Vue 響應式變數接管，樣式沿用現有電影感 Modal 與 Toast；SHALL NOT 使用 `data-bs-*` 或 Bootstrap JavaScript；使用者文字（行動、理由）與 AI 劇情 SHALL 一律以純文字渲染，嚴禁 `v-html`；所有文字為繁體中文；不載入任何外部資源。

#### Scenario: 純文字
- **WHEN** 編輯輸入框或劇情卡含 `<b>` 字樣
- **THEN** 以文字顯示，不被解析為標籤
