# Spec Delta

## ADDED Requirements

### Requirement: 後台事件只含固定列舉與內部編號
後台的事件 SHALL 以結構化日誌記錄，欄位只限事件名稱、內部使用者編號與固定列舉的原因：`admin.access_denied`（非管理員被擋，原因為 `not_admin`）、`admin.reauth_failed`（重新輸入密碼失敗，原因為嘗試的動作）、`admin.action`（改變資料的操作成功，原因為動作）。事件 SHALL NOT 含 email、顯示名稱、訊息文字、管理員填寫的原因或任何請求內容。

#### Scenario: 非管理員被擋
- **WHEN** 一般使用者開啟後台
- **THEN** 日誌有一筆 `admin.access_denied`，只含內部編號與原因 `not_admin`

#### Scenario: 停權成功
- **WHEN** 管理員停權一位使用者
- **THEN** 日誌有一筆 `admin.action`，原因為 `user.suspend`，沒有 email 與管理員填寫的原因

#### Scenario: 欄位白名單
- **WHEN** 檢查所有後台事件實際輸出的欄位
- **THEN** 每個欄位都在日誌的白名單內
