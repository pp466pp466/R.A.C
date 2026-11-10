# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入故事偏好與讀者頻道
帳號資料 SHALL 包含使用者的故事偏好（`all`、`male`、`female`）。使用者自己創作的劇本的每個版本 SHALL 包含讀者頻道（空值的舊版本匯出為 `all`）。JSON 與 CSV 兩種格式 SHALL 都含這兩項。

#### Scenario: 含故事偏好
- **WHEN** 偏好為 `female` 的使用者匯出
- **THEN** 帳號資料的故事偏好為 `female`

#### Scenario: 預設偏好
- **WHEN** 從未設定過偏好的使用者匯出
- **THEN** 帳號資料的故事偏好為 `all`

#### Scenario: 含版本的讀者頻道
- **WHEN** 使用者有一個男頻的已發佈版本與一個遷移之前建立、頻道為空值的舊版本
- **THEN** 匯出的兩個版本分別為 `male` 與 `all`

#### Scenario: CSV 也含
- **WHEN** 使用者匯出 CSV
- **THEN** 帳號資料與劇本版本的資料列都含故事偏好與讀者頻道
