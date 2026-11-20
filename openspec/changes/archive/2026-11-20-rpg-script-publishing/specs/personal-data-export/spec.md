# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入劇本版本的審核與下架結果
使用者自己創作的劇本的每個版本 SHALL 包含狀態（可為 `removed`）、決定的類別（人工退回或管理員下架時的固定列舉，否則為空）、決定的時間與下架者（`author`、`admin` 或空）。匯出 SHALL NOT 含管理員的審核備註，也不含稽核紀錄。JSON 與 CSV 兩種格式 SHALL 都含這些欄位。

#### Scenario: 被退回的版本
- **WHEN** 使用者有一個被人工以 `other_policy` 退回的版本並匯出
- **THEN** 該版本的狀態為 `rejected`，決定的類別為 `other_policy`，有決定時間

#### Scenario: 被管理員下架的版本
- **WHEN** 使用者有一個被管理員下架的版本並匯出
- **THEN** 該版本的狀態為 `removed`，下架者為 `admin`，有類別與時間

#### Scenario: 沒有備註
- **WHEN** 管理員曾為使用者的版本寫下審核備註，使用者匯出
- **THEN** 匯出的任何欄位都找不到備註的文字

#### Scenario: CSV 也含
- **WHEN** 使用者匯出 CSV
- **THEN** 劇本版本的資料列含這些欄位
