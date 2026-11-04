# Spec Delta

## ADDED Requirements

### Requirement: 送審紀錄保存 12 個月
送審紀錄（`script_submissions`，只含使用者、結果、類別列舉與時間）建立時間早於 12 個月（以 365 天計）者 SHALL 被清除。這項清除 SHALL 納入既有的過期資料清除指令，沿用預覽模式與分批處理，輸出 SHALL 只含筆數。清除 SHALL 不影響當下的暫停發佈旗標與當日的送審次數判斷（它們只看近期的紀錄）。

#### Scenario: 超過 12 個月
- **WHEN** 一筆送審紀錄已建立超過 365 天
- **THEN** 該紀錄被刪除

#### Scenario: 未滿 12 個月
- **WHEN** 一筆送審紀錄建立在 364 天前
- **THEN** 不被刪除

#### Scenario: 預覽
- **WHEN** 以預覽模式執行
- **THEN** 輸出將清除的筆數，沒有刪除任何資料

#### Scenario: 不影響暫停狀態
- **WHEN** 被暫停發佈的帳號的舊送審紀錄被清除
- **THEN** 暫停發佈的旗標不變
