# Spec Delta

## MODIFIED Requirements

### Requirement: Google 登入的帳號狀態與結果
透過 Google 登入的帳號若為 `suspended`，系統 SHALL 拒絕登入並顯示說明，且不建立登入狀態。若為 `deletion_pending`，系統 SHALL 允許登入但只建立受限登入狀態，並導向 `/account` 讓使用者撤銷刪除。登入成功時，登入狀態的建立方式與屬性 SHALL 與密碼登入相同，並更新最後登入時間。顯示名稱 SHALL 取自 Google 提供的名稱（最長 50 字），缺少時以信箱的本機部分代替。

#### Scenario: 停權帳號
- **WHEN** 已停權的帳號以 Google 登入
- **THEN** 導向登入頁並顯示帳號已停用，不建立登入狀態

#### Scenario: 待刪除帳號
- **WHEN** 狀態為 `deletion_pending` 的帳號以 Google 登入
- **THEN** 建立受限登入狀態並導向 `/account`，頁面只提供撤銷刪除與登出

#### Scenario: 缺少名稱
- **WHEN** Google 沒有提供名稱
- **THEN** 顯示名稱使用信箱的本機部分
