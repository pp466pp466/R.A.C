# Spec Delta

## MODIFIED Requirements

### Requirement: 密碼以 Argon2id 雜湊
系統 SHALL 以 Argon2id 雜湊管理員的密碼後才儲存，SHALL NOT 儲存或記錄明文密碼。密碼 SHALL 先經 Unicode NFKC 正規化再雜湊，設定與驗證使用同一規則。管理員密碼驗證成功時，若既有雜湊的參數已低於目前設定，系統 SHALL 以新參數重新雜湊。

#### Scenario: 儲存的是雜湊
- **WHEN** 以 `create-admin` 建立管理員後檢視資料庫中的密碼欄位
- **THEN** 內容為 Argon2id 雜湊字串，不含明文

#### Scenario: 全形與半形等價
- **WHEN** 以全形數字組成的密碼建立管理員，之後在密碼驗證步驟輸入對應的半形數字
- **THEN** 驗證成功

#### Scenario: 舊參數的雜湊
- **WHEN** 雜湊以較弱的參數產生，且管理員以正確密碼通過密碼驗證步驟
- **THEN** 資料庫中的雜湊被更新為目前參數

### Requirement: 連續失敗鎖定帳號
同一管理員帳號在密碼驗證步驟（含高風險操作的重新輸入密碼與變更密碼時的目前密碼）連續失敗 5 次，系統 SHALL 鎖定該帳號 15 分鐘。失敗計數與鎖定時間 SHALL 存放於資料庫，使多個 worker 共用。密碼驗證成功 SHALL 將失敗計數歸零。鎖定期間的嘗試 SHALL NOT 延長鎖定時間。並行的失敗嘗試 SHALL NOT 造成計數被低估。

#### Scenario: 第五次失敗
- **WHEN** 同一管理員連續第 5 次輸入錯誤密碼
- **THEN** 帳號被鎖定 15 分鐘

#### Scenario: 鎖定到期
- **WHEN** 鎖定期間已過，管理員以正確密碼通過驗證
- **THEN** 驗證成功，失敗計數歸零

#### Scenario: 成功登入重設計數
- **WHEN** 連續失敗 4 次後以正確密碼通過驗證，接著再輸入 1 次錯誤密碼
- **THEN** 帳號不會被鎖定，失敗計數為 1

#### Scenario: 多個 worker 並行失敗
- **WHEN** 兩個請求同時對同一帳號送出錯誤密碼
- **THEN** 失敗計數增加 2

#### Scenario: 失敗的請求結束後計數仍在
- **WHEN** 同一管理員透過一連串各自獨立的 HTTP 請求送出 5 次錯誤密碼（每次回應 401）
- **THEN** 失敗計數隨之累積到 5，帳號被鎖定，之後以正確密碼驗證仍被拒絕

### Requirement: 查詢目前登入者
系統 SHALL 提供查詢目前登入者的 API：已登入時回傳該會員自己的資料（對外識別碼、email、顯示名稱、會員等級、角色、帳號狀態、信箱是否已驗證、是否已設定密碼、是否已綁定 Google、是否已完成年齡確認、年齡群組 `minor` 或 `adult`），未登入時回應 401。尚未完成年齡確認者的年齡群組 SHALL 為空值。狀態為 `deletion_pending` 時回應 SHALL 另含預定刪除的時間。回應 SHALL NOT 包含密碼雜湊、內部編號或 `token_version`，也 SHALL NOT 包含生日、年齡區間、年齡確認的時間與政策版本。

#### Scenario: 已登入
- **WHEN** 已登入使用者查詢目前登入者
- **THEN** 回應其帳號資料，且不含密碼雜湊、內部編號與 `token_version`

#### Scenario: 未登入
- **WHEN** 未登入者查詢目前登入者
- **THEN** 回應 401 的 JSON 錯誤

#### Scenario: 年齡確認狀態
- **WHEN** 已完成與尚未完成年齡確認的使用者分別查詢目前登入者
- **THEN** 回應中的「是否已完成」分別為真與假，年齡群組分別為 `minor` 或 `adult`、以及空值，且都不含生日、年齡區間、確認時間與政策版本

#### Scenario: 生日到了之後
- **WHEN** 年齡確認時 17 歲的使用者在 18 歲生日之後查詢目前登入者
- **THEN** 年齡群組為 `adult`，不需要重新登入或補填

#### Scenario: 待刪除帳號
- **WHEN** 狀態為 `deletion_pending` 的使用者查詢目前登入者
- **THEN** 回應含帳號狀態與預定刪除的時間（申請時間加 7 天）

### Requirement: 變更密碼
已登入且已設定密碼的使用者（實際上只有管理員）SHALL 能在提供目前密碼的前提下變更密碼；新密碼須符合長度規則且不得與目前密碼相同。目前密碼錯誤時，系統 SHALL 回應 400 並計入登入失敗次數。變更成功後，系統 SHALL 使其他裝置的登入狀態失效；已有寄信設定時 SHALL 寄出密碼已變更的通知信。

#### Scenario: 目前密碼錯誤
- **WHEN** 管理員提供錯誤的目前密碼
- **THEN** 回應 400，密碼不變，且失敗次數增加

#### Scenario: 變更成功
- **WHEN** 管理員提供正確的目前密碼與合規的新密碼
- **THEN** 密碼更新，其他裝置被登出，目前裝置維持登入

