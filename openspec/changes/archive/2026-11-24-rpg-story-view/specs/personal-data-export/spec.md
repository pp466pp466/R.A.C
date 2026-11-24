# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入行動建議
每個存檔的匯出 SHALL 含目前保存的行動建議文字（沒有時為空），JSON 為字串陣列、CSV 的 `sessions.csv` 為單一欄位（各項以換行連接）；SHALL NOT 含錨點與內部編號。建議由玩家文字衍生，視為個資，與存檔的其他內容同樣只匯出給擁有者。

#### Scenario: 有建議
- **WHEN** 使用者的存檔保存了三個建議後匯出
- **THEN** JSON 的該存檔含這三個字串，`sessions.csv` 該列的建議欄含三行文字

#### Scenario: 沒有建議
- **WHEN** 存檔沒有建議
- **THEN** 匯出該欄位為空，其他欄位不變
