# Spec Delta

## ADDED Requirements

### Requirement: 用量事件只含白名單欄位
用量相關的拒絕、全站預算警告與對話暫停 SHALL 以結構化事件記錄，欄位只限事件名稱、內部使用者編號、原因代碼與整數的 `used`、`limit`。事件 SHALL NOT 含訊息文字、回覆文字、email 或任何機密。拒絕事件的原因代碼 SHALL 為固定列舉：`rate_limited`、`daily_quota`、`conversation_limit`、`suspended`、`global_budget`。

#### Scenario: 每分鐘限制拒絕
- **WHEN** 一則訊息因每分鐘限制被拒絕
- **THEN** 日誌出現一筆拒絕事件，含內部使用者編號與原因 `rate_limited`，不含訊息文字

#### Scenario: 預算警告
- **WHEN** 全站用量越過 80%
- **THEN** 事件含整數的 `used` 與 `limit`

#### Scenario: 欄位白名單
- **WHEN** 檢查所有用量事件實際輸出的欄位
- **THEN** 每個欄位都在日誌的白名單內，且新增的 `used`、`limit` 只出現整數
