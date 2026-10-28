# Spec Delta

## MODIFIED Requirements

### Requirement: 查詢目前登入者
系統 SHALL 提供查詢目前登入者的 API：已登入時回傳該會員自己的資料（對外識別碼、email、顯示名稱、會員等級、角色、帳號狀態、信箱是否已驗證、是否已設定密碼、是否已綁定 Google、是否已確認年滿 18 歲），未登入時回應 401。回應 SHALL NOT 包含密碼雜湊、內部編號或 `token_version`，也 SHALL NOT 包含年齡確認的時間與政策版本。

#### Scenario: 已登入
- **WHEN** 已登入使用者查詢目前登入者
- **THEN** 回應其帳號資料，且不含密碼雜湊、內部編號與 `token_version`

#### Scenario: 未登入
- **WHEN** 未登入者查詢目前登入者
- **THEN** 回應 401 的 JSON 錯誤

#### Scenario: 年齡確認狀態
- **WHEN** 已確認與尚未確認年滿 18 歲的使用者分別查詢目前登入者
- **THEN** 回應中的「是否已確認」分別為真與假，且不含確認時間與政策版本
