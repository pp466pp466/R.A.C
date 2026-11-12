## Purpose
定義遊戲畫面中玩家重新生成與編輯最後一輪的互動：按鈕何時出現與可用、理由 Modal 與編輯確認 Modal、剩餘免費次數與費用的呈現、串流中的氣泡與失敗時的還原、結果通知，以及快取覆寫與在途紀錄。全部遵守前端離線與 Vue 狀態接管規範。

## ADDED Requirements

### Requirement: 最後一輪的氣泡下方才有重新生成與編輯
時間軸 SHALL 只在最後一個完成的回合（依存檔快照的最後一輪資訊）的氣泡下方顯示「重新生成」與「編輯」；其他回合、第 0 幕、被擋與中斷的轉場 SHALL NOT 有這兩個按鈕。存檔已完結、離線、正在串流或修訂中、後端回報不可修訂時，按鈕 SHALL 隱藏或停用並說明原因。按鈕的顯示與停用 SHALL 由 Vue 的響應式變數接管。

#### Scenario: 只有最後一輪
- **WHEN** 時間軸有三個完成的回合
- **THEN** 只有第三個回合的氣泡下方有兩個按鈕

#### Scenario: 串流中
- **WHEN** 新的一輪正在串流
- **THEN** 最後一輪的按鈕不可用

#### Scenario: 已完結
- **WHEN** 存檔已完結
- **THEN** 沒有這兩個按鈕，畫面照常說明存檔已完結

#### Scenario: 離線
- **WHEN** 瀏覽器離線
- **THEN** 按鈕停用並說明需要連線

### Requirement: 費用與剩餘免費次數的呈現
「重新生成」旁 SHALL 顯示剩餘免費次數（例如「免費還剩 2 次」）；用完後 SHALL 改為顯示一輪的幣價。「編輯」旁 SHALL 顯示一輪的幣價。幣價 SHALL 取自錢包回應的每輪幣價，不寫死；缺值時不顯示數字。

#### Scenario: 還有免費次數
- **WHEN** 最後一輪還有 2 次免費重新生成
- **THEN** 按鈕旁顯示「免費還剩 2 次」

#### Scenario: 免費次數用完
- **WHEN** 已用完免費次數
- **THEN** 按鈕旁顯示幣價（例如「30 幣」）

### Requirement: 重新生成的理由 Modal
按下「重新生成」SHALL 先跳出自製 Modal（由 Vue 的 `v-if` 接管，不使用 Bootstrap 的 JavaScript）：必須選一個分類並填 5 至 50 字的理由才能送出，顯示字數；Modal SHALL 說明「理由會提供營運團隊檢視，不會送給 AI」。送出鈕在條件未滿足或請求進行中 SHALL 停用。理由與分類 SHALL NOT 寫入 IndexedDB、localStorage、主控台或日誌。取消 SHALL 不送出任何請求。

#### Scenario: 條件未滿足
- **WHEN** 沒選分類或理由不足 5 字
- **THEN** 送出鈕停用

#### Scenario: 送出
- **WHEN** 分類與理由都符合後按送出
- **THEN** Modal 關閉並開始重新生成的串流，請求帶 `expected_revision`

#### Scenario: 理由不被暫存
- **WHEN** 重新生成進行中或完成後檢查 IndexedDB 與儲存
- **THEN** 其中沒有理由與分類

### Requirement: 編輯的確認 Modal
按下「編輯」SHALL 跳出自製 Modal：以原行動預填、可修改的輸入框，提醒「會再次消耗遊戲幣、原本那輪已扣的幣不退、免費重新生成次數會歸零」，並顯示目前餘額與費用。餘額不足、文字為空、超過字數上限、或與原行動相同時，送出鈕 SHALL 停用。送出前的字數與空白檢查 SHALL 與送出新行動一致。

#### Scenario: 餘額不足
- **WHEN** 餘額低於一輪的幣價
- **THEN** 送出鈕停用，Modal 說明餘額不足並提供簽到入口

#### Scenario: 沒有改動
- **WHEN** 輸入框的文字與原行動相同
- **THEN** 送出鈕停用

### Requirement: 修訂進行中的氣泡與失敗還原
修訂串流期間，最後一輪的氣泡 SHALL 以打字機顯示新文字，舊內容 SHALL 保留在記憶體中。`done` 為 `complete`／`ended` 時 SHALL 以後端內容覆寫；`done` 為 `blocked`、串流出錯、逾時、中斷或玩家取消時 SHALL 還原舊內容，並以通知說明「已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數」。無論結果，`finally` SHALL 恢復按鈕與輸入狀態。被擋的轉場文字 SHALL 與新回合相同地以一行顯示後消失。

#### Scenario: 成功
- **WHEN** 重新生成成功
- **THEN** 氣泡顯示新文字，剩餘免費次數與餘額依 `done` 更新

#### Scenario: 被擋
- **WHEN** 重新生成最終被擋
- **THEN** 氣泡還原為舊內容並顯示說明，按鈕恢復可用

#### Scenario: 中途離開頁面
- **WHEN** 串流中離開頁面
- **THEN** 請求被中止，下次進入時畫面以後端內容為準

### Requirement: 修訂的快取覆寫與在途紀錄
修訂結束後（不論結果），前端 SHALL 從最後一個完成回合的前一個序號起增量重抓並以後端內容覆寫快取，使其他裝置與過期的 IndexedDB 內容被修正。回合視圖帶 `revision`，相同序號但 `revision` 或內容不同 SHALL 視為已變更。在途紀錄 SHALL 記錄種類與 `expected_revision`（編輯另含新行動，重新生成不含理由）；重新整理後若快取的回合 `revision` 已大於 `expected_revision`，視為已成功並清除紀錄，否則清除紀錄並通知「上次沒有完成，原本的回應已保留」。修訂 SHALL NOT 自動排隊重送，也不提供「重送」。

#### Scenario: 其他裝置已修訂
- **WHEN** 裝置乙的快取是舊的，裝置甲已重新生成成功，乙重新整理頁面
- **THEN** 乙的最後一輪顯示甲的新內容

#### Scenario: 串流中重新整理
- **WHEN** 重新生成串流中重新整理頁面，後端已成功替換
- **THEN** 在途紀錄被清除，畫面顯示新內容與剩餘免費次數

#### Scenario: 串流中重新整理且後端沒有替換
- **WHEN** 重新整理後後端的 `revision` 仍等於 `expected_revision`
- **THEN** 在途紀錄被清除，畫面通知上次沒有完成，舊內容與按鈕保持可用

### Requirement: 修訂介面遵守前端規範
修訂的 Modal、通知與按鈕狀態 SHALL 由 Vue 響應式變數接管，樣式沿用現有電影感 Modal 與 Toast；SHALL NOT 使用 `data-bs-*` 或 Bootstrap JavaScript；使用者文字（行動、理由）與 AI 劇情 SHALL 一律以純文字渲染，嚴禁 `v-html`；所有文字為繁體中文；不載入任何外部資源。

#### Scenario: 純文字
- **WHEN** 編輯輸入框或氣泡含 `<b>` 字樣
- **THEN** 以文字顯示，不被解析為標籤
