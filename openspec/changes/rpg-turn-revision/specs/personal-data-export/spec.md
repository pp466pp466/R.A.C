## ADDED Requirements

### Requirement: 匯出納入重新生成回饋
個人資料匯出的每個存檔 SHALL 含該存檔的重新生成回饋：回合序號、種類（重新生成或編輯）、分類、理由、是否免費、結果與時間；JSON 放在存檔區塊內，CSV 在 zip 內新增 `turn_regenerations.csv`（UTF-8-sig，公式字元開頭者依既有規則中和）。匯出 SHALL NOT 含內部編號、冪等鍵與回合前快照。沒有回饋的使用者 SHALL 得到空清單。

#### Scenario: JSON 含回饋
- **WHEN** 有 2 筆回饋的存檔被匯出為 JSON
- **THEN** 該存檔區塊含這 2 筆，包含失敗與被擋的嘗試

#### Scenario: CSV 含回饋
- **WHEN** 匯出 CSV
- **THEN** zip 內有 `turn_regenerations.csv`，每筆一列

#### Scenario: 不含內部資料
- **WHEN** 檢查匯出內容
- **THEN** 沒有內部整數編號與回合前快照
