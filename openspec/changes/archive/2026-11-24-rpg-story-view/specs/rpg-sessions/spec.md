# Spec Delta

## ADDED Requirements

### Requirement: 存檔保存行動建議
存檔 SHALL 有可為空的欄位保存最近一次產生的行動建議（建議文字陣列）與其錨點（最後一個完成回合的序號與修訂版本；尚無完成回合時為第 0 幕）。欄位 SHALL 沒有預設的非空值：舊資料列與舊版程式照常運作，視為沒有建議。存檔快照 SHALL 含 `suggestions`：錨點等於目前錨點時為建議文字陣列，過期或沒有時為空；快照 SHALL NOT 含錨點的內部細節。建議 SHALL 隨存檔刪除，且 SHALL NOT 改變存檔的 `version` 或世界狀態。

#### Scenario: 新存檔
- **WHEN** 開出新存檔
- **THEN** 沒有建議，快照的 `suggestions` 為空

#### Scenario: 寫入不影響世界狀態版本
- **WHEN** 為存檔保存一組建議
- **THEN** 存檔的 `version`、世界狀態、記事簿與回合列都不變

#### Scenario: 錨點改變後
- **WHEN** 存檔保存的建議之後又完成了一個新回合
- **THEN** 快照的 `suggestions` 為空
