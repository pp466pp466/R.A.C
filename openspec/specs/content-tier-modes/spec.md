# content-tier-modes Specification

## Purpose
定義內容程度的三種模式（安全、一般、成人）：它們的順序、玩家偏好與預設、年齡上限、實際生效模式的計算、成人模式的可用條件與成年確認的紀錄、部署層的設定與已淘汰的舊變數，以及讓玩家查詢與切換的 API。取代原本以 `sexual_content_level` 0 至 3 表示的分級。

## Requirements

### Requirement: 兩種內容模式與順序
內容模式 SHALL 只有 `safe`（安全模式開）與 `adult`（安全模式關）兩種，`safe` 較保守；兩個模式「取較保守者」SHALL 以此順序決定。未指定或無法判定時 SHALL 取最保守的 `safe`。舊值 `standard`（原一般模式）SHALL 一律讀作 `adult`，不論出現在玩家偏好、劇本內容或其他儲存的資料；系統 SHALL NOT 再寫入 `standard`。

#### Scenario: 順序
- **WHEN** 比較 `safe` 與 `adult`
- **THEN** `safe` 較保守

#### Scenario: 無法判定
- **WHEN** 內容模式的輸入缺少或不是兩種之一（且不是舊值 `standard`）
- **THEN** 視為 `safe`

#### Scenario: 舊值讀作成人
- **WHEN** 讀取儲存的 `standard`
- **THEN** 結果為 `adult`

#### Scenario: 不再寫入舊值
- **WHEN** 任何流程儲存內容模式
- **THEN** 寫入的值只會是 `safe` 或 `adult`

### Requirement: 內容模式的查詢與切換 API
系統 SHALL 提供需要登入的 `GET /api/account/content-mode` 與 `PUT /api/account/content-mode`，所有非 GET 請求 SHALL 通過 CSRF 檢查。回應 SHALL 只含：`preference`（`safe` 或 `adult`）、`effective`（生效模式，不含劇本）、`confirmed`（是否已在目前版本同意）與 `forced`（緊急煞車是否啟用）；SHALL NOT 含生日、年齡區間、年齡確認時間、同意的時間戳記或內部編號。`mode` 只接受 `safe` 或 `adult`，其他值（含 `standard`）SHALL 得到 400。緊急煞車啟用時要求 `adult` SHALL 得到 403 與代碼 `safe_mode_forced`，要求 `safe` 仍可成功。尚未完成年齡確認者得 403 `age_confirmation_required`，未登入得 401，被鎖定或待刪除的帳號依既有的受限登入規則被拒絕。變更成功 SHALL 記錄結構化事件，內容只含事件名稱、使用者編號與新的模式列舉值。

#### Scenario: 查詢
- **WHEN** 玩家查詢
- **THEN** 回應只含 `preference`、`effective`、`confirmed`、`forced`，不含生日或年齡區間

#### Scenario: 未登入
- **WHEN** 未登入者查詢
- **THEN** 回應 401

#### Scenario: 尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的使用者查詢或切換
- **THEN** 回應 403 與代碼 `age_confirmation_required`

#### Scenario: 不合法的模式
- **WHEN** 切換為 `standard` 或 `mature`
- **THEN** 回應 400，偏好不變

#### Scenario: 緊急煞車時要求關閉
- **WHEN** 緊急煞車啟用，玩家要求 `adult`
- **THEN** 回應 403 與代碼 `safe_mode_forced`，偏好不變

#### Scenario: 緊急煞車時切回安全
- **WHEN** 緊急煞車啟用，玩家要求 `safe`
- **THEN** 成功，偏好為 `safe`

#### Scenario: 缺少 CSRF 權杖
- **WHEN** 沒有 CSRF 權杖的 PUT
- **THEN** 被 CSRF 檢查拒絕

#### Scenario: 變更紀錄不含內容
- **WHEN** 成功切換模式後檢視日誌
- **THEN** 有一筆事件，只含事件名稱、使用者編號與新的模式，沒有同意勾選的文字或其他個資

