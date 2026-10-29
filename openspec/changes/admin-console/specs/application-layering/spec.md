# Spec Delta

## ADDED Requirements

### Requirement: 後台專用的資料存取只供後台 Service 使用
讀寫他人資料的後台專用 Repository SHALL 只被 `AdminService` 與負責組裝的程式使用；控制器、`app/security`、其他 Service 與一般使用者功能的 Repository SHALL NOT 匯入它們。這條規則 SHALL 由架構測試檢查，違反時測試失敗並指出是哪個檔案。

#### Scenario: 其他模組匯入後台 Repository
- **WHEN** 一個控制器或其他 Service 匯入後台專用的 Repository
- **THEN** 架構測試失敗並指出該檔案

### Requirement: 後台 Service 的每個公開方法先檢查管理員
`AdminService` 的每個公開方法 SHALL 在做任何事之前先通過政策物件的管理員檢查。這條規則 SHALL 由架構測試以原始碼結構檢查，新增方法而忘了檢查時測試失敗並指出方法名稱。

#### Scenario: 忘了檢查
- **WHEN** 新增一個沒有先呼叫管理員檢查的公開方法
- **THEN** 架構測試失敗並指出該方法
