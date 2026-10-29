# Spec Delta

## MODIFIED Requirements

### Requirement: 查詢目前登入者
系統 SHALL 提供查詢目前登入者的 API：已登入時回傳該會員自己的資料（對外識別碼、email、顯示名稱、會員等級、角色、帳號狀態、信箱是否已驗證、是否已設定密碼、是否已綁定 Google、是否已完成年齡確認、年齡群組 `minor` 或 `adult`），未登入時回應 401。尚未完成年齡確認者的年齡群組 SHALL 為空值。回應 SHALL NOT 包含密碼雜湊、內部編號或 `token_version`，也 SHALL NOT 包含生日、年齡區間、年齡確認的時間與政策版本。

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
