# Spec Delta

## MODIFIED Requirements

### Requirement: 停權
`POST /api/admin/users/<public_id>/suspend` SHALL 要求原因（清理後 1 至 200 字）與管理員的密碼；目標狀態為 `active` 時 SHALL 改為 `suspended` 並使其 `token_version` 加 1，使既有的登入狀態立即失效；目標已是 `suspended` 時 SHALL 得到 409。停權後該使用者 SHALL 無法登入（回應「帳號已停用」）。SHALL 寫入 `user.suspend` 稽核（含原因與原狀態）。

#### Scenario: 停權成功
- **WHEN** 管理員以正確的密碼與原因停權一位使用中的使用者
- **THEN** 該使用者狀態為 `suspended`、既有登入狀態失效、無法再登入，並有一筆 `user.suspend` 稽核

#### Scenario: 缺少原因
- **WHEN** 停權請求沒有原因或原因超過 200 字
- **THEN** 回應 400 並指出是原因欄位，使用者狀態不變

#### Scenario: 重複停權
- **WHEN** 對已停權的使用者再次停權
- **THEN** 回應 409，沒有新的稽核

### Requirement: 解除停權
`POST /api/admin/users/<public_id>/unsuspend` SHALL 要求管理員的密碼；只有狀態為 `suspended` 的使用者可被解除，結果一律為 `active`。其他狀態 SHALL 得到 409。SHALL 寫入 `user.unsuspend` 稽核。

#### Scenario: 解除後可登入
- **WHEN** 管理員以正確的密碼解除一位停權的使用者
- **THEN** 狀態為 `active`，該使用者可以再次以 Google 登入

#### Scenario: 信箱未驗證
- **WHEN** 被解除的使用者信箱驗證時間為空（遷移前遺留的帳號）
- **THEN** 狀態仍為 `active`，不會變成其他狀態

#### Scenario: 非停權狀態
- **WHEN** 對狀態為 `active` 的使用者解除停權
- **THEN** 回應 409，沒有新的稽核
