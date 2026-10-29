# Spec Delta

## ADDED Requirements

### Requirement: 全站每日用量上限設定
設定 SHALL 提供 `USAGE_DAILY_TOKEN_LIMIT`（全站每日 token 上限）與 `USAGE_DAILY_CALL_LIMIT`（全站每日模型呼叫次數上限），皆為正整數，未設定時使用預設值；不是正整數或超過合理上限時 SHALL 在啟動時以明確訊息失敗。`.env.example` SHALL 列出這兩個變數名稱而不含實際值。

#### Scenario: 預設值
- **WHEN** 載入沒有這兩個變數的設定
- **THEN** 設定物件的兩個上限為預設值

#### Scenario: 自訂值
- **WHEN** `USAGE_DAILY_TOKEN_LIMIT=500000` 且 `USAGE_DAILY_CALL_LIMIT=300`
- **THEN** 設定物件的兩個上限分別為 500000 與 300

#### Scenario: 不合法
- **WHEN** `USAGE_DAILY_CALL_LIMIT=0`
- **THEN** 載入設定失敗，訊息指出變數名稱
