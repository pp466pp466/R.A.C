# Spec Delta

## ADDED Requirements

### Requirement: 存檔在最後活動後 12 個月清除
最後活動時間（存檔的 `last_active_at`，每次開始或完成回合時更新；沒有任何回合的存檔以建立時間計算）早於 12 個月（以 365 天計）的存檔 SHALL 被清除，連同其全部回合與記事簿，不論存檔是進行中或已完結。這項清除 SHALL 納入既有的過期資料清除指令，沿用預覽模式與分批處理，輸出 SHALL 只含筆數。

#### Scenario: 超過 12 個月
- **WHEN** 一個存檔最後活動已超過 365 天
- **THEN** 該存檔與其回合被刪除

#### Scenario: 未滿 12 個月
- **WHEN** 一個存檔最後活動在 364 天前
- **THEN** 不被刪除

#### Scenario: 只看最後活動
- **WHEN** 一個存檔建立已超過 365 天，但最近完成了回合
- **THEN** 不被刪除

#### Scenario: 沒有回合的存檔
- **WHEN** 一個沒有任何回合的存檔建立已超過 365 天
- **THEN** 被刪除

#### Scenario: 已完結的存檔
- **WHEN** 一個已完結的存檔最後活動已超過 365 天
- **THEN** 被刪除

#### Scenario: 預覽
- **WHEN** 以預覽模式執行
- **THEN** 輸出將清除的存檔筆數，沒有刪除任何資料

### Requirement: 中斷與被擋回合的部分內容保存 14 天
狀態為 `interrupted` 或 `blocked` 的回合，其已生成的部分劇情內容超過 14 天 SHALL 被清為空字串；回合紀錄本身（序號、編號、狀態與時間）SHALL 保留，使冪等與統計不受影響。`complete` 與 `streaming` 的回合 SHALL NOT 受影響。這項清除 SHALL 納入既有的過期資料清除指令，沿用預覽模式與分批處理。

#### Scenario: 超過 14 天
- **WHEN** 一個 `interrupted` 回合已超過 14 天且有部分內容
- **THEN** 部分內容被清為空字串，回合紀錄仍在

#### Scenario: 未滿 14 天
- **WHEN** 一個 `interrupted` 回合只有 13 天
- **THEN** 部分內容保留

#### Scenario: 被擋的回合
- **WHEN** 一個 `blocked` 回合已超過 14 天
- **THEN** 部分內容被清除

#### Scenario: 已完成的回合不受影響
- **WHEN** 一個 `complete` 回合已超過 14 天
- **THEN** 劇情原文不變
