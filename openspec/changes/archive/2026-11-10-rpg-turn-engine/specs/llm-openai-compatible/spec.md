# Spec Delta

## MODIFIED Requirements

### Requirement: 以非串流的對話補全請求呼叫
結構化與一般呼叫（`generate`）SHALL 向設定的基底網址的 `/chat/completions` 送出一次非串流的 JSON POST；串流呼叫見「串流請求」。系統指令 SHALL 對應為 `system` 訊息；內容區塊的 `user` 與 `model` 角色 SHALL 對應為 `user` 與 `assistant`；輸出 token 上限與逾時 SHALL 來自呼叫端。基底網址預設為 OpenRouter 的官方網址，可由環境變數改為其他 OpenAI 相容端點（含本機模型）。

#### Scenario: 請求的內容
- **WHEN** 呼叫端要求一次含系統指令與兩個內容區塊的呼叫
- **THEN** 請求本文含一則 `system` 訊息，其後依序為 `user` 與 `assistant` 訊息，並含呼叫端指定的模型與輸出上限

#### Scenario: 改用其他端點
- **WHEN** 基底網址被設定為本機的 OpenAI 相容服務
- **THEN** 請求送往該網址的 `/chat/completions`，其餘格式相同

#### Scenario: 非串流
- **WHEN** 以 `generate` 呼叫
- **THEN** 請求的 `stream` 為假，回應為單一 JSON

## ADDED Requirements

### Requirement: 串流請求
串流呼叫 SHALL 送出 `stream` 為真並帶 `stream_options.include_usage` 的請求，逐行讀取 `data:` 事件，將文字增量對應為中立的文字事件、將最後的用量與結束原因對應為結束事件，並在短時間（約 1 秒）沒有位元組時產生閒置 tick，使外層能檢查期限與送出心跳。上游的讀取 SHALL 能被呼叫端中途關閉（客戶端斷線或審查命中時），關閉後 SHALL NOT 再讀取。供應商以註解行（以 `:` 開頭）送出的保活 SHALL 被忽略。失敗的對應 SHALL 與非串流相同，金鑰與供應商回應本文的處理亦同。

#### Scenario: 增量與用量
- **WHEN** 供應商送出兩個文字增量與最後帶用量的事件
- **THEN** 呼叫端收到兩個文字事件與一個含用量與結束原因的結束事件

#### Scenario: 保活註解
- **WHEN** 供應商送出以 `:` 開頭的保活行
- **THEN** 被忽略，不產生文字事件

#### Scenario: 中途關閉
- **WHEN** 呼叫端在串流中關閉
- **THEN** 上游連線被關閉，不再讀取

#### Scenario: 串流開始前的錯誤
- **WHEN** 供應商對串流請求回應 429
- **THEN** 呼叫端收到暫時性錯誤，與非串流的對應相同

### Requirement: 推理由設定關閉
環境變數 `OPENROUTER_REASONING` SHALL 控制推理，預設為關閉。關閉時每個請求（串流與非串流、主要與備援模型）SHALL 帶 `reasoning: {"enabled": false}`；開啟時 SHALL 不帶此參數，由供應商與模型的預設行為決定。推理的 token 數若有提供 SHALL 回報為思考 token。

#### Scenario: 預設
- **WHEN** 沒有設定 `OPENROUTER_REASONING`
- **THEN** 請求帶有關閉推理的參數

#### Scenario: 開啟
- **WHEN** `OPENROUTER_REASONING` 設為開啟
- **THEN** 請求不帶推理參數

### Requirement: 結束原因為長度且正文為空視為失敗
回應（串流或非串流）的結束原因為長度截斷而正文為空時，轉接器 SHALL 視為暫時性失敗（不得當作成功的空回覆），使呼叫端可切換備援模型；失敗只帶分類，不含供應商本文。

#### Scenario: 思考燒光輸出上限
- **WHEN** 模型因輸出上限停止，且沒有任何正文
- **THEN** 呼叫端收到暫時性錯誤，不是空字串的成功結果

#### Scenario: 有正文的截斷
- **WHEN** 模型因輸出上限停止，但已有正文
- **THEN** 回傳正文，結束原因為 `length`

### Requirement: 探測指令的輸出不因主控台編碼而中斷
`flask llm-probe` 印出模型回覆時 SHALL NOT 因主控台編碼無法表示某個字元（例如繁體中文 Windows 的 cp950 遇到符號）而中斷；無法編碼的字元 SHALL 以替代字元顯示，指令仍印出模型、延遲、token 數與結束原因並以成功狀態結束。指令 SHALL 同時顯示目前的推理設定。

#### Scenario: 無法編碼的字元
- **WHEN** 模型回覆含主控台無法編碼的字元
- **THEN** 指令完成輸出，不拋出編碼錯誤
