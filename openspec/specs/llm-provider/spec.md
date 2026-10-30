# llm-provider Specification

## Purpose
定義系統如何呼叫大型語言模型：以供應商中立的介面隔離具體供應商，並規定模型名稱、逾時、金鑰處理與錯誤分類，使日後更換供應商只需要替換轉接器與設定，不必修改業務邏輯。

## Requirements

### Requirement: 模型呼叫經由供應商中立的介面
Service 與端點 SHALL 只透過中立的模型呼叫介面取得結果，SHALL NOT 匯入任何供應商的 SDK 或自行向供應商發出請求。供應商專屬的請求格式、安全設定與錯誤對應 SHALL 只存在於該供應商的轉接器內。

#### Scenario: 以假的供應商執行業務邏輯
- **WHEN** 測試以假的供應商取代真實轉接器
- **THEN** 客服的全部業務流程可以在沒有網路與金鑰的情況下完整執行

#### Scenario: 業務程式碼不含供應商相依
- **WHEN** 檢查 Service 與 Controller 的匯入
- **THEN** 其中沒有任何供應商的 SDK 或供應商網域

### Requirement: 供應商錯誤對應為中立類別
轉接器 SHALL 把供應商的失敗對應為中立類別：暫時性錯誤（速率限制、伺服器錯誤、逾時、連線失敗）、被安全過濾阻擋、設定錯誤（請求不合法或無權限）、其他。呼叫端只依類別決定重試、切換備援或降級。

#### Scenario: 速率限制
- **WHEN** 供應商回應速率限制
- **THEN** 呼叫端收到「暫時性錯誤」類別，且不含供應商的原始錯誤本文

#### Scenario: 被安全過濾阻擋
- **WHEN** 供應商因內容安全而不產生回覆
- **THEN** 呼叫端收到「被阻擋」類別，並知道是輸入被擋或輸出被擋

#### Scenario: 設定錯誤
- **WHEN** 供應商回應請求不合法或金鑰無權限
- **THEN** 呼叫端收到「設定錯誤」類別，並記錄為需要人工處理的錯誤

### Requirement: 模型名稱由環境變數決定
系統 SHALL 以環境變數指定主要模型、備援模型與摘要模型。主要模型預設為 `gemini-3.8-flash`，備援模型預設為 `gemini-3.5-flash-lite`，摘要模型未指定時使用主要模型。Gemini 的模型名稱 SHALL 只含英文字母、數字、點、底線與連字號，且長度不超過 64。OpenAI 相容端點（RPG 用途）的模型以 `OPENROUTER_MODEL` 與選用的 `OPENROUTER_FALLBACK_MODEL` 指定，沒有預設值；其名稱另允許斜線與冒號（供應商的識別碼含供應商與變體，例如 `vendor/model:free`），長度同樣不超過 64（帳本記錄模型名稱的欄位只有 64 字元，設定接受的名稱必須寫得進帳本）。

#### Scenario: 未設定模型
- **WHEN** 沒有設定任何模型環境變數
- **THEN** 主要模型與備援模型使用預設值，摘要模型與主要模型相同

#### Scenario: 依用途指定不同模型
- **WHEN** 摘要模型被設定為與主要模型不同的名稱
- **THEN** 摘要呼叫使用該模型，對話呼叫仍使用主要模型

#### Scenario: 模型名稱不合法
- **WHEN** 任一 Gemini 模型名稱含有斜線、空白或其他不允許的字元
- **THEN** 應用程式啟動失敗，訊息指出變數名稱而不含其值

#### Scenario: OpenAI 相容端點的模型識別碼
- **WHEN** `OPENROUTER_MODEL` 為 `vendor/model-name:free`
- **THEN** 名稱合法，應用程式可啟動

#### Scenario: OpenAI 相容端點的模型名稱不合法
- **WHEN** `OPENROUTER_MODEL` 含有空白、引號或超過 64 個字元
- **THEN** 應用程式啟動失敗，訊息指出變數名稱而不含其值

### Requirement: 呼叫的逾時由設定決定
系統 SHALL 規定單次模型呼叫逾時（預設 20 秒）與單次請求的總預算（預設 30 秒，含重試與切換備援）。單次逾時 SHALL 不大於總預算，總預算 SHALL 小於 55 秒，使伺服器自己的降級回覆先於前端與 worker 的逾時送達。

#### Scenario: 逾時設定互相矛盾
- **WHEN** 單次呼叫逾時大於總預算，或總預算不小於 55 秒
- **THEN** 應用程式啟動失敗，訊息指出是哪個變數不合法

#### Scenario: 預設值
- **WHEN** 沒有設定任何逾時環境變數
- **THEN** 單次呼叫逾時為 20 秒、總預算為 30 秒

### Requirement: 輸出長度上限由設定決定
單次呼叫的輸出 token 上限（含供應商計入的思考 token）SHALL 由環境變數指定，預設 4096，合法範圍為 256 至 32768；範圍外 SHALL 啟動失敗。輸出因長度被截斷時，結束原因 SHALL 回報為長度截斷，呼叫端以不完整的輸出處理。

