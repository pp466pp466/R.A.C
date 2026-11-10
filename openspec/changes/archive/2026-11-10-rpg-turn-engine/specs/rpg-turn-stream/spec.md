# Spec Delta

## Purpose

定義 RPG 劇情的串流端點與 Server-Sent Events 協定：路徑、事件種類與內容、回應標頭、心跳與斷線、已完成回合的重送、唯讀端點，以及錯誤、快取與 CSRF 的行為。前端接收與打字機渲染屬後續的介面 change。

## ADDED Requirements

### Requirement: 回合與第 0 幕的串流端點
系統 SHALL 提供 `POST /api/game/sessions/<id>/turns/stream`（本文 `client_msg_id`、`action`）與 `POST /api/game/sessions/<id>/opening/stream`，回應為 `text/event-stream`。端點 SHALL 要求登入並通過 CSRF 與 Origin 檢查；`<id>` 是存檔的對外識別碼，非擁有者與不存在 SHALL 得到相同的 404。開始前的失敗（驗證、閘門、餘額、衝突、404）SHALL 以一般 JSON 錯誤回應，不開啟事件串流。

#### Scenario: 非擁有者
- **WHEN** 使用者 B 對使用者 A 的存檔送出回合
- **THEN** 得到與不存在相同的 404 JSON

#### Scenario: 沒有 CSRF 權杖
- **WHEN** 沒帶 CSRF 權杖送出
- **THEN** 被拒絕，沒有預扣

#### Scenario: 餘額不足
- **WHEN** 餘額不足的玩家送出行動
- **THEN** 回應 409 JSON 與代碼 `insufficient_coins`，不是事件串流

### Requirement: 事件種類與格式
事件格式 SHALL 為 `event: 名稱` 加單行 `data:` JSON，以空行分隔。事件種類 SHALL 只有：`meta`（`turn_id`、`client_msg_id`、`attempt`，第 0 幕為 `kind`）、`token`（`text`）、`ping`、`transition`（`text`、`will_retry`）、`state`（世界狀態摘要與通知）、`done`、`error`（`code`、繁體中文 `message`）。`done` SHALL 是明確的結束標記，含結束狀態（`complete`、`blocked`、`ended` 之一）、輪數（被擋為空）與剩餘遊戲幣。JSON 原文 SHALL NOT 以串流送給前端：模型的結構化輸出只經 Service 驗證後以 `state` 的白名單內容送出。

#### Scenario: 正常回合的事件順序
- **WHEN** 一個回合順利完成
- **THEN** 事件依序為 `meta`、若干 `token`、`state`、`done`，且 `done` 是最後一個事件

#### Scenario: 事件格式
- **WHEN** 檢查任一事件
- **THEN** 它是 `event:` 一行、`data:` 一行單行 JSON、空行

#### Scenario: 狀態事件不含模型原文
- **WHEN** 檢查 `state` 事件
- **THEN** 其中只有驗證後的欄位與年齡更正等通知，沒有模型輸出的原始 JSON

### Requirement: 回應標頭與心跳
串流回應 SHALL 帶 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`。伺服器 SHALL 在沒有其他輸出時每 15 秒送出 `ping`，並在開始狀態呼叫前送出一個 `ping`。客戶端斷線 SHALL 在下一次寫入時被偵測，偵測到後 SHALL 停止生成並關閉上游串流。

#### Scenario: 標頭
- **WHEN** 開始一個串流
- **THEN** 回應標頭符合上述三項

#### Scenario: 模型暫時沒有輸出
- **WHEN** 上游超過 15 秒沒有任何片段
- **THEN** 客戶端在這段期間收到 `ping`

### Requirement: 單次生成有時間上限
首個非空正文片段 SHALL 在 20 秒內到達，單次串流生成 SHALL 在 60 秒內結束。逾時 SHALL 送出 `error`（繁體中文訊息）並結束，回合依「中斷與失敗」處理並退回預扣。

#### Scenario: 首字逾時
- **WHEN** 主要與備援模型都在 20 秒內沒有任何正文片段
- **THEN** 事件串流以 `error` 結束，預扣被退回

#### Scenario: 超過生成上限
- **WHEN** 上游持續輸出超過 60 秒
- **THEN** 上游被關閉、事件串流以 `error` 結束

### Requirement: 已完成回合的重送
以已 `complete` 的 `client_msg_id` 重送 SHALL 回傳 `meta`（標示為重送）、一個含完整劇情原文的 `token`、`done`，不呼叫模型、不碰錢包、不新增回合。第 0 幕已 `ready` 或 `fallback` 時再呼叫開場端點 SHALL 以同樣的方式回傳既有文字，不重新生成。

#### Scenario: 重送
- **WHEN** 重送已完成的回合
- **THEN** 得到相同的劇情文字與 `done`，帳本與錢包沒有新增紀錄

### Requirement: 唯讀端點
系統 SHALL 提供 `GET /api/game/sessions/<id>`（存檔狀態、第 0 幕狀態與文字、世界狀態的玩家視圖、已完成輪數）與 `GET /api/game/sessions/<id>/turns?since_seq=`（依序號遞增的回合，含狀態與劇情，被擋與中斷的回合不含部分內容）。兩者 SHALL 以擁有者隔離，已完結的存檔仍可讀，回應 SHALL 禁止快取。世界狀態視圖 SHALL NOT 含內部編號、劇本複本全文與標記字串。

#### Scenario: 增量同步
- **WHEN** 以 `since_seq` 讀取回合
- **THEN** 只回傳序號較大的回合

#### Scenario: 已完結仍可讀
- **WHEN** 讀取已完結的存檔
- **THEN** 取得歷史與世界狀態

#### Scenario: 被擋回合
- **WHEN** 讀取含被擋回合的歷史
- **THEN** 該回合標示為被擋，沒有被擋的文字

### Requirement: 端點的快取與日誌
`/api/game/` 的所有回應 SHALL 禁止快取。日誌 SHALL 只記內部編號、列舉與整數（輪數、嘗試序號、字數、延遲、模型名稱、結束原因），SHALL NOT 含玩家行動、劇情原文、世界狀態、第 0 幕、被擋文字或標記字串。

#### Scenario: 日誌掃描
- **WHEN** 完成一個回合並收集期間的全部日誌
- **THEN** 日誌中找不到行動、劇情、NPC 名稱、第 0 幕或被擋文字
