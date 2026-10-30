# Design

## Context

- 動機與範圍見 proposal.md。這裡只記技術現況與取捨。
- **現況**：`app/llm/` 有中立型別（`types.py`：`LLMProvider` 只有 `generate()`、四種中立錯誤）與 `gemini.py`。`LlmService`（`app/services/llm_service.py`）一次只拿一個供應商，帳本的 `provider` 取自 `provider.name`；它的流程（至多 2 次呼叫、暫時性錯誤切備援、被擋與設定錯誤不重試、斷路看帳本）與供應商無關，可原封重用。
- **設定**：`LlmSettings` 含金鑰、主要／備援／摘要模型與逾時、預算、輸出上限、思考預算（Gemini 專屬）。`GEMINI_API_KEY` 在 staging 與 production 是必要機密。模型名稱的格式由 `_MODEL_NAME`（`[A-Za-z0-9._-]{1,64}`）限制，OpenRouter 的識別碼含 `/` 與 `:`，所以會被擋下。
- **內容上限**：`AccessPolicy.content_ceiling()` 讀模組常數 `MAX_ADULT_SEXUAL_CONTENT_LEVEL = 2`。
- **規範**：AGENTS.md 第 7 節（2026-10-30 修訂）規定等級 3 只能以環境設定開啟、預設關閉、須記錄許可涵蓋的供應商與模型、第 10 節訂定強化成年驗證之前不得對一般會員開啟，以及絕對禁止項須由自有審查層執行。
- **RPG 尚未實作**，docs/RPG.md 規劃的是 SSE 串流與非串流的狀態變更 JSON 兩種呼叫；沒有任何程式會送出露骨內容。

## Goals / Non-Goals

**Goals:**
- 加入第二個供應商而不動 `LlmService` 的流程與客服、摘要的行為。
- 免費換付費只改環境變數。
- 等級 3 的開關有明確的啟動時防呆，使「誤開」與「模型不在許可範圍」在啟動時就失敗，而不是執行時才發現。
- 讓使用者在 RPG 出現之前就能評估端點好不好用（含 JSON 穩定度）。

**Non-Goals:**
- 串流、RPG 的回合引擎與存檔分級路由、成年驗證、自家審查層、RPG 與客服分開計算全站預算、更換客服使用的模型。
- 不驗證「許可」本身是否存在或有效（沒有任何程式能做到）；清單只是防止誤用的護欄。

## Decisions

**D1　轉接器放在 `app/llm/openai_compatible.py`，共用傳輸抽到 `app/llm/transport.py`。**
`HttpTransport` 與 `UrllibTransport` 目前在 `gemini.py`，第二個轉接器需要同一個東西。抽成獨立檔案，`gemini.py` 改為匯入，行為不變（既有的 Gemini 測試守住）。替代方案「在新轉接器複製一份」被否決：兩份 TLS 與逾時處理會漂移。替代方案「新轉接器匯入 `gemini.py`」被否決：轉接器之間不該互相依賴。維持標準函式庫、不引入 SDK（與既有決策一致）。

**D2　供應商以「用途」區分，在 `registry` 組裝，`LlmService` 不改。**
`app.extensions["llm_providers"]` 存 `{"cs": GeminiProvider, "rpg": OpenAICompatibleProvider}`，既有的 `llm_provider`（客服與摘要）保留，`create_app` 多一個注入參數 `rpg_llm_provider` 給測試。`get_llm_service()` 維持原狀；新增 `get_rpg_llm_service()`，以 RPG 用途的供應商與設定建立同一個 `LlmService`。因為帳本與斷路都以「模型名稱」為鍵，兩個供應商的模型名稱不同，彼此的斷路不會互相影響。替代方案「讓 `LlmService` 持有多個供應商並依參數選擇」被否決：它會讓客服的呼叫路徑多一個判斷，而客服現在沒有任何問題，不該被這個 change 碰到。

**D3　RPG 用途有自己的 `LlmSettings`，與客服共用逾時、預算與輸出上限。**
`AppConfig` 新增 `rpg_llm: OpenAICompatibleSettings | None`，內含一個 `LlmSettings`（金鑰、主要與備援模型）加上基底網址、零保存偏好、是否支援結構描述輸出。逾時、總預算、輸出上限沿用 `LLM_*`（一個地方調整）；思考預算是 Gemini 專屬，RPG 用途固定為空。**沒有設定備援模型時，備援等於主要模型**，既有流程對「主要與備援相同」的處理是暫時性錯誤直接降級（README 已記載）。設定要嘛完整要嘛完全沒有（spec）：以「金鑰與主要模型都在」判定已設定。`OPENROUTER_API_KEY` **不加入** `REQUIRED_SECRETS`：RPG 用途在 RPG 實作之前是選用的。基底網址與金鑰一起送出，所以在 `load_config` 驗證：只允許 `https`，development 與 testing 另允許回送位址的 `http`（本機模型），含帳號、密碼或查詢字串者一律拒絕；錯誤訊息只列變數名稱。

**D4　結構化輸出預設「不送供應商參數、改在系統指令附結構描述」。**
開放模型（含目前的免費模型）常不支援結構描述參數，送了會被拒（404 或 400），反而讓呼叫整個失敗。預設的退化做法是在系統指令末端附上「只輸出符合下列 JSON Schema 的 JSON」與結構描述的 JSON；`OPENROUTER_JSON_SCHEMA=true` 才送 `response_format` 的結構描述參數。輸出一律仍由 `parse_llm_json` 驗證，驗證失敗的重試與降級照既有流程。這對 RPG 的狀態變更 JSON 是關鍵風險，所以 `flask llm-probe` 提供結構化選項，先量穩定度。

