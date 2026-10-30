# Proposal

## Why

RPG 的敘事（尤其是成年角色之間合意的露骨內容，`sexual_content_level 3`）不適合交給 Gemini：供應商的內容安全過濾會擋下它，也沒有可調的出口。使用者決定為這個用途改接 OpenAI 相容的端點（目前是 OpenRouter 上的開放模型），客服與摘要則維持 Gemini，沒有任何問題。現在的程式只有 `GeminiProvider`、`LlmService` 一次只注入一個供應商、設定與機密都是 `GEMINI_*`，所以要先有「第二個供應商與依用途選擇供應商」的基礎。

目前階段是**個人測試**：先用免費端點確認好不好用，確定後才換付費版（只改環境變數）；對一般會員開放留到後期。AGENTS.md 第 7 節已於 2026-10-30 修訂為「等級 3 只能以環境設定開啟，預設關閉」，這個 change 把它落實到程式，而不是在程式中放寬寫死的常數。

## What Changes

- **新增 OpenAI 相容的轉接器**（`app/llm/`）：非串流的 `chat/completions` 呼叫，只用標準函式庫 `urllib`（與 Gemini 轉接器一致、不引入 SDK）；金鑰放請求標頭；失敗對應成既有的四種中立類別；可注入傳輸層，測試不連網。結構化輸出依模型能力退化（見 design）。
- **依用途指定供應商**：客服與摘要永遠走 Gemini；新增「RPG 敘事」用途，使用 OpenAI 相容端點。沒有設定 `OPENROUTER_*` 時，只有該用途不可用（503），客服完全不受影響。
- **新增環境變數**（沿用使用者已在 `.env` 使用的名稱）：`OPENROUTER_API_KEY`、`OPENROUTER_MODEL`，以及選用的備援模型、基底網址、資料保存偏好（零保存路由）、是否使用 JSON Schema 輸出。**免費換付費只改 `OPENROUTER_MODEL`（與選用的資料保存偏好）**，程式不動。
- **模型名稱的格式規則改為依供應商**：OpenAI 相容端點的模型識別碼含 `/` 與 `:`（例如 `cognitivecomputations/dolphin-mistral-24b-venice-edition:free`），既有規格只允許字母、數字、點、底線與連字號，所以要修訂（Gemini 的名稱規則不變）。
- **`sexual_content_level 3` 改為環境開關**：新增 `SEXUAL_CONTENT_LEVEL3_ENABLED`（預設關閉）與 `SEXUAL_CONTENT_LEVEL3_MODELS`（許可涵蓋的模型清單）。`AccessPolicy.content_ceiling()` 在「成年且開關開啟」時回傳 3，否則仍是 2；未成年者永遠是 0。development 與 testing 可開啟；**staging 與 production 在成年驗證完成之前，設定為開啟會啟動失敗**（AGENTS.md：第 10 節訂定強化驗證之前不得對一般會員開啟）。開啟時，RPG 用途的模型必須都在清單內，否則啟動失敗。
- **開發用的探測指令**：RPG 尚未實作，所以新增 `flask llm-probe`（只在 development 與 testing 可用），以固定的無害文字或使用者指定的短文字呼叫 RPG 用途的端點，並印出回覆、模型、延遲、token 與結束原因；加上選項時改測結構化輸出的穩定度。不寫入帳本與資料庫，供「先用免費端點確認好不好用」。
- **不做**：串流（`stream()`，待 RPG 的回合引擎）、RPG 本身與等級 3 的存檔路由、成年驗證、自家審查層（擋絕對禁止項）、客服與摘要的任何變動、後台的新功能。**這個 change 不產生任何露骨內容的入口**，因為 RPG 尚未實作。

## Capabilities

### New Capabilities
- `llm-openai-compatible`: OpenAI 相容端點的轉接器行為：請求與回應的對應、驗證標頭、錯誤分類、用量回報、結構化輸出的退化、資料保存偏好的傳遞、免費與付費只差設定。

### Modified Capabilities
- `llm-provider`: 模型名稱的合法字元依供應商而定（OpenAI 相容端點允許 `/` 與 `:`）；金鑰變數依供應商（`GEMINI_API_KEY`、`OPENROUTER_API_KEY`），金鑰一樣不得外洩；新增「依用途指定供應商」，沒有設定 RPG 用途的供應商時只有該用途回 503。
- `age-confirmation`: 成年者的內容上限在「等級 3 開關開啟」時為 3，其餘仍為 2，未成年者永遠是安全模式；新增等級 3 開關的設定規則（預設關閉、模型清單、staging 與 production 的限制）。

## Impact

- **新增程式**：`app/llm/openai_compatible.py`（轉接器）、`app/llm/transport.py`（把兩個轉接器共用的 HTTP 傳輸從 `gemini.py` 抽出，行為不變）、`flask llm-probe`（`app/cli.py`）。
- **修改程式**：`app/config.py`（`LlmSettings` 之外新增 RPG 用途的供應商設定與等級 3 開關、啟動驗證）、`app/__init__.py`（建立第二個供應商並以用途存放）、`app/runtime.py`、`app/services/registry.py`（依用途取得供應商）、`app/services/access_policy.py`（`content_ceiling` 讀開關，取代寫死的常數）、`.env.example`。
- **文件與測試**：README 環境變數表（測試逐項比對 `.env.example`）、`docs/模型呼叫層-llm-layer.md`、`docs/帳號與安全-auth-security.md`、`docs/營運與部署-operations.md`、`docs/路線圖-roadmap.md`；假 HTTP 傳輸的轉接器測試、設定驗證測試、`content_ceiling` 的組合測試。
- **資料庫**：無變更（沒有新資料表或欄位）。`llm_call_logs.provider` 已是字串，帳本會記錄實際使用的供應商與模型。
- **相依套件**：不新增。
- **既有行為**：客服、摘要、用量限制、後台都不變；`content_ceiling` 在預設設定下與現在完全相同（成年者上限 2）。
- **外部**：真實呼叫只在人工驗收進行，使用無害的合成文字、免費端點，且每次都需要使用者當下同意；金鑰只存在使用者本機的 `.env`。
- **規範**：AGENTS.md 第 7 節已修訂並同步 skill 鏡像（本 change 不再修改規範）；第 10 節的強化成年驗證做法尚未訂定，是對外開放前的另一個 change。
