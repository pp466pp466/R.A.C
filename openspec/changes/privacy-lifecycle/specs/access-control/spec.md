# Spec Delta

## MODIFIED Requirements

### Requirement: 需要登入的端點
未登入者呼叫需要登入的 API 時，系統 SHALL 回應 401 與 JSON 錯誤；開啟需要登入的頁面時 SHALL 導向登入頁並保留安全的返回位置。已登入但狀態不允許的使用者 SHALL 得到對應的 403 說明。持有受限登入狀態（帳號為 `deletion_pending`）者 SHALL 視為「狀態不允許」，除非該端點或頁面明確允許受限狀態。

#### Scenario: 未登入呼叫 API
- **WHEN** 未登入者呼叫需要登入的 API
- **THEN** 回應 401 的 JSON 錯誤

#### Scenario: 未登入開啟受保護頁面
- **WHEN** 未登入者開啟需要登入的頁面
- **THEN** 導向登入頁，登入後返回原頁面

#### Scenario: 受限狀態呼叫 API
- **WHEN** 持有受限登入狀態者呼叫未明確允許受限狀態的 API
- **THEN** 回應 403 與代碼 `account_deletion_pending`，且不執行任何動作

#### Scenario: 受限狀態開啟其他頁面
- **WHEN** 持有受限登入狀態者開啟 `/account` 以外需要登入的頁面
- **THEN** 導向 `/account`

## ADDED Requirements

### Requirement: 受限登入狀態預設拒絕
受限登入狀態 SHALL 預設拒絕：只有明確列入允許清單的端點能接受它，允許清單 SHALL 僅含查詢自己、撤銷刪除、登出與 `/account` 頁面。新增的需要登入的端點若沒有明確允許，SHALL 自動拒絕受限狀態，且 SHALL 有自動化測試掃描所有需要登入的端點來證明這一點。

#### Scenario: 允許清單以外的端點
- **WHEN** 測試以受限登入狀態逐一呼叫所有需要登入的 API
- **THEN** 允許清單以外的端點全部回應 403 與 `account_deletion_pending`

#### Scenario: 新增端點
- **WHEN** 開發者新增一個需要登入的端點而未列入允許清單
- **THEN** 該端點對受限狀態同樣回應 403
