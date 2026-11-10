# Spec Delta

## MODIFIED Requirements

### Requirement: 存檔的初始狀態
新存檔 SHALL 為進行中、版本號 1、已經過的遊戲月數為 0、記事簿為空且記事簿版本為 1、記事簿已處理到的輪次為 0、目標清單的進行中與已歸檔皆為空、里程碑進度為空、NPC 狀態為空，第 0 幕的狀態為 `pending` 且文字為空，並記錄開存檔當下的內容模式（玩家目前生效的模式與劇本宣告兩者中較保守者）。存檔 SHALL 記錄主角的角色卡與錨定事實（見 `rpg-protagonist`；錨定事實可為空），主角的姓名與角色年齡（劇中角色開存檔時的年齡，與使用者的年齡無關，使用者年齡一律由生日計算）SHALL 由角色卡同步得出。

#### Scenario: 初始值
- **WHEN** 開出一個新存檔
- **THEN** 各欄位為上述初始值，記事簿為空字串，第 0 幕為 `pending`

#### Scenario: 內容模式取較保守者
- **WHEN** 生效模式為 `standard` 的玩家用宣告為 `safe` 的劇本開存檔
- **THEN** 存檔的內容模式為 `safe`

#### Scenario: 新存檔不受其他存檔影響
- **WHEN** 同一位玩家已有一個記事簿有內容的存檔，再開一個新存檔
- **THEN** 新存檔的記事簿為空

#### Scenario: 主角資料
- **WHEN** 以合法的角色卡與錨定事實開存檔
- **THEN** 存檔記錄該角色卡與錨定事實，姓名與角色年齡與角色卡一致

#### Scenario: 略過錨定事實
- **WHEN** 玩家明確略過錨定事實開存檔
- **THEN** 存檔有角色卡，沒有錨定事實

### Requirement: 回合的唯一性與冪等
每個回合 SHALL 帶有前端產生的唯一編號（`client_msg_id`），同一存檔內 SHALL 唯一，同一存檔內的回合 SHALL 有嚴格遞增且唯一的序號（`seq`），並帶有嘗試序號（`attempt`，從 1 起）。回合狀態只有 `streaming`、`complete`、`interrupted`、`blocked`。以已存在的 `client_msg_id` 再次開始回合時：原回合為 `complete` SHALL 直接回傳既有回合而不新增資料；原回合為 `interrupted` 或 `blocked` SHALL 允許重新生成並覆蓋該筆，`attempt` 加 1；原回合為 `streaming` 且未逾時 SHALL 回應衝突，逾時（超過預扣逾時秒數未更新）則視為中斷並接手，`attempt` 加 1。同一存檔同時 SHALL 只有一個未逾時的 `streaming` 回合，其他編號的新回合 SHALL 回應衝突。並行地以同一個 `client_msg_id` 開始回合 SHALL 最多產生一筆。

#### Scenario: 重送已完成的回合
- **WHEN** 以已 `complete` 回合的 `client_msg_id` 再次開始回合
- **THEN** 回傳原回合，回合總數不變，`attempt` 不變

#### Scenario: 重送中斷的回合
- **WHEN** 以 `interrupted` 回合的 `client_msg_id` 再次開始回合
- **THEN** 該筆被重設為 `streaming`，沒有新增第二筆，`attempt` 加 1

#### Scenario: 串流中的重送
- **WHEN** 原回合仍為未逾時的 `streaming` 時以相同編號再次開始
- **THEN** 回應衝突

#### Scenario: 逾時的串流被接手
- **WHEN** 原回合停在 `streaming` 超過逾時秒數，以相同編號再次開始
- **THEN** 該筆被接手，`attempt` 加 1

#### Scenario: 同一存檔的另一個編號
- **WHEN** 存檔已有未逾時的 `streaming` 回合，以不同編號開始另一個回合
- **THEN** 回應衝突，沒有新增回合

#### Scenario: 並行
- **WHEN** 兩個請求同時以同一個 `client_msg_id` 開始回合
- **THEN** 資料庫中只有一筆

#### Scenario: 序號遞增
- **WHEN** 依序開始三個回合
- **THEN** 序號嚴格遞增
