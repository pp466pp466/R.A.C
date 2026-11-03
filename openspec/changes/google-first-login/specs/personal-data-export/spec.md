# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入申訴
個人資料匯出 SHALL 包含該使用者自己送出的申訴（內容、提交時間、處理狀態），JSON 與 CSV 兩種格式都 SHALL 包含，編碼規則與其他內容相同。匯出 SHALL NOT 包含鎖定計次紀錄、他人的申訴或管理員的處理備註。被鎖定的受限登入狀態 SHALL NOT 能呼叫匯出（匯出不在其允許清單內）。

#### Scenario: 有申訴
- **WHEN** 曾提交申訴的使用者（已解除鎖定）匯出 JSON
- **THEN** 匯出內容包含申訴的文字、提交時間與處理狀態

#### Scenario: 被鎖定時匯出
- **WHEN** 被鎖定的玩家呼叫匯出
- **THEN** 回應 403 與代碼 `account_locked`