#### Scenario: 沒有密碼的帳號
- **WHEN** 玩家（Google 帳號，沒有密碼）呼叫變更密碼
- **THEN** 回應 400，說明目前的帳號沒有設定密碼

### Requirement: 認證回應不可被快取
認證相關的 API 回應與含權杖的頁面 SHALL 禁止被瀏覽器或中介快取。

#### Scenario: 回應標頭
- **WHEN** 請求任一 `/api/auth/` 端點，或開啟重設密碼的確認頁
- **THEN** 回應標頭指示不得快取

## ADDED Requirements

### Requirement: 管理員密碼驗證的失敗回應一致
管理員的密碼驗證步驟在密碼錯誤或帳號被鎖定時，系統 SHALL 回應相同的狀態碼與繁體中文通用訊息，SHALL NOT 洩漏帳號是否被鎖定。中間狀態對不上目前的帳號（帳號不存在、不是管理員、`token_version` 已變、被停權）時，系統 SHALL 仍執行與驗證密碼相當的運算，並回應 401 要求重新以 Google 登入。

#### Scenario: 密碼錯誤
- **WHEN** 持有中間狀態的管理員輸入錯誤密碼
- **THEN** 回應 401 與通用訊息

#### Scenario: 帳號被鎖定時以正確密碼驗證
- **WHEN** 管理員帳號處於鎖定期間，輸入正確密碼
- **THEN** 回應與密碼錯誤相同的通用訊息，且不建立登入狀態

#### Scenario: 中間狀態對不上帳號
- **WHEN** 中間狀態所指的管理員在這 10 分鐘內被改了密碼（`token_version` 已變）
- **THEN** 回應 401 要求重新以 Google 登入，不計入失敗次數

### Requirement: 帳號狀態決定能否登入
Google 登入成功（玩家）或管理員通過密碼驗證步驟後，狀態為 `active` 的帳號 SHALL 可登入。狀態為 `suspended` 的帳號 SHALL 被拒絕登入，回應 403 與繁體中文說明。狀態為 `deletion_pending` 的帳號 SHALL 可登入，但只得到受限登入狀態：回應中的帳號狀態為 `deletion_pending`，且除了查詢自己、撤銷刪除、登出與開啟帳號設定頁之外，其餘功能 SHALL 被拒絕。系統 SHALL NOT 再有 `pending_verification` 狀態。

#### Scenario: 停權帳號登入
- **WHEN** 狀態為 `suspended` 的使用者以 Google 登入
- **THEN** 被拒絕並顯示說明，不建立登入狀態

#### Scenario: 待刪除帳號登入
- **WHEN** 狀態為 `deletion_pending` 且已綁定 Google 的使用者以 Google 登入
- **THEN** 登入成功，回應中的帳號狀態為 `deletion_pending`，之後只能撤銷刪除或登出

#### Scenario: 停權的管理員
- **WHEN** 管理員通過 Google 後，中間狀態所指的帳號已被停權
- **THEN** 密碼驗證回應 401，不建立登入狀態

### Requirement: 玩家帳號不持有密碼
系統 SHALL NOT 提供以信箱與密碼登入的端點（`POST /api/auth/login` 回應 404），玩家帳號 SHALL NOT 持有密碼雜湊、登入失敗計數或密碼鎖定。資料遷移 SHALL 清除所有非管理員帳號的密碼雜湊、失敗計數與密碼鎖定，並把既有的 `pending_verification` 帳號（以及申請刪除前狀態為 `pending_verification` 者）改為 `active`。遷移之後，持有舊帳號的玩家以同信箱的 Google 登入即綁定回原帳號。

#### Scenario: 密碼登入端點不存在
- **WHEN** 任何人以任何信箱與密碼呼叫 `POST /api/auth/login`
- **THEN** 回應 404，沒有登入 session

#### Scenario: 遷移清除玩家的密碼
- **WHEN** 資料庫中有帶密碼雜湊與失敗計數的玩家帳號與管理員帳號，執行遷移
- **THEN** 玩家帳號的密碼雜湊為空、失敗計數為 0、密碼鎖定為空；管理員帳號完全不變

#### Scenario: 遷移改寫待驗證狀態
- **WHEN** 資料庫中有狀態為 `pending_verification` 的帳號，以及申請刪除前狀態為 `pending_verification` 的帳號，執行遷移
- **THEN** 兩者都變為 `active`，其餘欄位（包含信箱驗證時間為空）不變

#### Scenario: 舊玩家以 Google 回來
- **WHEN** 遷移前以密碼註冊並已完成年齡確認的玩家，之後以同信箱的 Google 帳號登入
- **THEN** 綁定回原帳號並登入，不需要重填年齡

## REMOVED Requirements

### Requirement: 登入失敗回應一致
**Reason**: 沒有以信箱加密碼登入的端點，也就沒有「信箱不存在」的登入回應；失敗回應一致的要求改由管理員密碼驗證步驟承接。
**Migration**: 見「管理員密碼驗證的失敗回應一致」。

### Requirement: 帳號狀態對登入的影響
**Reason**: 原需求以密碼驗證成功為前提並含 `pending_verification` 狀態；兩者都已移除。
**Migration**: 見「帳號狀態決定能否登入」。
