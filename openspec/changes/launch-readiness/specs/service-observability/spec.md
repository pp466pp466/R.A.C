# Spec Delta

## ADDED Requirements

### Requirement: 還原後重新抹除的記錄
系統 SHALL 為還原後重新抹除的每一個帳號記錄一筆結構化日誌 `account.deletion_replayed`，欄位只有事件名稱與內部使用者編號。事件 SHALL NOT 含 email、名稱、生日、檔案路徑或檔案內容；預覽模式 SHALL NOT 記錄此事件。

#### Scenario: 重新抹除
- **WHEN** 還原後的資料庫重新抹除了使用者 7
- **THEN** 日誌新增一筆 `account.deletion_replayed`，只含內部編號 7

#### Scenario: 完整流程後掃描日誌
- **WHEN** 走完還原、預覽、實際抹除並收集日誌與指令輸出
- **THEN** 找不到 email、名稱、生日或檔案內容

#### Scenario: 預覽
- **WHEN** 以預覽模式執行
- **THEN** 沒有任何 `account.deletion_replayed` 事件

### Requirement: 告警檢查完成的心跳記錄
系統 SHALL 在 `check-alerts` 完整跑完一次（非預覽、非唯讀模式，且沒有任何一項評估失敗）時記錄一筆結構化日誌 `alert.check_completed`，欄位只有事件名稱，作為平台偵測「告警檢查停擺」的心跳。預覽、唯讀（未設定收件人）與有評估失敗的執行 SHALL NOT 記錄此事件。

#### Scenario: 成功的一次執行
- **WHEN** 設有收件人的環境完整執行 `check-alerts`
- **THEN** 日誌新增一筆 `alert.check_completed`，除事件名稱外沒有其他欄位

#### Scenario: 預覽與唯讀
- **WHEN** 以預覽模式執行，或沒有設定收件人
- **THEN** 沒有 `alert.check_completed`

#### Scenario: 有評估失敗
- **WHEN** 某一項評估失敗而指令以非零代碼結束
- **THEN** 沒有 `alert.check_completed`