### Requirement: 舊的等級 3 環境變數已淘汰且不會開啟任何東西
`SEXUAL_CONTENT_LEVEL3_ENABLED`、`SEXUAL_CONTENT_LEVEL3_MODELS`、`ADULT_MODE_ENABLED` 與 `ADULT_MODE_MODELS` SHALL 被忽略：不論值為何，安全模式 SHALL NOT 因它們而改變，也 SHALL NOT 因它們而啟動失敗。環境中出現其中任何一個 SHALL 在啟動時記錄一則警告，只指出變數名稱與已淘汰，SHALL NOT 回顯值。`.env.example` SHALL NOT 再列出它們，部署計畫檔含有它們 SHALL 被拒絕。

#### Scenario: 舊變數為開啟
- **WHEN** 環境設定了 `SEXUAL_CONTENT_LEVEL3_ENABLED=true` 或 `ADULT_MODE_ENABLED=true`
- **THEN** 緊急煞車與玩家可用的模式都不受影響，啟動時有一則警告，只含變數名稱

#### Scenario: 舊變數不會讓啟動失敗
- **WHEN** 在 production 環境殘留 `ADULT_MODE_ENABLED=maybe`
- **THEN** 應用程式可啟動，警告不含 `maybe`

#### Scenario: 部署計畫檔
- **WHEN** 部署計畫檔含有 `ADULT_MODE_MODELS` 或 `SEXUAL_CONTENT_LEVEL3_MODELS`
- **THEN** 計畫被拒絕並指出該變數已淘汰

### Requirement: 關閉安全模式的同意與紀錄
玩家 SHALL 自行在個人頁關閉安全模式（偏好改為 `adult`）；需要的條件 SHALL 只有：已完成年齡確認、緊急煞車未啟用、並在目前版本的安全規範下同意（`confirm_rules` 為明確的真）。同意成功 SHALL 記錄確認時間、安全規範的版本與確認方式（自行聲明），並 SHALL NOT 要求另外勾選年滿 18 歲（全站皆成年）。安全規範的版本改變後，先前的同意 SHALL 失效、須重新同意。回到 `safe` SHALL NOT 清除紀錄。已在目前版本同意的玩家再次關閉 SHALL 不需要重新勾選。系統 SHALL NOT 蒐集證件或接第三方驗證。

#### Scenario: 首次關閉
- **WHEN** 玩家送出關閉安全模式且 `confirm_rules` 為真
- **THEN** 偏好變為 `adult`，並記錄確認時間、規範版本與方式為自行聲明

#### Scenario: 缺少同意
- **WHEN** 送出關閉安全模式但沒有 `confirm_rules`（或值不是布林真）
- **THEN** 回應 400，錯誤指出 `confirm_rules`，偏好不變

#### Scenario: 已同意目前版本
- **WHEN** 已在目前規範版本同意過的玩家從 `safe` 再切換為 `adult`
- **THEN** 不需要 `confirm_rules`，直接成功

#### Scenario: 規範版本改變
- **WHEN** 安全規範的版本更新，先前同意過的玩家偏好仍是 `adult`
- **THEN** 生效模式退為 `safe`，直到玩家重新同意

#### Scenario: 回到安全模式不清除紀錄
- **WHEN** 玩家從 `adult` 切換回 `safe`
- **THEN** 同意紀錄仍在

#### Scenario: 不再要求年滿 18 歲的勾選
- **WHEN** 玩家送出關閉安全模式，只帶 `confirm_rules`
- **THEN** 成功，沒有要求 `confirm_adult`

### Requirement: 生效模式依設定、劇本綁定與緊急煞車現算
實際生效的內容模式 SHALL 在每次需要時現算，為下列各項中最保守的一個：玩家目前的偏好（缺少目前安全規範版本下的同意時視為 `safe`）、劇本是否綁定安全模式（有劇本時）、緊急煞車（啟用時為 `safe`）。存檔 SHALL NOT 記錄或釘住自己的模式：玩家在個人頁調整後，下一個回合起即套用新的模式；SHALL NOT 依年齡而有不同的上限（全站皆成年）。管理員與各會員等級 SHALL NOT 豁免。

#### Scenario: 取各項最保守
- **WHEN** 偏好 `adult` 且已同意、劇本綁定安全模式、緊急煞車未啟用
- **THEN** 生效模式為 `safe`

#### Scenario: 全部放寬
- **WHEN** 偏好 `adult` 且已同意、劇本未綁定、緊急煞車未啟用
- **THEN** 生效模式為 `adult`

