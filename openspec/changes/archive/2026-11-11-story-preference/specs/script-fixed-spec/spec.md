# Spec Delta

## ADDED Requirements

### Requirement: 讀者頻道
固定規格 SHALL 有選填欄位 `audience`（讀者頻道，單選），只能是 `all`（全讀者）、`male`（男頻）、`female`（女頻）之一；沒有提供、為空值或為 `all` SHALL 都視為全讀者。標準化後的內容 SHALL 只在 `audience` 為 `male` 或 `female` 時帶有該欄位，全讀者 SHALL 不寫入標準化內容，使不使用這個欄位的既有內容其標準化結果與 `content_hash` 與加入此欄位之前完全相同。規格版本 SHALL 維持 1。不是三個列舉值之一（含非字串、大小寫不同）SHALL 回報 `audience` 的錯誤，訊息不回顯輸入。`audience` 是列舉值，不計入文字總量，也不進入內容審查的文字欄位。

#### Scenario: 缺值視為全讀者
- **WHEN** 驗證一份沒有 `audience` 的劇本內容
- **THEN** 驗證通過，標準化內容沒有 `audience` 欄位

#### Scenario: 明確的全讀者
- **WHEN** `audience` 為 `all`
- **THEN** 標準化內容與不提供該欄位時完全相同，`content_hash` 也相同

#### Scenario: 男頻與女頻
- **WHEN** `audience` 為 `male`
- **THEN** 標準化內容帶有 `audience: "male"`，且 `content_hash` 與同內容但全讀者者不同

#### Scenario: 既有內容的雜湊不變
- **WHEN** 驗證一份在加入此欄位之前就合法、沒有 `audience` 的內容
- **THEN** 得到的 `content_hash` 與加入此欄位之前相同

#### Scenario: 標準化內容可再被驗證
- **WHEN** 把帶有 `audience: "female"` 的標準化輸出再驗證一次
- **THEN** 得到相同的標準化內容與 `content_hash`

#### Scenario: 不合法的頻道
- **WHEN** `audience` 為 `Male`、`boys` 或數字 `1`
- **THEN** 驗證失敗，錯誤指出 `audience` 且訊息不含輸入的值

#### Scenario: 不計入文字總量
- **WHEN** 兩份文字完全相同的內容，一份為 `male`、一份為全讀者
- **THEN** 兩者的文字總量相同
