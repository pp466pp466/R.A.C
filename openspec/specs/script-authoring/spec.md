# script-authoring Specification

## Purpose
定義玩家創作劇本的 API 與 `/scripts` 頁面：建立與編輯草稿、以內容雜湊偵測編輯衝突、由既有版本建立新草稿、刪除與撤回、重新取用暱稱作為筆名、範例與額度查詢，以及作者只能存取自己的劇本。送審、審查與審核分別由 `script-submission`、`script-review` 與後續的發佈 change 處理。

## Requirements

### Requirement: 創作端點只開放給已登入且完成年齡確認的作者本人
`/api/scripts/*` 與 `/scripts` SHALL 需要登入並先通過帳號與年齡閘門；受限登入狀態（鎖定、等待刪除）SHALL 被拒絕。所有讀寫 SHALL 帶擁有者條件，他人的劇本與版本一律得到 404，與不存在相同；回應 SHALL 禁止快取。

#### Scenario: 未登入
- **WHEN** 未登入者呼叫任一創作端點
- **THEN** 回應 401

#### Scenario: 未完成年齡確認
- **WHEN** 尚未完成年齡確認的玩家呼叫創作端點
- **THEN** 回應 403 與代碼 `age_confirmation_required`

#### Scenario: 他人的劇本
- **WHEN** 玩家讀取、更新或刪除另一位玩家的版本
- **THEN** 一律得到 404，與不存在的版本相同

### Requirement: 建立劇本
`POST /api/scripts` SHALL 以固定規格驗證內容，通過時建立劇本與第 1 版草稿，並以目前的暱稱與短代號作為筆名快照。沒有設定暱稱 SHALL 得到 409 與代碼 `nickname_required`；已達劇本數量上限 SHALL 得到 409 與代碼 `script_limit_reached`；內容不合格 SHALL 得到逐欄位的錯誤。回應 SHALL 含劇本與版本的對外識別碼，不含內部編號。

#### Scenario: 建立成功
- **WHEN** 已設定暱稱的作者提交合格的內容
- **THEN** 建立劇本與第 1 版草稿，筆名為「暱稱」與短代號的快照

#### Scenario: 沒有暱稱
- **WHEN** 沒有暱稱的作者建立劇本
- **THEN** 回應 409 與代碼 `nickname_required`，沒有建立任何資料

#### Scenario: 達到數量上限
- **WHEN** 作者的劇本數已等於其會員等級的上限
- **THEN** 回應 409 與代碼 `script_limit_reached`，沒有建立任何資料

#### Scenario: 內容不合格
- **WHEN** 提交的內容有多個欄位不合格
- **THEN** 一次回報全部欄位的錯誤，錯誤不回顯輸入的內容

### Requirement: 宣告的內容模式不得高於作者的上限
劇本宣告的 `content_tier` SHALL NOT 高於作者目前的年齡上限（由生日於當下計算）：未滿 18 歲的作者只能宣告 `safe`；已滿 18 歲的作者可宣告到 `adult`，不另要求作者本人開啟成人模式（誰能遊玩由玩家的生效模式決定）。超過時 SHALL 以 `content_tier` 欄位的錯誤回應。

#### Scenario: 未成年作者宣告一般模式
- **WHEN** 15 歲的作者提交宣告為 `standard` 的內容
- **THEN** 被拒絕，錯誤在 `content_tier` 欄位

#### Scenario: 成年作者宣告成人模式
- **WHEN** 25 歲、偏好為一般模式的作者宣告 `adult`
- **THEN** 通過驗證

### Requirement: 更新草稿以內容雜湊偵測衝突
`PUT /api/scripts/versions/<id>` SHALL 只接受 `draft` 版本，本文帶完整內容與 `base_hash`（作者開始編輯時的內容雜湊）。`base_hash` 與目前的內容雜湊不同 SHALL 得到 409 與代碼 `draft_changed`，不覆寫；非草稿 SHALL 得到 409 與代碼 `not_draft`。成功時 SHALL 回傳新的內容雜湊。內容相同的重送 SHALL 成功且不改變資料。

#### Scenario: 正常更新
- **WHEN** 作者以最新的 `base_hash` 提交修改
- **THEN** 內容更新並回傳新的雜湊

#### Scenario: 兩個分頁
- **WHEN** 另一個分頁已更新過草稿，這個分頁以舊的 `base_hash` 提交
- **THEN** 回應 409 與代碼 `draft_changed`，內容不被覆寫

#### Scenario: 已送審的版本
- **WHEN** 作者更新狀態為 `pending_review` 的版本
- **THEN** 回應 409 與代碼 `not_draft`

### Requirement: 由既有版本建立新草稿
`POST /api/scripts/<id>/versions` SHALL 以劇本最新一個非草稿版本（已發佈、被退回或被取代）的內容建立下一個草稿版本，並沿用里程碑的 `id`。劇本已有進行中的版本（草稿或待審）SHALL 得到 409 與代碼 `open_version_exists`。已發佈的版本 SHALL 保持不變。

#### Scenario: 由被退回的版本修改
- **WHEN** 作者對最新版被退回的劇本建立新草稿
- **THEN** 新草稿的內容等於被退回版本，版本編號加 1

#### Scenario: 已有進行中的版本
- **WHEN** 劇本已有一個草稿時再建立新草稿
- **THEN** 回應 409 與代碼 `open_version_exists`

