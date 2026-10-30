# Spec Delta

## ADDED Requirements

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

## MODIFIED Requirements

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
