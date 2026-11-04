# rpg-sessions Specification

## Purpose
定義 RPG 存檔與回合的資料行為：開存檔時複製劇本內容、擁有者隔離、世界狀態與記事簿欄位、版本號的衝突偵測、回合的唯一性與冪等、中斷的回合不計輪，以及「已完結」的存檔不得再新增回合。回合引擎、串流與介面建立在這個基礎上，由後續的 change 提供。

## Requirements


### Requirement: 開存檔時複製劇本內容
開存檔 SHALL 以一個劇本版本為來源，並把該版本標準化後的完整內容（固定規格的全部欄位與 `spec_version`）複製進存檔。之後劇本被修改、被取代、被下架或被刪除，進行中與已完結的存檔 SHALL NOT 受到影響，存檔也 SHALL NOT 回頭讀取劇本。存檔 SHALL 以可為空的參照記錄來源版本，來源被刪除時參照被清為空值而不阻止刪除。複本在存檔建立之後 SHALL NOT 被修改。

#### Scenario: 複本與來源一致
- **WHEN** 玩家用一個已發佈的劇本版本開存檔
- **THEN** 存檔裡的劇本內容與該版本完全相同

#### Scenario: 來源之後被修改
- **WHEN** 存檔開出後，作者發佈了新版本並取代了舊版
- **THEN** 存檔裡的內容仍是開存檔當時的版本

#### Scenario: 來源被刪除
- **WHEN** 存檔開出後，來源劇本被刪除
- **THEN** 存檔仍在且內容不變，來源參照為空值

### Requirement: 誰可以用哪個版本開存檔
開存檔 SHALL 先通過帳號與年齡閘門（帳號啟用、已完成年齡確認、年齡不低於最低使用年齡，管理員與各會員等級不豁免）。任何通過的玩家 SHALL 只能用 `published` 的版本開存檔；擁有者 SHALL 另外可以用自己劇本的任何版本開存檔。非擁有者用他人的非上架版本開存檔 SHALL 得到 404。劇本宣告的 `content_tier` 高於玩家目前生效的內容模式時 SHALL 被拒絕（`403`，代碼 `content_tier_restricted`），不開出存檔。非內建劇本的版本 SHALL 另外通過模型層審查的把關（對應目前內容的結果為 `pass` 或 `flag`，見 `script-review`），未通過時不開出存檔。

#### Scenario: 用已發佈的版本
- **WHEN** 已完成年齡確認的成年玩家用內建劇本開存檔
- **THEN** 開出存檔，狀態為進行中

#### Scenario: 未完成年齡確認
- **WHEN** 尚未完成年齡確認的玩家嘗試開存檔
- **THEN** 回應 403 與代碼 `age_confirmation_required`，沒有開出存檔

#### Scenario: 擁有者用自己的草稿
- **WHEN** 作者用自己的 `draft` 版本開存檔，且該內容的模型層審查為 `pass`
- **THEN** 開出存檔

#### Scenario: 非擁有者用他人的草稿
- **WHEN** 另一位玩家嘗試用別人的 `draft` 版本開存檔
- **THEN** 得到與不存在相同的 404

#### Scenario: 劇本分級高於玩家模式
- **WHEN** 安全模式的玩家嘗試用宣告為 `standard` 的劇本開存檔
- **THEN** 回應 403 與代碼 `content_tier_restricted`，沒有開出存檔

#### Scenario: 審查未通過
- **WHEN** 作者用審查結果為 `reject` 的草稿開存檔
- **THEN** 回應 422 與代碼 `script_rejected`，沒有開出存檔

#### Scenario: 審查不可用
- **WHEN** 草稿尚未審查，而模型層無法給出結果
- **THEN** 回應 503 與代碼 `review_unavailable`，沒有開出存檔

### Requirement: 存檔的初始狀態
新存檔 SHALL 為進行中、版本號 1、已經過的遊戲月數為 0、記事簿為空且記事簿版本為 1、記事簿已處理到的輪次為 0、目標清單的進行中與已歸檔皆為空、里程碑進度為空、NPC 狀態為空，並記錄開存檔當下的內容模式（玩家目前生效的模式與劇本宣告兩者中較保守者）。存檔 SHALL 記錄主角的姓名與角色年齡（劇中角色的年齡，與使用者的年齡無關，使用者年齡一律由生日計算）。

#### Scenario: 初始值
- **WHEN** 開出一個新存檔
- **THEN** 各欄位為上述初始值，記事簿為空字串

#### Scenario: 內容模式取較保守者
- **WHEN** 生效模式為 `standard` 的玩家用宣告為 `safe` 的劇本開存檔
- **THEN** 存檔的內容模式為 `safe`

#### Scenario: 新存檔不受其他存檔影響
- **WHEN** 同一位玩家已有一個記事簿有內容的存檔，再開一個新存檔
- **THEN** 新存檔的記事簿為空

### Requirement: 擁有者隔離
所有存檔與回合的讀寫方法 SHALL 接收擁有者的使用者編號並以它為查詢條件，不提供只憑識別碼取得資料的方法。存檔 SHALL 有對外識別碼（UUID），網址與 API 只使用它；非擁有者 SHALL 得到與「不存在」相同的 404，授權不依賴識別碼的不可猜測性。管理員 SHALL NOT 能經由這些方法讀取他人的存檔。

