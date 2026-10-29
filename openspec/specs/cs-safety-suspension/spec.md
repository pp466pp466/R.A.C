# cs-safety-suspension Specification

## Purpose
定義同一位使用者在一天內反覆被內容安全過濾阻擋時的處置：暫停客服對話一段時間並留下記錄，使惡意或誤用不會持續消耗模型呼叫，而不需要自建關鍵字過濾。

## Requirements

### Requirement: 一日被阻擋三次即暫停一小時
同一位使用者在 Asia/Taipei 的同一日內，客服對話呼叫被內容安全過濾阻擋（輸入或輸出被擋）達 3 次時，系統 SHALL 暫停其客服對話 1 小時，自最近一次被擋的時間起算。暫停期間送出訊息 SHALL 回應 403 與代碼 `chat_suspended`，說明暫停的原因與恢復的時間（台灣時間），並 SHALL NOT 儲存訊息或呼叫模型。

#### Scenario: 第三次被擋
- **WHEN** 使用者今日第 3 次因安全過濾被擋
- **THEN** 該則照常得到既有的被擋文案；之後 1 小時內再送出訊息回應 403、代碼 `chat_suspended`

#### Scenario: 少於三次
- **WHEN** 使用者今日只被擋 2 次
- **THEN** 之後送出的訊息照常處理

#### Scenario: 暫停期滿
- **WHEN** 最近一次被擋已超過 1 小時
- **THEN** 使用者可以再送出訊息

#### Scenario: 期滿後再被擋
- **WHEN** 暫停期滿後使用者再次被擋，今日累計已達 3 次以上
- **THEN** 再次暫停 1 小時

#### Scenario: 隔日重新計算
- **WHEN** 台灣時間進入新的一天
- **THEN** 前一日的阻擋次數不再計入

### Requirement: 暫停狀態由帳本推得
是否暫停及何時恢復 SHALL 由 `llm_call_logs` 中該使用者當日的對話呼叫阻擋紀錄推得，SHALL NOT 依賴行程內記憶體；摘要呼叫的阻擋 SHALL NOT 計入。多個 worker 因此看到同一個結果，重啟也不會讓暫停失效。

#### Scenario: 換 worker
- **WHEN** 暫停發生後，下一個請求由另一個 worker 處理
- **THEN** 該請求同樣被拒絕為 `chat_suspended`

#### Scenario: 摘要被擋不計入
- **WHEN** 使用者今日有 2 次對話呼叫被擋與 1 次摘要呼叫被擋
- **THEN** 今日阻擋次數為 2，沒有暫停

### Requirement: 暫停時記錄事件
暫停生效時，系統 SHALL 記錄一筆事件，包含使用者的內部編號與原因代碼，SHALL NOT 含訊息文字、被擋的類別以外的模型內容或使用者的 email，供管理員之後在後台查看。

#### Scenario: 事件內容
- **WHEN** 使用者因第三次被擋而被暫停
- **THEN** 日誌出現暫停事件，欄位只有白名單內的內部編號與原因代碼

### Requirement: 暫停只影響客服對話
暫停 SHALL 只拒絕會呼叫模型的送出訊息；使用者 SHALL 仍可登入、閱讀與刪除自己的歷史對話、查看用量與管理帳號。`GET /api/cs/usage` SHALL 在暫停期間回報截止時間。

#### Scenario: 暫停期間閱讀
- **WHEN** 被暫停的使用者列出對話與讀取訊息
- **THEN** 照常回應

#### Scenario: 用量顯示暫停
- **WHEN** 被暫停的使用者查詢用量
- **THEN** 回應包含暫停的截止時間
