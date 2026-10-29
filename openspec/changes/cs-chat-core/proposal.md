# Proposal

## Why

平台的第一個核心功能是會員制智能客服，但目前只有帳號與權限這一層：沒有任何對話資料、沒有呼叫模型的程式、沒有 `/cs` 頁面（`/api/cs/*` 與 `llm_call_logs` 也還不存在）。共用層（C-1 地基、C-2 會員驗證、年齡閘門）已經就緒，`AccessPolicy.require_chat_access` 正等著第一個會呼叫 Gemini 的端點；使用者的 `GEMINI_API_KEY` 也已放進本機 `.env`。C-3（`cs-chat-core`）把這條路打通：一位已登入且通過年齡確認的會員，可以在 `/cs` 與 AI 客服對話、回看歷史、重送不會重複花費模型呼叫、模型失敗時看到降級文案。

這個 change 同時決定 RPG 之後會共用的那一層：**模型呼叫端做成介面加轉接器**（使用者計畫日後可能換供應商）、**單一的 JSON 解析與驗證函式**、**`llm_call_logs` 用量帳本**、**滑動視窗與補償式摘要**。這些做對了，RPG 只需要增加串流。

## What Changes

- **LLM 呼叫端**：新增供應商中立的 `LLMProvider` 介面與 Gemini 轉接器（標準函式庫 `urllib`，不引入 SDK）。模型名稱、逾時、備援模型都來自環境變數；金鑰只由後端讀取，放在請求標頭，不進網址、日誌或回應。
- **單次呼叫契約**：新增 Service 層唯一的 `parse_llm_json`（去 code fence、JSON 解析、schema 驗證、語意驗證）與呼叫編排——驗證失敗以同一模型重試一次；429、5xx、逾時改走備援模型（算作該次重試）；安全過濾與設定錯誤不重試不切換；總共最多 2 次呼叫、單次請求總預算 30 秒；失敗降級為繁體中文文案、標記 `fallback` 且不計額度。主要模型掛住或 5xx 後，由 `llm_call_logs` 推得 60 秒的斷路狀態（不用行程內記憶體）。
- **資料**：新增 `chat_conversations`、`chat_messages`、`llm_call_logs` 三張表（migration `0005`，只新增、不動既有資料表）。
- **客服的意圖與實體**：單一來源的意圖列舉與固定結構的 entities，同一份常數產生 Gemini 的 response schema、驗證與 system prompt；system prompt 只含指令，使用者與歷史文字一律放在資料區塊；內含不公開標記以偵測洩漏；`reply_text` 一律純文字。
- **對話 API**（`/api/cs/*`）：建立、列出、改名、刪除對話；送出訊息（冪等：同一 `client_msg_id` 不重複呼叫模型）；以 `since_seq` 增量拉取。後端指派單調遞增的 `seq`；同一對話同時只處理一則；非擁有者一律 404。
- **記憶**：送給模型的上下文固定為「摘要 + 最近 10 輪原文 + 當前狀態」；`state_json`（`member_tier`、`last_intent`、`pending_entities`、`open_issue`）帶 `state_version`；超過 10 輪時以補償式摘要折疊最舊的一輪。
- **`/cs` 頁面**：可開關的 280px 左側 Sidebar（桌面推擠、手機覆蓋）、打字機渲染、`suggested_actions` 快速回覆 chips、IndexedDB 暫存與背景同步（待送訊息以 `client_msg_id` 重送、以 `since_seq` 補齊）、axios 逾時（由伺服器的總預算加 5 秒推得，預設 35 秒）搭配 `AbortController`。逾時三個數值的最終值待模型耗時量測後由使用者決定。
- **設定與部署**：`GEMINI_API_KEY` 成為 staging 與 production 的必要機密（缺漏即啟動失敗）；新增模型名稱、逾時與斷路視窗的環境變數與 `.env.example`；容器驗收腳本同步更新。
- **不在範圍（留給後續 change）**：每位使用者的速率與每日輪數、對話數上限、全站 token 預算、同一使用者一日被安全過濾阻擋 3 次即暫停 1 小時（C-4 `usage-safety-ops`）；管理員後台、保留期限清理、對話匯出與刪除帳號（C-4）；訂單查詢與退款（C-6）；RPG 的串流與劇情（RPG 系列）。

## Capabilities

### New Capabilities
- `llm-provider`: 供應商中立的模型呼叫介面、Gemini 轉接器、模型與逾時設定、錯誤對應、金鑰處理。
- `llm-structured-calls`: 結構化輸出的單次呼叫契約——`parse_llm_json`、重試與備援與斷路規則、降級、`llm_call_logs` 用量帳本。
- `cs-nlp-contract`: 客服的意圖與實體、輸出 schema、system prompt 的規則與防護、`suggested_actions`。
- `cs-conversations`: 對話與訊息的資料、`/api/cs/*`、擁有者隔離、冪等與排序與增量同步、單一處理、輸入限制、刪除。
- `cs-conversation-memory`: 滑動視窗上下文、客服當前狀態、補償式摘要。
- `cs-chat-ui`: `/cs` 頁面、Sidebar、打字機與 chips、IndexedDB 暫存與同步、前端逾時與中止。

### Modified Capabilities
- `app-configuration`: `GEMINI_API_KEY` 加入預備與正式環境的必要機密；新增 LLM 設定（模型名稱、逾時、斷路視窗）的驗證。
- `application-layering`: Service 的業務錯誤新增「服務暫時無法使用」（503），對應模型供應商整體不可用或金鑰未設定。
- `access-control`: 客服對話只有擁有者可存取；所有會呼叫模型的端點先過 `require_chat_access`。
- `service-observability`: 新增模型呼叫的結構化記錄（只含模型、嘗試次數、token、結束原因、錯誤類型等白名單欄位）。

## Impact

- **新增程式**：`app/llm/`（介面、Gemini 轉接器）、`app/services/` 的 `llm_service.py`、`llm_json.py`、`cs_intents.py`、`cs_prompt.py`、`chat_service.py`、`context_builder.py`、`summary_service.py`；`app/models/` 三個模型；`app/repositories/` 三個 Repository；`app/controllers/cs_api_controller.py`、`cs_page_controller.py`；`app/templates/cs/`；`app/static/js/cs/`、`css/cs.css`；migration `0005`。
- **修改程式**：`app/config.py`、`app/__init__.py`、`app/runtime.py`、`app/services/registry.py`、`app/services/errors.py`、`app/controllers/errors.py`、`app/services/input_cleaning.py`、`.env.example`、`scripts/verify_container.py`、README、`docs/spec_report_v2.2.0.md`。
- **相依套件**：不新增（HTTP 用標準函式庫，與 Google 登入一致）。
- **既有行為**：staging 與 production 之後必須有 `GEMINI_API_KEY` 才能啟動；本機開發與測試環境沒有金鑰時，客服端點回 503 而不是整個應用程式無法啟動。
- **外部**：開始實作前須重新探測主要模型的可用性與金鑰額度（RPG.md T-3、T-6：`gemini-3.8-flash` 近期多次回 503，免費層每分鐘只有 5 次）；真實呼叫只用不含個資的合成文字，且需要使用者在當下同意（會消耗額度）。
- **上線限制**：C-3 完成後、C-4 完成前，**不得**以真實金鑰對公開網路開放（沒有額度保護）。目前的年齡設定與 Gemini 條款不符，僅限個人測試（見 `docs/RPG.md` R-15.4）。
