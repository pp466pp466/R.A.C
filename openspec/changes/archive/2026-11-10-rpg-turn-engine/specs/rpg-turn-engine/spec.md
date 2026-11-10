# Spec Delta

## Purpose

定義 RPG 一個回合從玩家送出行動到完成的生命週期：開始前的閘門與輸入檢查、遊戲幣的預扣與結算時機、完成時的寫入、中斷與逾時的處理，以及自訂結局如何讓存檔完結。串流協定、上下文、世界狀態與輸出審查另見各自的能力。

## ADDED Requirements

### Requirement: 開始回合前先過閘門與輸入檢查
開始回合 SHALL 依序：通過帳號與年齡閘門；驗證 `client_msg_id`（1 至 64 字）與行動文字（去除控制字元並做 NFC 正規化，1 至 1000 字，空白行動在 Service 層擋下）；對行動文字執行規則層；確認全站每日 token 上限未用盡。任何一步失敗 SHALL 在預扣遊戲幣之前結束，不新增回合、不呼叫模型。

#### Scenario: 空白行動
- **WHEN** 玩家送出只有空白的行動
- **THEN** 回應 400 與欄位錯誤，沒有預扣、沒有新增回合、沒有呼叫模型

#### Scenario: 未完成年齡確認
- **WHEN** 尚未完成年齡確認的玩家送出行動
- **THEN** 回應 403 與代碼 `age_confirmation_required`

#### Scenario: 全站預算用盡
- **WHEN** 全站每日 token 已達上限
- **THEN** 回應 503 與繁體中文的系統忙碌文案，沒有預扣

### Requirement: 玩家行動的規則層只擋確定的違規
行動文字 SHALL 通過規則層：命中必須拒絕的類別（涉及未成年的性內容、非自願的性情節、性暴力、以真人名義的性內容）時 SHALL 回應 422 與代碼 `input_blocked`，只帶類別，不回顯內容，也不預扣、不呼叫模型。只因內容模式較保守而出現的「分級不符」SHALL NOT 阻擋行動（輸出層會依模式處理），系統只可提醒玩家能調整模式，SHALL NOT 自動調高。

#### Scenario: 明確違規的行動
- **WHEN** 行動文字命中必須拒絕的類別
- **THEN** 回應 422 與代碼 `input_blocked`，回應只含類別，沒有預扣

#### Scenario: 安全模式下的曖昧行動
- **WHEN** 安全模式的玩家送出一句曖昧的行動
- **THEN** 回合正常開始，內容模式不被調高

### Requirement: 第 0 幕未完成時不得開始回合
存檔的第 0 幕狀態不是 `ready` 或 `fallback`（含尚未生成與生成中）時，開始回合 SHALL 被拒絕為衝突（409，代碼 `opening_pending`），不預扣、不新增回合。第 0 幕狀態為空值的舊存檔 SHALL 視為 `fallback` 而不受限制。

#### Scenario: 開場尚未完成
- **WHEN** 新存檔的第 0 幕仍是 `pending`，玩家直接送出行動
- **THEN** 回應 409 與代碼 `opening_pending`

#### Scenario: 舊存檔
- **WHEN** 遷移之前建立的存檔（第 0 幕狀態為空值）送出行動
- **THEN** 回合正常開始

### Requirement: 回合開始時以含嘗試序號的冪等鍵預扣遊戲幣
新回合與重送的回合 SHALL 以冪等鍵 `turn:{存檔識別碼}:{client_msg_id}:{attempt}` 預扣一輪的幣價；`attempt` 從 1 起，每次因失敗或中斷而重新開始同一個 `client_msg_id` 時加 1。已 `complete` 的回合重送 SHALL 直接回傳既有結果，不碰錢包。餘額不足 SHALL 回應 409 與代碼 `insufficient_coins`（帶餘額與所需數量），且 SHALL NOT 留下新的回合列。

#### Scenario: 新回合
- **WHEN** 餘額足夠的玩家送出新行動
- **THEN** 錢包預扣一輪的幣價，回合列的 `attempt` 為 1

#### Scenario: 中斷後重送
- **WHEN** 一個已中斷（預扣已退回）的 `client_msg_id` 被重送
- **THEN** `attempt` 加 1、使用新的冪等鍵重新預扣，沒有出現免費生成

#### Scenario: 已完成的回合重送
- **WHEN** 重送已 `complete` 的 `client_msg_id`
- **THEN** 回傳既有結果，錢包沒有任何異動

#### Scenario: 餘額不足
- **WHEN** 合計餘額低於一輪的幣價
- **THEN** 回應 409 與代碼 `insufficient_coins`，資料庫中沒有這個回合列

