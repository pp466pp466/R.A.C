# Spec Delta

## MODIFIED Requirements

### Requirement: 以 token_version 撤銷登入狀態
每次請求辨識登入狀態時，系統 SHALL 比對 cookie 內的 `token_version` 與資料庫中的值，不一致即視為未登入。改密碼、登出所有裝置、重設密碼、綁定 Google 身分與申請刪除帳號時，系統 SHALL 使 `token_version` 加 1。帳號狀態為 `suspended` 時，既有的登入狀態 SHALL 立即失效。狀態為 `deletion_pending` 的帳號，申請當下的既有登入狀態因 `token_version` 加 1 而失效；之後重新登入得到的受限登入狀態 SHALL 有效。

#### Scenario: 登出所有裝置
- **WHEN** 使用者在裝置 A 與 B 皆已登入，於 A 執行「登出所有裝置」
- **THEN** 裝置 B 下一次請求被視為未登入

#### Scenario: 改密碼後
- **WHEN** 使用者改密碼
- **THEN** 其他裝置的登入狀態失效，目前裝置以新的 `token_version` 維持登入

#### Scenario: 帳號被停權
- **WHEN** 已登入使用者的帳號狀態變為 `suspended`
- **THEN** 其下一次請求被視為未登入

#### Scenario: 申請刪除後所有裝置登出
- **WHEN** 使用者在裝置 A 與 B 皆已登入，於 A 申請刪除帳號
- **THEN** 兩個裝置下一次請求都被視為未登入

#### Scenario: 待刪除帳號重新登入後的狀態
- **WHEN** 待刪除的使用者重新登入成功
- **THEN** 取得的受限登入狀態在之後的請求中有效，直到被撤銷、登出或帳號被刪除

### Requirement: 帳號狀態對登入的影響
密碼驗證成功後，狀態為 `active` 與 `pending_verification` 的帳號 SHALL 可登入；`pending_verification` 的帳號登入後 SHALL 只能使用驗證相關功能，不得使用客服。狀態為 `suspended` 的帳號 SHALL 被拒絕登入，回應 403 與繁體中文說明。狀態為 `deletion_pending` 的帳號 SHALL 可登入，但只得到受限登入狀態：回應中的帳號狀態為 `deletion_pending`，且除了查詢自己、撤銷刪除、登出與開啟帳號設定頁之外，其餘功能 SHALL 被拒絕。

#### Scenario: 待驗證帳號登入
- **WHEN** 狀態為 `pending_verification` 的使用者以正確密碼登入
- **THEN** 登入成功，帳號資訊顯示尚未驗證信箱

#### Scenario: 停權帳號登入
- **WHEN** 狀態為 `suspended` 的使用者以正確密碼登入
- **THEN** 回應 403 與說明，且不建立登入狀態

#### Scenario: 待刪除帳號登入
- **WHEN** 狀態為 `deletion_pending` 的使用者以正確密碼登入
- **THEN** 登入成功，回應中的帳號狀態為 `deletion_pending`，之後只能撤銷刪除或登出

#### Scenario: 待刪除帳號密碼錯誤
- **WHEN** 狀態為 `deletion_pending` 的使用者以錯誤密碼登入
- **THEN** 與一般帳號相同，回應通用的失敗訊息並計入失敗次數

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
- **WHEN** 註冊時 17 歲的使用者在 18 歲生日之後查詢目前登入者
- **THEN** 年齡群組為 `adult`，不需要重新登入或補填

#### Scenario: 待刪除帳號
- **WHEN** 狀態為 `deletion_pending` 的使用者查詢目前登入者
- **THEN** 回應含帳號狀態與預定刪除的時間（申請時間加 7 天）
