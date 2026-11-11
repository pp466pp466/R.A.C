# story-preference Specification

## Purpose
定義玩家的故事偏好（男頻、女頻、全讀者）：值域與預設、查詢與更新的 API、帳號頁的設定區塊，以及它唯一的作用——決定劇本清單的排序。偏好不影響內容分級、敘事、權限與額度。

## Requirements

### Requirement: 故事偏好的值域與預設
每位使用者 SHALL 有一個故事偏好，值只能是 `all`（全讀者）、`male`（男頻）、`female`（女頻）之一。新帳號與遷移之前就存在的帳號 SHALL 都是 `all`。偏好 SHALL NOT 在任何情況下被系統自動改變（包含年齡確認、內容模式切換與滿 18 歲）。

#### Scenario: 新帳號的預設
- **WHEN** 玩家以 Google 首次登入並建立帳號
- **THEN** 其故事偏好為 `all`

#### Scenario: 既有帳號
- **WHEN** 遷移之前就存在的帳號查詢故事偏好
- **THEN** 得到 `all`

#### Scenario: 不被其他流程改變
- **WHEN** 玩家完成年齡確認，或切換內容模式
- **THEN** 故事偏好維持原值

### Requirement: 故事偏好的查詢與更新 API
`GET /api/account/story-preference` SHALL 回傳目前的偏好；`PUT /api/account/story-preference` SHALL 接收 `{"story_preference": <值>}` 並回傳更新後的偏好。兩者 SHALL 要求已登入（受限登入狀態一律拒絕），PUT 須通過 CSRF 與來源檢查，回應 SHALL 禁止快取且為 JSON。值不是三個列舉值之一（含非字串、空值、大小寫不同）SHALL 以 400 與欄位錯誤回應，偏好不變。更新 SHALL 為冪等：設成目前的值同樣成功。回應 SHALL NOT 含生日、年齡區間或其他個資。

#### Scenario: 更新成功
- **WHEN** 已登入的玩家 PUT `{"story_preference": "female"}`
- **THEN** 回應 200，內容為 `female`；之後 GET 也得到 `female`

#### Scenario: 不合法的值
- **WHEN** PUT `{"story_preference": "Male"}` 或 `{"story_preference": 1}` 或沒有該欄位
- **THEN** 回應 400 並指出 `story_preference` 欄位，偏好不變

#### Scenario: 未登入
- **WHEN** 沒有登入就請求
- **THEN** 回應 401

#### Scenario: 受限登入狀態
- **WHEN** 帳號被鎖定、等待刪除或尚未完成年齡確認的玩家請求
- **THEN** 依既有的受限登入規則回 403（`account_locked`、`account_deletion_pending`、`age_confirmation_required`），偏好不變

#### Scenario: 缺少 CSRF
- **WHEN** PUT 沒有帶有效的 CSRF 標頭
- **THEN** 被拒絕，偏好不變

#### Scenario: 只能改自己的
- **WHEN** 玩家 A 更新偏好
- **THEN** 玩家 B 的偏好不受影響（請求中沒有任何可指定他人的欄位）

### Requirement: 偏好只影響清單排序
故事偏好 SHALL 只影響劇本清單的排序，SHALL NOT 影響內容分級的生效模式、可見或可玩的劇本集合、敘事風格、模型提示詞、遊戲幣與額度，也 SHALL NOT 進入任何傳給模型的內容。

#### Scenario: 不改變可見的劇本
- **WHEN** 玩家把偏好從 `all` 改為 `male`
- **THEN** 其可看見與可開存檔的劇本集合與改之前完全相同，只有順序可能不同

#### Scenario: 不改變生效的內容模式
- **WHEN** 玩家改變故事偏好
- **THEN** 其生效的內容模式不變

### Requirement: 劇本清單的排序規則
依偏好排序的劇本清單 SHALL 依下列順序分層：偏好為 `male` 時 男頻、全讀者、女頻；偏好為 `female` 時 女頻、全讀者、男頻；偏好為 `all` 時 SHALL 不重排（所有劇本同一層）。版本的讀者頻道為空值 SHALL 視同全讀者。同一層內 SHALL 維持既有順序（發佈時間由新到舊）。排序 SHALL NOT 隱藏或排除任何劇本，且 SHALL 在分頁之前套用，使各頁的順序一致。

#### Scenario: 偏好男頻
- **WHEN** 偏好為 `male`，上架的劇本有女頻、全讀者、男頻各一份
- **THEN** 清單順序為男頻、全讀者、女頻

#### Scenario: 偏好女頻
- **WHEN** 偏好為 `female`，同樣三份劇本
- **THEN** 清單順序為女頻、全讀者、男頻

#### Scenario: 偏好全讀者不重排
- **WHEN** 偏好為 `all`
- **THEN** 清單順序與沒有偏好時完全相同（發佈時間由新到舊）

#### Scenario: 同層維持新到舊
- **WHEN** 偏好為 `male`，有兩份男頻劇本，較晚發佈者
- **THEN** 排在較早發佈者之前

#### Scenario: 空值視同全讀者
- **WHEN** 偏好為 `female`，有一份頻道為空值的舊劇本與一份男頻劇本
- **THEN** 空值的舊劇本排在男頻劇本之前

#### Scenario: 分頁一致
- **WHEN** 偏好為 `male`，清單分兩頁取得
- **THEN** 兩頁串起來的順序與一次取得全部相同，沒有重複或遺漏

#### Scenario: 不隱藏劇本
- **WHEN** 偏好為 `male`
- **THEN** 女頻劇本仍在清單中，只是排在最後

### Requirement: 帳號頁的故事偏好設定
`/account` 對完成年齡確認且非等待刪除的帳號 SHALL 顯示「故事偏好」區塊：三個單選項（全讀者、男頻、女頻，使用繁體中文標籤）、目前的選取狀態與一句說明（只影響劇本清單的排序、不限制可看的內容），以及儲存按鈕。儲存期間按鈕 SHALL 禁用並標示忙碌，且於 `finally` 恢復；成功以 Toast 回報，失敗以 Toast 或欄位錯誤顯示繁體中文說明且保留玩家目前的選取。介面 SHALL 以 Vue 全域變數模式編寫，資源來自本機，不使用 Bootstrap 的 JavaScript 外掛。

#### Scenario: 顯示目前值
- **WHEN** 偏好為 `female` 的玩家開啟 `/account`
- **THEN** 「女頻」被選取

#### Scenario: 儲存成功
- **WHEN** 玩家改選「男頻」並按儲存，伺服器回應成功
- **THEN** 以 Toast 回報，重新整理後仍是「男頻」

#### Scenario: 儲存失敗
- **WHEN** 儲存請求失敗
- **THEN** 以 Toast 顯示繁體中文說明，按鈕恢復可用，玩家的選取不被重設

#### Scenario: 等待刪除的帳號沒有此區塊
- **WHEN** 帳號正在等待刪除的人開啟 `/account`
- **THEN** 頁面沒有故事偏好區塊
