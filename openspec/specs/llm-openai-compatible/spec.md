# llm-openai-compatible Specification

## Purpose
定義 OpenAI 相容端點（目前是 OpenRouter）作為模型供應商時的呼叫行為：請求與回應如何對應到中立的介面、驗證與機密如何處理、失敗如何分類、結構化輸出如何退化，以及從免費模型換成付費模型只需要改設定。

## Requirements

### Requirement: 以非串流的對話補全請求呼叫
轉接器 SHALL 向設定的基底網址的 `/chat/completions` 送出一次非串流的 JSON POST。系統指令 SHALL 對應為 `system` 訊息；內容區塊的 `user` 與 `model` 角色 SHALL 對應為 `user` 與 `assistant`；輸出 token 上限與逾時 SHALL 來自呼叫端。基底網址預設為 OpenRouter 的官方網址，可由環境變數改為其他 OpenAI 相容端點（含本機模型）。

#### Scenario: 請求的內容
- **WHEN** 呼叫端要求一次含系統指令與兩個內容區塊的呼叫
- **THEN** 請求本文含一則 `system` 訊息，其後依序為 `user` 與 `assistant` 訊息，並含呼叫端指定的模型與輸出上限

#### Scenario: 改用其他端點
- **WHEN** 基底網址被設定為本機的 OpenAI 相容服務
- **THEN** 請求送往該網址的 `/chat/completions`，其餘格式相同

### Requirement: 金鑰只放在請求標頭且不外洩
金鑰 SHALL 以 `Authorization: Bearer` 標頭送出，SHALL NOT 出現在網址、日誌、錯誤訊息、HTTP 回應、帳本，或轉接器與設定物件的字串表示中。供應商回應的本文 SHALL NOT 被保留或記錄，失敗只回報分類、狀態碼與延遲。

#### Scenario: 轉接器的字串表示
- **WHEN** 轉接器或設定物件被轉成字串
- **THEN** 金鑰被遮罩

#### Scenario: 供應商回應錯誤
- **WHEN** 供應商回應含有請求內容回聲的錯誤本文
- **THEN** 呼叫端收到的失敗只含分類與狀態碼，日誌與帳本中找不到該本文

### Requirement: 失敗對應為中立類別
轉接器 SHALL 把 429、408、5xx、逾時與連線失敗對應為暫時性錯誤；把 400、401、402（餘額不足）、403、404（模型不存在或沒有可用端點）對應為設定錯誤；把結束原因為內容過濾的結果對應為「輸出被阻擋」。回應本文含 `error` 物件而沒有結果時 SHALL 依其代碼歸類，無法判斷時視為暫時性錯誤。

#### Scenario: 速率限制
- **WHEN** 供應商回應 429
- **THEN** 呼叫端收到暫時性錯誤，可依既有規則切換備援模型

#### Scenario: 餘額不足
- **WHEN** 供應商回應 402
- **THEN** 呼叫端收到設定錯誤，不重試也不切換備援

#### Scenario: 內容過濾
- **WHEN** 供應商以內容過濾結束這次生成
- **THEN** 呼叫端收到「輸出被阻擋」，不重試也不切換備援

#### Scenario: 模型沒有可用端點
- **WHEN** 供應商回應 404（模型已下架或沒有可用的端點）
- **THEN** 呼叫端收到設定錯誤，需要人工處理

### Requirement: 回報用量與正規化的結束原因
轉接器 SHALL 回報實際使用的模型名稱、輸入與輸出的 token 數，並把結束原因正規化為既有的集合：`stop`、`length`、`safety`、`other`。供應商沒有提供思考 token 時 SHALL 為空值，不是 0。

#### Scenario: 長度截斷
- **WHEN** 供應商因長度上限停止產生
- **THEN** 結束原因為 `length`

#### Scenario: 沒有用量資料
- **WHEN** 供應商的回應沒有用量欄位
- **THEN** token 數為空值，呼叫不因此失敗

### Requirement: 結構化輸出依模型能力退化
預設情況下，轉接器 SHALL NOT 送出供應商的結構化輸出參數（許多開放模型不支援，會被拒絕），而是在系統指令末端附上輸出結構描述與「只輸出符合它的 JSON」的要求。環境變數指定該模型支援結構描述輸出時，才送出結構描述參數。無論哪種方式，輸出的驗證 SHALL 仍由呼叫端負責。