#### Scenario: 預設值
- **WHEN** 沒有設定輸出上限
- **THEN** 送出的請求使用 4096

#### Scenario: 輸出被截斷
- **WHEN** 供應商因長度上限停止產生
- **THEN** 結束原因為長度截斷，不完整的 JSON 走驗證失敗的路徑

#### Scenario: 設定值不合法
- **WHEN** 輸出上限為 100 或不是整數
- **THEN** 應用程式啟動失敗，訊息指出變數名稱

### Requirement: 金鑰只由後端讀取且不外洩
模型金鑰 SHALL 只由後端自環境變數讀取：Gemini 為 `GEMINI_API_KEY`，OpenAI 相容端點為 `OPENROUTER_API_KEY`。金鑰 SHALL 放在請求標頭，SHALL NOT 出現在網址、日誌、錯誤訊息、HTTP 回應或設定物件的字串表示中；轉接器記錄的供應商錯誤 SHALL 不含金鑰。

#### Scenario: 送出請求
- **WHEN** 轉接器向供應商送出請求
- **THEN** 金鑰位於請求標頭，請求網址不含金鑰

#### Scenario: 供應商錯誤被記錄
- **WHEN** 供應商回應錯誤且錯誤內容被寫入日誌
- **THEN** 日誌中找不到金鑰

#### Scenario: 兩把金鑰互不混用
- **WHEN** 同時設定了 `GEMINI_API_KEY` 與 `OPENROUTER_API_KEY`
- **THEN** Gemini 的請求只帶前者，OpenAI 相容端點的請求只帶後者

### Requirement: 每次呼叫回報用量與結束原因
每次成功取得結果的呼叫 SHALL 回報實際使用的模型名稱、輸入與輸出的 token 數、思考 token 數（供應商有提供時）、延遲，以及正規化後的結束原因（正常結束、長度截斷、被安全過濾、輸入被阻擋、其他）。

#### Scenario: 備援模型回覆
- **WHEN** 呼叫以備援模型完成
- **THEN** 回報的模型名稱是備援模型，而不是主要模型

#### Scenario: 供應商未提供思考 token
- **WHEN** 供應商的用量資料沒有思考 token 欄位
- **THEN** 思考 token 數為空值，不是 0

### Requirement: 結構化輸出由供應商強制
需要結構化輸出的呼叫 SHALL 以非串流請求，並要求供應商依呼叫端提供的結構描述產生輸出。轉接器 SHALL NOT 假設輸出符合結構描述，驗證由呼叫端負責。

#### Scenario: 傳遞結構描述
- **WHEN** 呼叫端要求結構化輸出
- **THEN** 送出的請求包含輸出格式為 JSON 與該結構描述

### Requirement: 模型供應商不可用時回覆服務暫時無法使用
系統未設定模型供應商（例如本機開發沒有金鑰）時，所有需要呼叫模型的端點 SHALL 回應 503 與繁體中文訊息，且 SHALL NOT 寫入任何對話資料。應用程式本身仍可啟動。

#### Scenario: 開發環境沒有金鑰
- **WHEN** `APP_ENV` 為 `development` 且沒有設定金鑰
- **THEN** 應用程式可啟動，送出客服訊息得到 503，資料庫沒有新增訊息

#### Scenario: 測試環境不需要金鑰
- **WHEN** `APP_ENV` 為 `testing` 且沒有設定金鑰
- **THEN** 應用程式可建立，並可注入假的供應商

### Requirement: 依用途指定模型供應商
客服對話與對話摘要 SHALL 永遠使用 Gemini。RPG 敘事用途 SHALL 使用 OpenAI 相容端點，其供應商、金鑰、主要模型與備援模型的設定與其他用途互相獨立。未設定 RPG 用途時，只有該用途的功能回 503，其他用途 SHALL NOT 受影響。RPG 用途的設定 SHALL 要嘛完整、要嘛完全沒有：只有金鑰沒有模型，或只有模型沒有金鑰，SHALL 讓應用程式啟動失敗，訊息只列出變數名稱。

#### Scenario: 沒有設定 RPG 用途
- **WHEN** 只設定了 Gemini 金鑰，沒有任何 `OPENROUTER_*`
- **THEN** 客服與摘要照常運作，RPG 用途的功能回 503

#### Scenario: 設定不完整
- **WHEN** 設定了 `OPENROUTER_API_KEY` 但沒有 `OPENROUTER_MODEL`
- **THEN** 應用程式啟動失敗，訊息指出缺少 `OPENROUTER_MODEL`，不含金鑰值

#### Scenario: 客服不受影響
- **WHEN** RPG 用途的供應商持續回應錯誤
- **THEN** 客服與摘要的呼叫、備援與斷路都只依 Gemini 的狀況，不受影響

#### Scenario: 帳本記錄實際使用的供應商
- **WHEN** RPG 用途完成一次呼叫
- **THEN** 帳本記錄的供應商與模型是 OpenAI 相容端點與實際使用的模型，而不是 Gemini
