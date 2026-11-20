# Spec Delta

## MODIFIED Requirements

### Requirement: 清單依故事偏好排序並分頁
清單 SHALL 以玩家的故事偏好排序（符合偏好的頻道最前，其次全讀者，其餘最後；偏好為全讀者時不重排；同一層依發佈時間由新到舊），排序 SHALL 在分頁之前完成，使分頁之間的順序一致。偏好 SHALL 只影響順序，SHALL NOT 增減清單包含的版本。分頁 SHALL 以游標進行：`limit`（預設 12、至多 24）與 `cursor`（上一頁回應的 `next_cursor`）控制分頁，回應 SHALL 含 `has_more` 與 `next_cursor`（沒有更多時為空）；篩選與關鍵字見 `rpg-script-catalog`。`limit` 不是合法整數（含負數、超過上限）或 `cursor` 損毀 SHALL 回 400 並指出欄位；`offset` 參數 SHALL 不再被讀取。

#### Scenario: 依偏好排序
- **WHEN** 偏好為男頻的玩家讀取清單，有男頻、全讀者、女頻各一份上架版本
- **THEN** 順序為男頻、全讀者、女頻

#### Scenario: 偏好為全讀者
- **WHEN** 偏好為全讀者的玩家讀取清單
- **THEN** 順序為發佈時間由新到舊

#### Scenario: 分頁串起來等於一次取得
- **WHEN** 以 `limit=2` 沿著 `next_cursor` 依序讀取每一頁直到 `has_more` 為假
- **THEN** 各頁串起來的順序與一次取得全部的順序相同，沒有重複或遺漏

#### Scenario: 偏好不改變集合
- **WHEN** 同一位玩家把偏好依序改為男頻、女頻與全讀者並讀取全部頁面
- **THEN** 每次得到的版本集合相同

#### Scenario: 不合法的分頁參數
- **WHEN** `limit` 為 `0`、`25` 或 `abc`，或 `cursor` 為損毀的字串
- **THEN** 回應 400 並指出該欄位，不回傳清單

#### Scenario: 最後一頁
- **WHEN** 讀到最後一頁
- **THEN** `has_more` 為假，`next_cursor` 為空