#### Scenario: 預設
- **WHEN** 呼叫端要求結構化輸出且沒有設定模型支援結構描述
- **THEN** 請求不含結構化輸出參數，系統指令含結構描述與只輸出 JSON 的要求

#### Scenario: 模型支援
- **WHEN** 設定指出該模型支援結構描述輸出
- **THEN** 請求含結構描述參數

### Requirement: 資料保存偏好隨請求送出且不被靜默放寬
設定要求零保存時，轉接器 SHALL 在每個請求（含備援模型）帶上「只路由到不保存提示的端點」的偏好。沒有符合的端點時供應商會拒絕，該次呼叫 SHALL 歸為設定錯誤，SHALL NOT 在不帶偏好的情況下重送。

#### Scenario: 要求零保存
- **WHEN** 設定要求零保存並送出請求
- **THEN** 請求帶有零保存的路由偏好

#### Scenario: 沒有符合的端點
- **WHEN** 要求零保存，但供應商回應沒有可用端點
- **THEN** 呼叫端收到設定錯誤，沒有任何不帶偏好的重送

### Requirement: 免費與付費模型只差設定
轉接器與呼叫流程 SHALL NOT 依模型名稱是否帶有免費標記而有不同行為。從免費模型換成付費模型 SHALL 只需要改模型名稱（以及選用的資料保存偏好）的環境變數，不需要修改程式或重新建立資料。

#### Scenario: 換模型
- **WHEN** 模型名稱由帶免費標記的識別碼改為不帶的識別碼
- **THEN** 請求格式與錯誤處理完全相同，只有請求中的模型名稱改變

### Requirement: 開發用的探測指令只限開發環境且不留下紀錄
`flask llm-probe` SHALL 只在 `development` 與 `testing` 執行，`staging` 與 `production` SHALL 拒絕並以非零狀態結束。指令 SHALL 呼叫 RPG 用途的端點一次，印出回覆文字、實際使用的模型、延遲、token 數與結束原因；加上結構化選項時，SHALL 改以固定的結構描述呼叫並回報輸出是否通過驗證。指令 SHALL NOT 寫入帳本或任何資料表，SHALL NOT 在輸出或日誌中顯示金鑰。

#### Scenario: 開發環境
- **WHEN** 在 `development` 執行指令且 RPG 用途已設定
- **THEN** 指令印出回覆、模型、延遲、token 數與結束原因，資料庫沒有新增任何資料列

#### Scenario: 預備與正式環境
- **WHEN** 在 `staging` 或 `production` 執行指令
- **THEN** 指令拒絕執行並以非零狀態結束，沒有任何請求送出

#### Scenario: 沒有設定 RPG 用途
- **WHEN** 沒有設定 `OPENROUTER_*` 時執行指令
- **THEN** 指令說明 RPG 用途尚未設定並以非零狀態結束

#### Scenario: 結構化穩定度
- **WHEN** 以結構化選項執行，且模型回覆的不是合法 JSON
- **THEN** 指令回報驗證失敗與原因分類，不印出供應商的錯誤本文

#### Scenario: 金鑰
- **WHEN** 指令執行時任何輸出
- **THEN** 輸出中找不到金鑰

### Requirement: 基底網址必須安全
基底網址 SHALL 使用 `https`。只有 `development` 與 `testing` 可使用 `http`，且主機只能是回送位址（`localhost`、`127.0.0.1`、`::1`），讓本機模型可用而不讓金鑰以明文送出。網址 SHALL NOT 含帳號、密碼或查詢字串。不符者 SHALL 使應用程式啟動失敗，訊息只指出變數名稱而不含其值。

#### Scenario: 正式環境使用 http
- **WHEN** `APP_ENV` 為 `production` 且基底網址以 `http://` 開頭
- **THEN** 應用程式啟動失敗，訊息指出 `OPENROUTER_BASE_URL`

#### Scenario: 開發環境的本機模型
- **WHEN** `APP_ENV` 為 `development` 且基底網址為 `http://localhost:11434/v1`
- **THEN** 應用程式可啟動

#### Scenario: 開發環境使用遠端 http
- **WHEN** `APP_ENV` 為 `development` 且基底網址為 `http://example.com/v1`
- **THEN** 應用程式啟動失敗，訊息指出 `OPENROUTER_BASE_URL`

#### Scenario: 網址夾帶憑證
- **WHEN** 基底網址含有 `user:pass@` 或查詢字串
- **THEN** 應用程式啟動失敗，訊息不含該網址的內容
