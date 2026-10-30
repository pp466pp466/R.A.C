# Spec Delta

## ADDED Requirements

### Requirement: 告警事件只含固定列舉與數字
系統 SHALL 為告警的進入、恢復、通知失敗與評估失敗各記錄一筆結構化日誌（`alert.firing`、`alert.resolved`、`alert.delivery_failed`、`alert.check_failed`），欄位只有事件名稱、告警代碼（固定列舉）、嚴重度（固定列舉）與整數數值。事件 SHALL NOT 含收件人地址、信件內容、使用者資料或例外訊息；傳入列舉以外的告警代碼 SHALL 立刻出錯。預覽模式 SHALL NOT 產生任何告警事件。

#### Scenario: 進入告警
- **WHEN** 某項告警進入警告
- **THEN** 日誌新增一筆 `alert.firing`，含告警代碼與嚴重度，不含收件人

#### Scenario: 寄信失敗
- **WHEN** 告警信寄送失敗
- **THEN** 日誌新增一筆 `alert.delivery_failed`，只含告警代碼，不含收件人地址與例外訊息

#### Scenario: 完整流程後掃描日誌
- **WHEN** 走完進入、持續、升級、提醒、恢復與寄信失敗，並收集期間的全部日誌與指令輸出
- **THEN** 找不到任何收件人地址、寄信密碼或使用者資料

#### Scenario: 預覽
- **WHEN** 以預覽模式執行告警檢查
- **THEN** 沒有任何告警事件被記錄