### Requirement: 只有草稿可以刪除，待審可以撤回
`DELETE /api/scripts/versions/<id>` SHALL 只刪除自己的草稿版本（那是劇本唯一的版本時連劇本一起刪除），非草稿 SHALL 得到 409。`POST /api/scripts/versions/<id>/withdraw` SHALL 把自己的 `pending_review` 版本退回 `draft`。已被別人的存檔參照的版本被刪除時，存檔 SHALL 保留。

#### Scenario: 刪除草稿
- **WHEN** 作者刪除自己的草稿
- **THEN** 版本消失，回應成功

#### Scenario: 刪除已發佈的版本
- **WHEN** 作者嘗試刪除已發佈的版本
- **THEN** 回應 409，版本不變

#### Scenario: 撤回
- **WHEN** 作者撤回待審的版本
- **THEN** 版本回到草稿，可以再編輯

### Requirement: 重新取用暱稱作為筆名
`POST /api/scripts/<id>/author-name/refresh` SHALL 在劇本從未發佈時，把筆名與短代號更新為目前的暱稱與短代號。筆名已在第一次發佈時鎖定 SHALL 得到 409 與代碼 `author_name_locked`。更改暱稱本身 SHALL NOT 自動改變任何劇本的筆名。

#### Scenario: 發佈前重新取用
- **WHEN** 作者改了暱稱後，對從未發佈的劇本重新取用
- **THEN** 該劇本的筆名更新為新的暱稱

#### Scenario: 不自動更新
- **WHEN** 作者只更改暱稱
- **THEN** 既有劇本的筆名快照不變

#### Scenario: 鎖定後
- **WHEN** 作者對筆名已鎖定的劇本重新取用
- **THEN** 回應 409 與代碼 `author_name_locked`

### Requirement: 範例與額度查詢
`GET /api/scripts/example` SHALL 回傳內建首發劇本的標準化內容（不含識別碼與雜湊），供編輯器帶入欄位範例。`GET /api/scripts/limits` SHALL 回傳作者自己的劇本數與上限、今日送審次數與上限、是否被暫停發佈，不含其他玩家的資料。

#### Scenario: 帶入範例
- **WHEN** 作者請求範例
- **THEN** 回傳的內容通過固定規格驗證

#### Scenario: 額度
- **WHEN** 有 2 個劇本、今日送審 1 次的免費作者查詢額度
- **THEN** 回應含劇本數 2 與其上限、今日送審 1 與其上限

### Requirement: 創作頁面的行為
`/scripts` SHALL 以 Vue 全域變數模式編寫、資源全部來自本機，列出自己的劇本與版本狀態，提供編輯器（目錄資訊、背景與體系、角色、地點、里程碑、結局、初始對話、標籤、讀者頻道）、欄位範例帶入與里程碑預覽（純前端、依觸發排序，不呼叫 API）。讀者頻道 SHALL 位於編輯器最下方，為男頻、女頻、全讀者的單選，預設全讀者，並以一句說明告知它只影響劇本在清單中的排序、不限制誰能玩；唯讀的版本（待審、已發佈等）SHALL 顯示但不可更改。帶入範例、載入既有版本與儲存 SHALL 保留頻道的選取；沒有頻道的舊版本與範例 SHALL 顯示為全讀者。刪除與送審 SHALL 先以自製 Modal 確認；送出期間按鈕 SHALL 禁用並在 `finally` 恢復；被暫停發佈或使用中被鎖定時，頁面 SHALL 顯示「前往申訴」按鈕（見 `appeal-shortcut`）。所有使用者文字 SHALL 以純文字渲染，不使用 `v-html`。

#### Scenario: 里程碑預覽
- **WHEN** 作者新增三個不同觸發的里程碑
- **THEN** 預覽依觸發先後列出，且沒有任何網路請求

#### Scenario: 送審前的確認
- **WHEN** 作者按下送審
- **THEN** 先顯示自製 Modal 說明內容將被凍結，確認後才送出請求

#### Scenario: 請求失敗
- **WHEN** 儲存請求失敗
- **THEN** 以 Toast 顯示繁體中文說明，按鈕恢復可用，編輯中的內容不遺失

#### Scenario: 預設全讀者
- **WHEN** 作者開新劇本
- **THEN** 讀者頻道單選預設為全讀者

#### Scenario: 選擇頻道並儲存
- **WHEN** 作者選「女頻」並儲存草稿
- **THEN** 送出的內容帶有 `audience: "female"`，重新載入該版本後仍選取「女頻」

#### Scenario: 舊版本顯示全讀者
- **WHEN** 作者載入一個沒有頻道的舊版本
- **THEN** 單選顯示「全讀者」

#### Scenario: 帶入範例後的頻道
- **WHEN** 作者選了「男頻」後帶入範例並確認
- **THEN** 內容被範例取代，頻道依範例顯示為全讀者

#### Scenario: 唯讀版本不可改頻道
- **WHEN** 作者檢視待審或已發佈的版本
- **THEN** 讀者頻道單選為禁用狀態

### Requirement: 劇本內容是個資且不進日誌
創作流程的日誌與事件 SHALL 只含事件名稱、內部編號、整數長度與固定列舉的類別，SHALL NOT 含劇本的任何文字、標題、標籤、筆名或暱稱。

#### Scenario: 日誌掃描
- **WHEN** 建立、更新、刪除、撤回與送審的整個流程收集日誌
- **THEN** 日誌中找不到劇本文字、標題、標籤與筆名
