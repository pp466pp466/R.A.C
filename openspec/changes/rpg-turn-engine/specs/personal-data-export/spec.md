# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入第 0 幕
個人資料匯出的每個存檔 SHALL 含第 0 幕的狀態與文字（`pending` 或空文字時為空）。第 0 幕是由玩家的主角衍生的文字，視為個資，SHALL 隨存檔匯出；JSON 與 CSV 的存檔資料 SHALL 各含該欄位。匯出 SHALL NOT 含開始生成的時間與內部編號。

#### Scenario: 有第 0 幕
- **WHEN** 有第 0 幕的存檔被匯出
- **THEN** 匯出的存檔含第 0 幕的狀態與文字

#### Scenario: 尚未生成
- **WHEN** 第 0 幕仍是 `pending` 的存檔被匯出
- **THEN** 該存檔的第 0 幕文字為空