**D5　資料保存偏好：送出偏好，沒有符合的端點就失敗，不靜默放寬。**
`OPENROUTER_ZDR=true` 時，每個請求（含備援）帶 `provider` 偏好（零保存與不收集資料）。沒有符合的端點時供應商拒絕，歸為設定錯誤（`LLMConfigError`）。**不重送不帶偏好的請求**：否則使用者以為資料不被保存，實際上被降級而無從得知。免費端點通常沒有符合零保存的端點，所以免費測試時不開它；換付費時開啟。請求欄位的確切名稱依 OpenRouter 文件，轉接器測試把目前的名稱釘住，真實呼叫的人工驗收再確認。

**D6　等級 3 的設定在啟動時驗證，`AccessPolicy` 只讀結果。**
`AppConfig` 新增 `content: ContentSettings(level3_enabled, level3_models)`。啟動驗證（`load_config`）：
1. 開關值必須是布林（沿用既有的布林解析方式）。
2. `staging` 與 `production` 開啟 → 啟動失敗（成年驗證尚未存在，之後的 change 放寬）。
3. 開啟時：RPG 用途必須已設定、`SEXUAL_CONTENT_LEVEL3_MODELS` 非空、RPG 的主要與備援模型都在清單內。
`AccessPolicy` 的建構子新增 `adult_ceiling: int = 2`（預設與現在完全相同，既有測試不受影響），`registry` 依設定傳入 2 或 3。`content_ceiling()` 邏輯只是：未成年或沒有生日 → 0，否則 `adult_ceiling`。年齡數字仍只來自 `AgePolicy`。常數 `MAX_ADULT_SEXUAL_CONTENT_LEVEL` 保留為預設值，另加 `LEVEL3 = 3`。模型清單不影響開關關閉時的任何行為。

**D7　`flask llm-probe` 放在 `app/cli.py`，用 `build_*` 函式，不經過請求。**
與既有的維運指令一致（沒有請求，明確傳入組裝好的東西）。只在 development 與 testing 執行；呼叫 RPG 用途的供應商一次，**不經過 `LlmService`、不寫帳本**（沒有使用者，也不該影響全站用量）。結構化選項用固定的小結構描述，經 `parse_llm_json` 驗證並回報通過與否。輸出是純文字印在標準輸出，不進結構化日誌（日誌規範禁止記錄模型輸出）。

**D8　`.env.example` 與 README 的環境變數表一起更新。**
新增八個名稱：`OPENROUTER_API_KEY`、`OPENROUTER_MODEL`、`OPENROUTER_FALLBACK_MODEL`、`OPENROUTER_BASE_URL`、`OPENROUTER_ZDR`、`OPENROUTER_JSON_SCHEMA`、`SEXUAL_CONTENT_LEVEL3_ENABLED`、`SEXUAL_CONTENT_LEVEL3_MODELS`。`tests/test_readme.py` 逐項比對 README 的表與 `.env.example`，兩處必須同步。預設基底網址放在轉接器模組（供應商網域只出現在 `app/llm/`，`config.py` 不含網域，符合「業務程式碼不含供應商網域」的架構測試）。

## Risks / Trade-offs

- **開放模型的 JSON 不穩（RPG 的狀態變更依賴它）** → 預設退化做法加上 `parse_llm_json` 的驗證與一次重試；`llm-probe --structured` 先量；真正的取捨留給 RPG 的 change。
- **免費模型隨時下架、限流（每分鐘 20 次、整個帳號共用）** → 404 歸為設定錯誤、429 走備援；`llm-probe` 讓問題在 RPG 之前就浮現。
- **免費端點的上游可能保存或訓練提示** → 測試只用無害的合成文字；露骨內容用付費模型加零保存（D5）；等級 3 的模型清單強迫使用者明確列出免費與付費兩個識別碼。
- **模型清單是自我申報，不是許可的證明** → 它只防止「換了模型卻忘了許可範圍」；許可的供應商與日期仍須由人記錄（見 Open Questions）。
- **開發環境開啟等級 3 時，沒有自家審查層** → 這個 change 沒有任何送出露骨內容的入口（RPG 未實作）；審查層是任何入口出現之前的前置條件，記入路線圖。
- **staging 與 production 無法開啟等級 3** → 這是刻意的（AGENTS.md）；成年驗證的 change 會放寬這個檢查。
- **抽出共用傳輸會動到 Gemini 轉接器** → 純搬移；既有的轉接器測試與整合測試全數通過才算完成。

## Migration Plan

沒有資料庫變更。部署：在 `.env`（或平台機密）加入 `OPENROUTER_*`；不設定即維持現狀。回復：移除這些變數與開關，應用程式回到只有 Gemini。啟動驗證只在新變數出現時才生效，所以舊的環境不受影響。

## Open Questions

- **許可涵蓋的供應商、模型與日期**：使用者尚未提供。取得後寫入 `docs/帳號與安全-auth-security.md` 並填入 `SEXUAL_CONTENT_LEVEL3_MODELS`；程式不依賴它們，所以不影響 specs 與任務。
- **OpenRouter 對內容審核的回應**：若人工驗收發現被審核擋下的輸入回 403 且帶有審核標示，應改歸為「輸入被阻擋」而非設定錯誤；屆時補一個 spec 情境，改動很小。
- **零保存偏好的確切欄位名稱**：轉接器依文件實作，人工驗收確認；欄位名稱變動只影響轉接器與其測試。
- **串流**：留給 RPG 的回合引擎（RC-2），那時再為 `LLMProvider` 加 `stream()`；轉接器的請求組裝會與非串流共用，不需要重寫。
- **RPG 與客服是否分開計算全站預算**：RPG 呼叫出現時才有意義，留給 RPG 的 change。
