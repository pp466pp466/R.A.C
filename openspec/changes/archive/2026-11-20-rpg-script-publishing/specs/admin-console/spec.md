# Spec Delta

## ADDED Requirements

### Requirement: 後台新增「劇本審核」分頁
`/admin` 的分頁 SHALL 新增「劇本審核」，其資料一律經由 `/api/admin/scripts*` 取得，行為與內容見 `admin-script-review`；頁面外殼仍只有初始狀態（管理員的顯示名稱），不含任何劇本內容。待審的數量 SHALL 顯示在分頁標籤上（來自清單端點的回應，不新增總覽欄位）。

#### Scenario: 分頁存在
- **WHEN** 管理員開啟 `/admin`
- **THEN** 分頁列有「劇本審核」，切換後載入待審清單

#### Scenario: 外殼沒有劇本內容
- **WHEN** 檢查 `/admin` 回傳的 HTML
- **THEN** 其中沒有任何劇本標題、筆名或內文

#### Scenario: 一般使用者
- **WHEN** 一般使用者開啟 `/admin`
- **THEN** 得到與先前相同的拒絕回應，沒有分頁資訊
