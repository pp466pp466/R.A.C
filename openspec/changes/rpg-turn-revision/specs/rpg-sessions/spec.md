## ADDED Requirements

### Requirement: 回合的修訂欄位
回合 SHALL 帶有修訂資料：`revision`（從 1 起，每次成功的修訂加 1）、`regen_count`（目前這份輸入已成功重新生成的次數，編輯後歸零）、修訂認領時間（無認領時為空）與回合前快照（見回合引擎）。這些欄位 SHALL 為可為空或有預設值（舊資料列與舊版程式照常運作）；舊資料列的 `revision` 視為 1、`regen_count` 視為 0、快照為空。修訂 SHALL NOT 改變回合的序號、輪數與 `client_msg_id`。

#### Scenario: 新回合的預設值
- **WHEN** 新回合建立
- **THEN** `revision` 為 1、`regen_count` 為 0、沒有認領時間

#### Scenario: 修訂成功
- **WHEN** 最後一輪被成功修訂
- **THEN** `revision` 加 1，序號、輪數與 `client_msg_id` 不變，認領時間清空

### Requirement: 玩家端回合視圖帶 revision
回合視圖 SHALL 含 `revision`。存檔快照 SHALL 含最後一輪的修訂資訊 `last_turn`：回合的 `client_msg_id`、`seq`、`revision`、是否可修訂（`revisable`）、不可修訂的原因（`ended`、`in_progress`、`no_snapshot`，可修訂時為空）、剩餘免費重新生成次數與兩種操作目前的幣價；沒有完成的回合時為空。視圖 SHALL NOT 含快照、認領時間與內部編號。

#### Scenario: 回合視圖
- **WHEN** 讀取回合清單
- **THEN** 每個回合都帶 `revision`

#### Scenario: 可修訂的快照
- **WHEN** 最後一輪可被修訂且已用 1 次免費重新生成
- **THEN** `last_turn.revisable` 為真，剩餘免費次數為 1，重新生成的幣價為 0

#### Scenario: 免費次數用完
- **WHEN** 免費次數已用完
- **THEN** 重新生成的幣價為一輪的幣價

#### Scenario: 已完結
- **WHEN** 存檔已完結
- **THEN** `last_turn.revisable` 為假，原因為 `ended`