#### Scenario: 存檔跟著設定
- **WHEN** 玩家在遊戲中途於個人頁把安全模式由開改為關，之後回到同一個未綁定劇本的存檔
- **THEN** 下一個回合的生效模式為 `adult`，不因開存檔時的設定而固定

#### Scenario: 緊急煞車
- **WHEN** 緊急煞車啟用
- **THEN** 所有玩家與所有存檔的生效模式一律為 `safe`

#### Scenario: 沒有同意紀錄
- **WHEN** 偏好為 `adult`，但沒有目前版本的同意紀錄
- **THEN** 生效模式為 `safe`，沒有錯誤

#### Scenario: 管理員不豁免
- **WHEN** 管理員的偏好為 `safe`
- **THEN** 生效模式為 `safe`

### Requirement: 玩家的安全模式偏好與預設
每位使用者 SHALL 有一個內容模式偏好（`safe` 或 `adult`），預設為最保守的 `safe`。年齡確認成功的當下 SHALL 把偏好設為 `safe`。偏好 SHALL NOT 因任何原因被系統自動調高（包含玩家輸入、模型輸出、年齡或登入）；只有玩家自己在個人頁切換才會改變。遷移之前儲存為 `standard` 或 `adult` 的偏好 SHALL 視為 `adult`，但仍須在目前安全規範版本下完成同意才會生效。

#### Scenario: 新帳號
- **WHEN** 新玩家完成年齡確認
- **THEN** 偏好為 `safe`

#### Scenario: 不自動調高
- **WHEN** 偏好為 `safe` 的玩家在遊戲中輸入性內容
- **THEN** 偏好與生效模式都不變

#### Scenario: 舊偏好
- **WHEN** 既有帳號儲存的偏好為 `standard`，且沒有目前版本的同意紀錄
- **THEN** 偏好讀作 `adult`，生效模式為 `safe`

### Requirement: 緊急煞車只能以環境設定啟用
環境變數 `SAFE_MODE_FORCED` SHALL 控制緊急煞車，預設為未啟用（玩家可關閉安全模式），SHALL NOT 在程式中寫死為啟用。啟用時所有生效模式 SHALL 一律為 `safe`，不論玩家偏好與劇本；它是營運出事時全站停用「關閉安全模式」的開關，不是使用門檻，SHALL NOT 要求任何書面許可、模型清單或法務前提。四個環境（`development`、`testing`、`staging`、`production`）皆可設定；值不是布林 SHALL 使應用程式啟動失敗，訊息只指出變數名稱而不含其值。設定 SHALL 於重新啟動後生效，且 SHALL 不需要資料庫遷移或程式改版。

#### Scenario: 沒有設定
- **WHEN** 沒有設定 `SAFE_MODE_FORCED`
- **THEN** 緊急煞車未啟用，玩家可關閉安全模式

#### Scenario: 啟用
- **WHEN** `SAFE_MODE_FORCED` 為真
- **THEN** 所有玩家的生效模式為 `safe`，玩家保存的偏好不被改寫

#### Scenario: 值不合法
- **WHEN** 值是 `maybe`
- **THEN** 應用程式啟動失敗，訊息指出 `SAFE_MODE_FORCED` 而不含其值

#### Scenario: 解除後恢復
- **WHEN** 煞車解除並重新啟動
- **THEN** 偏好為 `adult` 且已同意的玩家（劇本未綁定）生效模式恢復為 `adult`

### Requirement: 絕對禁止項不隨模式放寬
不論生效模式為何，系統 SHALL 一律禁止涉及未成年的性內容、性暴力、非自願的性情節與以真人名義的性內容；判定 SHALL 由伺服器端自有的規則層與審查層執行，SHALL NOT 僅依賴模型拒絕。玩家行動命中這些類別 SHALL 在預扣遊戲幣與呼叫模型之前被拒絕。沒有明確年齡的角色 SHALL NOT 因此被擋，露骨場景也一樣。

#### Scenario: 安全關仍擋明確違規
- **WHEN** 生效模式為 `adult`，玩家行動命中必須拒絕的類別
- **THEN** 回應 422 與代碼 `input_blocked`，沒有預扣遊戲幣也沒有呼叫模型

#### Scenario: 沒有明確年齡的角色
- **WHEN** 生效模式為 `adult`，年齡未知的 NPC 出現在露骨場景，沒有任何未成年的線索
- **THEN** 不因此被擋