### Requirement: 預扣依結果結算或退回
回合完整生成並通過審查後 SHALL 結算預扣；失敗、被擋（自動重生仍被擋）與文字尚未完成前的中斷 SHALL 退回預扣。被擋後的自動重生 SHALL 沿用同一筆預扣，不再次扣款。狀態呼叫失敗但文字已送達的回合 SHALL 照常結算。結算與退回 SHALL 各自冪等且互斥，並 SHALL 在回合列離開生成中的同一次寫入流程完成。

#### Scenario: 成功
- **WHEN** 回合完成
- **THEN** 預扣被結算，玩家淨扣一輪的幣價

#### Scenario: 生成失敗
- **WHEN** 主要與備援模型都失敗，沒有任何文字送達
- **THEN** 預扣被退回，玩家餘額回到回合開始前

#### Scenario: 自動重生成功
- **WHEN** 第一次生成被擋、自動重生的結果通過審查
- **THEN** 只有一筆預扣，結算後玩家淨扣一輪的幣價

#### Scenario: 兩次都被擋
- **WHEN** 第一次與自動重生都被擋
- **THEN** 預扣被退回，回合為 `blocked`

#### Scenario: 狀態呼叫失敗
- **WHEN** 文字已完整送達，狀態呼叫重試後仍失敗
- **THEN** 回合仍為 `complete`，預扣被結算

### Requirement: 完成回合時指派輪數並寫入世界狀態
回合完成 SHALL 在一個短交易內：指派輪數（已完成的回合數加 1）、保存劇情原文與結構化結果、以預期的版本號寫入通過驗證的世界狀態（版本加 1）。串流期間 SHALL NOT 持有資料庫交易；寫回 SHALL NOT 依賴請求結束時的清理。`interrupted` 與 `blocked` 的回合 SHALL NOT 有輪數、SHALL NOT 進入上下文。

#### Scenario: 完成
- **WHEN** 第一個回合完成
- **THEN** 輪數為 1、存檔版本加 1、劇情原文已保存

#### Scenario: 串流期間不持有交易
- **WHEN** 模型串流進行中
- **THEN** 該請求沒有未提交的資料庫交易

### Requirement: 連線中斷的處理
文字生成尚未完成時客戶端斷線，系統 SHALL 停止生成並關閉上游串流，回合標記為 `interrupted` 並保存已收到的片段，退回預扣，並以輸出字數估算用量記入帳本。文字已完整生成並通過審查之後才斷線，回合 SHALL 照常完成並結算；玩家以相同 `client_msg_id` 重送即可取得既有結果。

#### Scenario: 生成中斷線
- **WHEN** 客戶端在串流進行中斷線
- **THEN** 回合為 `interrupted`、預扣被退回、上游連線被關閉

#### Scenario: 狀態呼叫期間斷線
- **WHEN** 文字已完整送達，客戶端在狀態呼叫期間斷線
- **THEN** 回合仍完成並結算，重送相同編號得到既有結果

### Requirement: 卡在生成中的回合可被接手
`streaming` 狀態的回合更新時間超過預扣逾時秒數（資料庫設定表的 `coin.hold_timeout_seconds`）時，SHALL 視為已中斷：以相同 `client_msg_id` 重送 SHALL 接手該筆，`attempt` 加 1 並重新預扣。未逾時的 `streaming` 回合重送 SHALL 回應衝突（409，代碼 `turn_in_progress`）。

#### Scenario: worker 重啟後重送
- **WHEN** 回合停在 `streaming` 超過逾時秒數，玩家重送相同編號
- **THEN** 該筆被接手為新的嘗試，舊預扣已被錢包的逾時規則退回，玩家只被扣一輪

#### Scenario: 尚未逾時
- **WHEN** 回合 `streaming` 且未逾時，玩家重送相同編號
- **THEN** 回應 409 與代碼 `turn_in_progress`

### Requirement: 自訂結局使存檔完結
狀態呼叫提議結局時，Service SHALL 先確認該存檔的劇本複本結局模式為自訂結局才生效；無結局的劇本 SHALL 忽略提議。壽終年齡 SHALL 由 Service 判定：回合開始時主角目前年齡不低於結局年齡，該回合即為結局回合。結局回合的文字 SHALL 照常送完，之後存檔轉為已完結並記錄完結時間；已完結的存檔 SHALL 拒絕新回合。

#### Scenario: 無結局劇本
- **WHEN** 無結局的劇本，模型提議結局
- **THEN** 提議被忽略，存檔仍為進行中

#### Scenario: 模型提議結局
- **WHEN** 自訂結局的劇本，狀態呼叫提議結局
- **THEN** 該回合文字照常送完，`done` 標示存檔已完結，之後新回合被拒絕為 `session_ended`

#### Scenario: 壽終年齡
- **WHEN** 自訂結局且主角目前年齡已不低於結局年齡
- **THEN** 該回合為結局回合，完成後存檔轉為已完結