#### Scenario: 非擁有者讀存檔
- **WHEN** 使用者 B 以正確的識別碼讀取使用者 A 的存檔
- **THEN** 得到與不存在相同的 404

#### Scenario: 列表只含自己的
- **WHEN** 使用者 A 列出自己的存檔
- **THEN** 只含 A 自己的存檔

#### Scenario: 管理員不能讀他人存檔
- **WHEN** 管理員以這些方法讀取玩家的存檔
- **THEN** 得到 404

### Requirement: 回合的唯一性與冪等
每個回合 SHALL 帶有前端產生的唯一編號（`client_msg_id`），同一存檔內 SHALL 唯一，同一存檔內的回合 SHALL 有嚴格遞增且唯一的序號（`seq`）。回合狀態只有 `streaming`、`complete`、`interrupted`、`blocked`。以已存在的 `client_msg_id` 再次開始回合時：原回合為 `complete` SHALL 直接回傳既有回合而不新增資料；原回合為 `interrupted` 或 `blocked` SHALL 允許重新生成並覆蓋該筆；原回合為 `streaming` SHALL 回應衝突。並行地以同一個 `client_msg_id` 開始回合 SHALL 最多產生一筆。

#### Scenario: 重送已完成的回合
- **WHEN** 以已 `complete` 回合的 `client_msg_id` 再次開始回合
- **THEN** 回傳原回合，回合總數不變

#### Scenario: 重送中斷的回合
- **WHEN** 以 `interrupted` 回合的 `client_msg_id` 再次開始回合
- **THEN** 該筆被重設為 `streaming`，沒有新增第二筆

#### Scenario: 串流中的重送
- **WHEN** 原回合仍為 `streaming` 時以相同編號再次開始
- **THEN** 回應衝突

#### Scenario: 並行
- **WHEN** 兩個請求同時以同一個 `client_msg_id` 開始回合
- **THEN** 資料庫中只有一筆

#### Scenario: 序號遞增
- **WHEN** 依序開始三個回合
- **THEN** 序號嚴格遞增

### Requirement: 中斷的回合不計輪
輪數 SHALL 只計 `complete` 的回合：回合完成時才被指派輪數（目前已完成的回合數加 1），`streaming`、`interrupted` 與 `blocked` 的回合 SHALL NOT 有輪數，也 SHALL NOT 計入記事簿「每完成 10 輪」的計數或進入上下文。

#### Scenario: 完成才有輪數
- **WHEN** 第一個回合開始、尚未完成
- **THEN** 它沒有輪數；完成後輪數為 1

#### Scenario: 中斷不佔輪數
- **WHEN** 第二個回合中斷，之後第三個回合完成
- **THEN** 第三個回合的輪數為 2

### Requirement: 世界狀態以版本號偵測衝突
存檔的世界狀態（所在地點、狀態摘要、NPC 狀態、目標與里程碑進度、已過經的月數）SHALL 帶版本號，更新 SHALL 以預期的版本號為條件，成功時版本號加 1；版本號不一致 SHALL 拒絕寫入並回應版本衝突（沿用既有的 `VersionConflict`），以後端為準。

#### Scenario: 版本一致
- **WHEN** 以目前的版本號更新世界狀態
- **THEN** 更新成功，版本號加 1

#### Scenario: 版本落後
- **WHEN** 以較舊的版本號更新
- **THEN** 回應版本衝突，狀態不變

### Requirement: 已完結的存檔不得再新增回合
存檔 SHALL 能被標記為「已完結」並記錄完結時間；已完結的存檔 SHALL 保持可讀（歷史、世界狀態與記事簿），對它開始新回合 SHALL 被拒絕為衝突（代碼 `session_ended`），資料不變。已完結的存檔 SHALL NOT 被改回進行中；玩家可用同一個劇本開新存檔。玩家自行封存（`is_archived`）與劇情的完結是兩件不同的事，互不影響。

#### Scenario: 完結
- **WHEN** 存檔被標記為已完結
- **THEN** 狀態為已完結，有完結時間

#### Scenario: 已完結後開始回合
- **WHEN** 對已完結的存檔開始新回合
- **THEN** 回應衝突與代碼 `session_ended`，沒有新增回合

#### Scenario: 仍可讀
- **WHEN** 讀取已完結的存檔
- **THEN** 取得歷史回合、世界狀態與記事簿

#### Scenario: 不可恢復
- **WHEN** 嘗試把已完結的存檔改回進行中
- **THEN** 被拒絕，仍為已完結

#### Scenario: 同劇本開新存檔
- **WHEN** 玩家用同一個劇本在原存檔完結之後再開存檔
- **THEN** 開出新的進行中存檔

### Requirement: 存檔與回合的刪除與日誌
存檔隨其擁有者被抹除時 SHALL 連同全部回合一併刪除，外鍵 SHALL 偏向串聯刪除。存檔與回合的內容（使用者行動、劇情原文、記事簿、世界狀態、劇本複本）SHALL NOT 出現在日誌中；日誌 SHALL 只記內部編號、列舉值與整數長度。

#### Scenario: 抹除一併刪除
- **WHEN** 有存檔與回合的使用者被抹除
- **THEN** 存檔與回合都不存在

#### Scenario: 日誌不含內容
- **WHEN** 建立存檔、完成回合並收集期間的日誌
- **THEN** 日誌中找不到使用者行動、劇情原文、記事簿或劇本文字
