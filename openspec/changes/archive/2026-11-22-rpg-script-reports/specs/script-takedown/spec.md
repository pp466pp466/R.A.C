# Spec Delta

## MODIFIED Requirements

### Requirement: 管理員可下架與恢復並須重新輸入密碼
`POST /api/admin/scripts/versions/<id>/remove` SHALL 把 `published` 的版本改為 `removed`，下架者為 `admin`，並 SHALL 要求固定列舉的 `category`（審查類別列舉加 `other_policy`）、選填的審核備註（至多 500 字）與管理員的密碼，寫入 `script.remove` 稽核（細節含類別）。`POST …/restore` SHALL 只適用下架者為 `admin` 的版本，要求密碼並寫入 `script.restore` 稽核。密碼錯誤 SHALL 被拒絕、寫入 `reauth.failed`，版本不變；下架者為作者的版本 SHALL 回 409 `removed_by_author`；恢復時同劇本已有另一個上架版本 SHALL 回 409 `live_exists`。內建劇本（有種子鍵的劇本）SHALL 不可下架（409 `builtin_not_removable`）；判斷 SHALL 依種子鍵，不依作者是否為空，所以作者已註銷、匿名保留的玩家劇本 SHALL 可被下架。下架 SHALL 把該版本未結案的檢舉與下架同一個交易結為「下架」。

#### Scenario: 管理員下架
- **WHEN** 管理員以正確的密碼與類別下架玩家劇本
- **THEN** 版本為 `removed`、下架者為 `admin`，有一筆 `script.remove` 稽核，公開目錄不再出現

#### Scenario: 缺少密碼
- **WHEN** 下架請求沒有密碼
- **THEN** 回應 400 指出密碼欄位，版本仍上架

#### Scenario: 密碼錯誤
- **WHEN** 下架請求的密碼錯誤
- **THEN** 被拒絕，有 `reauth.failed` 稽核，版本不變

#### Scenario: 管理員恢復
- **WHEN** 管理員以正確的密碼恢復自己下架的版本
- **THEN** 版本回到 `published`，有一筆 `script.restore` 稽核，決定的類別與備註清除

#### Scenario: 不恢復作者自己下架的
- **WHEN** 管理員對作者自行下架的版本呼叫恢復
- **THEN** 回應 409 `removed_by_author`

#### Scenario: 內建劇本
- **WHEN** 管理員對內建劇本呼叫下架
- **THEN** 回應 409 `builtin_not_removable`，版本仍上架

#### Scenario: 已註銷作者的劇本
- **WHEN** 管理員以正確的密碼下架一份作者已註銷、匿名保留的上架版本
- **THEN** 版本為 `removed`，有一筆目標使用者為空的 `script.remove` 稽核，不回 `builtin_not_removable`

#### Scenario: 下架時結案檢舉
- **WHEN** 管理員下架一個有未結案檢舉的版本
- **THEN** 那些檢舉結為「下架」
