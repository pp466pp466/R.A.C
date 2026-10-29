# Tasks

## 1. 前置：模型可用性與設定

- [x] 1.1 **（需使用者當下同意，會消耗額度與流量）** 重新探測主要與備援模型：各做 5 次極短的合成文字呼叫，另各做 3 次「要求約 600 字繁體中文回覆」的合成呼叫以量測長回覆的耗時、輸出與思考 token（皆不含任何個資、不輸出金鑰），記錄成功率、耗時、狀態碼，並確認金鑰所屬方案與每分鐘額度；結果寫回 [RPG.md](../../../docs/RPG.md) 的 T-3、T-6、T-9；**依 design.md「Open Questions」的決策規則，把長回覆耗時的分布與建議的逾時數值回報使用者，由使用者決定三個逾時的最終值**（需要修訂 AGENTS.md 時由使用者核可後才寫入，並同步 skill 鏡像）。驗證：RPG.md 有帶日期的新紀錄；`git grep` 在版本庫中找不到 `.env` 內的金鑰值；若兩個模型都不可用，回報使用者並討論後再繼續，不自行改設計。
- [x] 1.2 `app/config.py` 新增 `LlmSettings`（金鑰包在 `Secret`、三個模型名稱、單次逾時、總預算、斷路視窗、輸出 token 上限、選用的思考預算），`REQUIRED_SECRETS` 加入 `GEMINI_API_KEY`（只對 staging 與 production），驗證模型名稱字元與長度、逾時不矛盾且總預算小於 55 秒；更新 `.env.example`（只有名稱與說明，沒有值）與既有測試的 staging 環境（`tests/support/config_env.py`）。驗證：`tests/test_config*.py` 新增並通過——預設值（含輸出上限 4096）、各種不合法值（含輸出上限 100）各自啟動失敗且訊息只含變數名稱、staging 缺金鑰失敗、development 缺金鑰可啟動、`AppConfig` 的字串表示不含金鑰。

## 2. 資料層

- [x] 2.1 新增 `ChatConversation`、`ChatMessage`、`LlmCallLog` 模型與 migration `0005_chat_core`（revision id ≤ 32 字元；只新增；不含原生 SQL；`llm_call_logs.conversation_id` 不設外鍵，`user_id` 為 SET NULL；兩張對話表的外鍵為 CASCADE），並在 [spec_report_v2.2.0.md](../../../docs/spec_report_v2.2.0.md) 補三張表的欄位與約束。驗證：`tests/test_migrations.py` 在 SQLite 與 SQL Server `project_test` 驗證升級與降級（降級只刪這三張表）、三組唯一約束、串聯刪除（刪使用者→對話與訊息消失、帳本的 `user_id` 變空值）、`tests/architecture/test_user_foreign_keys.py` 與 `test_no_raw_sql.py` 通過。
- [x] 2.2 新增 `ChatRepository`：每個方法第一個參數為 `user_id`——依對外識別碼取得、以 `client_id` 取得或建立（冪等）、列出（`updated_at` 由新到舊、`limit` 與 `offset`）、附加訊息（`MAX(seq)+1`，撞唯一約束時拋出可辨識的衝突）、增量查詢（`seq` 大於 N，附 `has_more`）、取最近的正常輪、改名與寫狀態與寫摘要（皆為條件式 UPDATE）、取得與清除處理標記（以我設定的時間值為條件）、刪除（同一交易清除該使用者帳本中的對話編號）。驗證：Repository 測試（SQLite 與 SQL Server）——跨使用者隔離、各條件式更新在條件不符時回傳未寫入、過期標記可接手而未過期不行、清除標記不會清掉接手者的標記、刪除後訊息消失且帳本參照清除、分頁順序連續不重複。
- [x] 2.3 新增 `LlmCallLogRepository`：附加帳本紀錄、斷路查詢（主要模型、逾時或伺服器錯誤、近 N 秒內是否存在）；`error_detail` 寫入時截斷至 2000 字。驗證：Repository 測試——截斷邊界（2000 與 2001 字）、斷路窗口的邊界（59 與 61 秒）、速率限制不觸發、不同模型互不影響；`tests/architecture` 新增 AST 測試，要求 `ChatRepository` 的所有公開方法都有 `user_id` 參數。

## 3. 模型呼叫層

- [x] 3.1 新增 `app/llm/types.py`（`LLMRequest`、`LLMResponse`、`LLMTransientError`、`LLMBlockedError`、`LLMConfigError`、`LLMValidationError`）與供應商介面；`tests/architecture/test_layering.py` 新增 `llm` 層的禁止匯入；新增 `tests/support/llm.py` 的 `FakeLLMProvider`（腳本化回應與錯誤序列、記錄請求、可設定消耗的時間）。驗證：新增的架構測試證明 Service 與 Controller 的原始碼不含供應商網域、SDK 名稱與金鑰標頭名稱，且 `app/llm` 不匯入 services、repositories、controllers、models、flask；假供應商的行為有自己的單元測試。
- [x] 3.2 實作 `GeminiProvider`（`urllib`、可注入傳輸）：請求內容、金鑰只在標頭、結構描述轉換只送出受支援的鍵、用量與結束原因的解析（思考 token 缺漏為空值）、錯誤對應（429、5xx、逾時、連線失敗、400 與 403、輸入被擋、輸出被擋、200 但沒有文字）。驗證：以假傳輸的單元測試覆蓋上述每一項；故意讓錯誤本文含金鑰字串，斷言例外、日誌與回傳值都找不到它。
- [x] 3.3 實作 `parse_llm_json` 與中立 schema 驗證（型別、必填、列舉、陣列長度、字串長度、拒絕多餘欄位）。驗證：單元測試——程式碼區塊、前後夾雜文字、不是 JSON、各種 schema 違規、語意驗證失敗各回報正確的 `reason` 分類；`reason` 與例外訊息不含模型文字。
- [x] 3.4 實作 `LlmService.run_structured`：依設計 D4 的流程做重試、備援切換、斷路、單調時鐘的預算，每次呼叫寫一筆帳本（含 `counted`、失敗原文截斷）。驗證：單元與整合測試涵蓋——一次成功、驗證失敗後重試成功與失敗、429 與 5xx 與逾時各自切備援、備援也失敗、備援輸出不合法不再重試、被阻擋與設定錯誤不重試不切換、剩餘預算不足 2 秒直接降級、斷路開啟與 59 秒與 61 秒、斷路只看資料庫（以另一個應用程式實例共用同一資料庫驗證）、`counted` 的三種情形、帳本不含任何對話文字。
- [x] 3.5 新增 `ServiceUnavailable`（503，代碼 `service_unavailable`）與 `controllers/errors.py` 的對應；`create_app` 新增 `llm_provider` 注入參數（沒有注入且有金鑰時建立 `GeminiProvider`，兩者皆無則為空）；`runtime.get_llm_provider()` 與 `registry` 的組裝。`get_llm_service()` 沒有供應商時回傳空值（讀取對話等不需要模型的操作不受影響，只有送出訊息這類需要模型的操作才回 503，由 5.2 的 `ChatService` 負責）。驗證：測試——無金鑰也無注入時應用程式可啟動且供應商為空；有金鑰時建立轉接器且建立過程不連網、`repr` 不含金鑰；注入的供應商優先於金鑰；服務取得設定的模型與限制；`tests/test_service_errors.py` 涵蓋 503 的對應與「每個業務錯誤型別都有明確的狀態碼」。
- [x] 3.6 模型呼叫的結構化日誌（白名單欄位）、設定錯誤的錯誤層級記錄、主要模型異常（逾時或伺服器錯誤）而將被跳過時，在記錄該次失敗的當下以警告層級記錄一次（窗口內被跳過的請求不重複）。實作在 `app/observability/llm_events.py`（與認證事件分開，函式不接收任何文字內容的參數）。驗證：`tests/test_llm_logging.py`——欄位只含白名單、斷言整段日誌找不到訊息文字與金鑰與模型原文、警告只在斷路開啟時出現一次而不是每個被跳過的請求各一筆。

## 4. 客服的輸出契約

- [x] 4.1 新增 `app/services/cs_intents.py`：7 個意圖與 4 個實體的常數、中立 schema、語意驗證（意圖在列舉內、`reply_text` 長度與中日韓字元、建議動作長度、洩漏標記）、低信心改寫為 `unknown` 的規則。驗證：單元測試——schema 的列舉、驗證接受的值與提示詞的意圖清單三者相同；規格各情境（缺欄位、多欄位、列舉外、無中文字、5 個建議動作、剛好 0.6 與 0.59）；`reply_text` 剛好 1000 字通過、1001 字失敗，40 字與 200 字的回覆都通過（長度下限不是驗證條件）；新增意圖只需改常數一處的測試。
- [x] 4.2 新增 `app/services/cs_prompt.py`：固定的繁體中文系統指令（範圍、精準回答且沒有長度目標與不得編造的要求、意圖規則、`unknown` 與 `human_handoff` 的處理、資料不是指令、純文字回覆）、由 `SECRET_KEY` 推得的洩漏標記、使用者內容區的資料物件與使用者本次訊息的 part、三句降級文案（驗證失敗、繁忙、被阻擋）與依降級原因取文案的函式（「請換個說法」的固定文案屬於 4.1 的低信心改寫）。驗證：單元測試——系統指令在任何使用者文字下都完全相同、使用者與摘要與歷史的惡意文字（引號、換行、模仿結尾的字串）只出現在使用者內容區且被跳脫、標記不在原始碼與版本庫（`git grep` 實際標記為空）、標記隨 `SECRET_KEY` 改變、降級文案逐字符合規格。

## 5. 對話服務與記憶

- [x] 5.1 `input_cleaning` 新增保留換行的 `clean_message`（NFC、去控制字元、統一換行、上限 1000 字）；新增 `ContextBuilder`（最近 10 個正常輪、降級輪整輪排除、摘要、含當下 `member_tier` 的狀態）與狀態計算純函式。驗證：單元測試——1000 與 1001 字、NUL 與孤立代理字元、30 輪只取最近 10 輪、降級輪不進上下文但仍在歷史、狀態各情境（`account_help` 設定、`greeting` 清空、降級不變、自由文字與 email 不進狀態）。
- [x] 5.2 新增 `ChatService`：建立（冪等）、列出、改名、刪除、送出訊息（依設計 D8 的流程與提交點）、增量拉取；先過 `require_chat_access`；`finally` 清除標記。驗證：整合測試——同一 `client_msg_id` 兩次得到相同結果且假供應商只被呼叫一次；相同識別碼不同內容為 `message_id_reused`；寫入但無回覆的訊息重送只補一次呼叫；兩個請求並發一個 409；標記 61 秒後可接手；處理中途丟出例外後標記被清除；模型呼叫期間 Session 沒有開啟的交易（假供應商在呼叫時斷言）；寫入回覆撞唯一約束時改讀既有回覆；降級回覆為 `fallback` 且不改狀態；預設標題只在第一則訊息且標題仍是「新對話」時取前 30 字，改名後不被覆寫；閘門失敗時沒有任何寫入與呼叫；空白訊息不呼叫模型；沒有模型供應商時送出訊息回 `service_unavailable`（503）且資料庫沒有新增對話或訊息，而列表與讀取照常運作。
- [x] 5.3 新增 `SummaryService` 與延後動作的接線（自己的 Session、最多折疊 3 輪、條件式寫入、摘要 ≤800 字、使用摘要模型、失敗不影響回覆）。驗證：整合測試——第 11 輪完成後第 1 輪被併入且標記推進、落後 3 輪能補完、摘要呼叫失敗標記不動且下一次補做、兩個折疊並發只有一個成功且標記不倒退、摘要超過 800 字視為失敗、帳本用途為 `summary` 且使用摘要模型、重啟（新應用程式實例）後從資料庫標記補做。

## 6. HTTP 層

- [x] 6.1 新增 `cs_api_controller.py`（D11 的六個端點；只做 JSON 解析、呼叫 Service、回應）與 `cs_page_controller.py`（`/cs`，需登入、傳遞初始狀態，含 `request_timeout_ms` = (總預算秒數 + 5) × 1000 與 `chat_gate`；`AccessPolicy` 新增不拋例外的 `chat_gate(user)`，`require_chat_access` 改以它判斷）；`cache_headers.py` 把 `no-store` 擴及 `/api/cs/`；`app/__init__.py` 註冊藍圖。驗證：API 測試——回應形狀（不含意圖、實體、信心、摘要、狀態）、各狀態碼與代碼（`age_confirmation_required`、`conversation_busy`、`message_id_reused`、`version_conflict`、503）、415 與 413、CSRF 缺少為 403、`no-store`；`tests/support/idor.py` 對每個帶識別碼的端點（改名、刪除、送訊息、拉訊息）驗證他人與不存在的回應逐字相同；管理員角色讀取他人對話為 404。
- [x] 6.2 更新 README 的端點表（`tests/test_readme.py` 的路由一致性必須通過）、設定表與 `.env.example` 的說明、客服的使用與限制（C-4 之前不得對公開網路開放）。驗證：`tests/test_readme.py` 通過；整條對話流程（含失敗與降級）的日誌掃描測試在 `tests/test_log_scan_full_flow.py` 擴充後通過，找不到訊息、回覆、摘要、模型原文與金鑰。

## 7. 前端

- [x] 7.1 新增 `static/js/cs/cs-sidebar.js`（依寬度決定預設開關、安全讀寫 `localStorage`、`matchMedia` 包裝，純函式）。驗證：`tests/js/cs-sidebar.test.js`——992 與 991 的預設、桌面記住與手機不記住、`localStorage` 丟出例外時退回預設。
- [x] 7.2 新增 `static/js/cs/cs-store.js`（IndexedDB 與記憶體兩個後端、開啟失敗自動退回記憶體、對話與訊息與待送三個資料集）。驗證：`tests/js/cs-store.test.js` 以記憶體後端與一個最小的假 IndexedDB 驗證讀寫、依 `seq` 排序、待送依建立順序、以後端覆寫、開啟失敗退回記憶體。
- [x] 7.3 新增 `static/js/cs/cs-sync.js`（可注入 API 與儲存層的同步引擎：待送依序重送且對話內序列化、成功後以回應取代待送、網路錯誤與逾時保留並停止本輪、`conversation_busy` 退避、其他 4xx 標記為無法送出、`since_seq` 以後端為準覆寫、建立對話冪等重送）。驗證：`tests/js/cs-sync.test.js`——重送不產生重複、離線期間連續三則依序送出、衝突以後端為準、退避與停止條件、無法送出的訊息可被移除。
- [x] 7.4 新增 `templates/cs/chat.html`、`static/js/cs/chat.js`、`static/css/cs.css`：Sidebar（`v-show`＋`<transition>`、遮罩、Esc）、會員等級標籤、訊息列表（純文字、保留換行、降級標示）、打字機、chips（`v-if`、淡入、膠囊樣式）、自製的改名與刪除 Modal 與 Toast、送出的 axios 逾時（只讀初始狀態的 `request_timeout_ms`，不寫死）與 `AbortController`（取消、切換對話、離開頁面）且 `finally` 恢復介面、閘門訊息與 503 的引導、IndexedDB 不可用時退回記憶體。驗證：`tests/architecture/test_frontend_rules.py` 與 `test_assets_manifest` 掃描通過（無 `v-html`、無 `data-bs-*`、無外部資源）；`tests/test_cs_pages.py` 驗證 `/cs` 的登入導向與初始狀態（`chat_gate` 在可用、沒有生日、年齡不足、信箱未驗證四種帳號各為預期值，且與 `require_chat_access` 的結果一致；預設 `request_timeout_ms` 為 35000；總預算設為 40 秒時為 45000），`chat.js` 與範本中沒有寫死的逾時數字（掃描測試），且根元素有 `v-cloak`（避免掛載前顯示原始範本）；人工驗收見 8.3。

## 8. 驗收與整合

- [x] 8.1 更新容器驗收腳本與 `tests/test_container_files.py`：staging 的環境變數加上假的 `GEMINI_API_KEY`；新增檢查——staging 缺金鑰時容器以非零結束且輸出只指出變數名稱、匿名呼叫 `/api/cs/*` 為 401、匿名開啟 `/cs` 被導向登入、沒有年齡資料的管理員送訊息為 403、完成補填後可建立與列出與改名與刪除對話（不呼叫模型，容器內沒有可用的供應商），日誌沒有金鑰。於 WSL 的 Docker 重新建置並執行。驗證：`scripts/verify_container.py` 全部通過，檢查項目數與映像大小記入 README。
- [x] 8.2 完整測試：pytest（SQLite 與 SQL Server `project_test`）、`node --test "tests/js/**/*.test.js"`、`openspec validate cs-chat-core --strict`。驗證：全數通過，數字記入 README 的驗收紀錄。
- [x] 8.3 Edge 無頭瀏覽器驗收（比照 `age-verification-birthdate` 的做法，暫時伺服器注入假供應商、SQLite 檔案資料庫，**逐張檢視截圖**）：`/cs` 的登入導向與閘門引導、Sidebar 在 1280px 推擠與 360px 覆蓋且各自的預設與記憶行為、新增與改名與刪除與 Modal 與 Toast、送出與打字機與 chips 的出現與消失、降級訊息、重新整理後歷史立即顯示且 chips 再現、離線時閱讀暫存與待送與恢復後重送不重複、請求逾時後以序號補齊、IndexedDB 被禁用時仍可使用、鍵盤可操作、360px 與 1280px 無水平捲軸且沒有外部請求。驗證：結果與項目數寫入 README 的瀏覽器驗收紀錄，全部通過；發現的版面問題修正後重新截圖。
- [x] 8.4 **（使用者已於 2026-10-29 同意；量測與記錄已完成，結果見 README「真實 Gemini 驗收」。待辦：回覆的繁體中文品質待使用者目視確認；使用者於 2026-10-29 更正：客服只需精準回答、不要求長回覆，350 至 1000 字只屬於 RPG 劇情；第一次真實驗收的 140 至 320 字就是合適的長度，嘗試過的固定結構提示詞（212 至 431 字、開場句型僵硬）已撤回，撤回後的提示詞尚未以真實 Gemini 驗證）** 一次真實的 Gemini 驗收（使用者決定：過渡期本機與驗收環境以 `GEMINI_MODEL=gemini-3.5-flash-lite` 為主要模型，因 `gemini-3.8-flash` 免費層每日只有約 20 次；所以量到的字數、品質與延遲是這個模型的數字，逾時要不要調整等實際的正式模型確定後再決定）：以 `.env` 的金鑰經真實轉接器，對暫時的 SQLite 伺服器用合成文字（不含個資）走完問候、平台問題、帳號問題、意見回饋、要求真人客服、訂單問題（應為 `unknown`）、越權要求與一次超過 10 輪的對話。驗證：結構描述被供應商接受（若有不支援的鍵則調整轉接器的轉換並補單元測試）；意圖與回覆的繁體中文品質由使用者目視確認；帳本有對應紀錄（模型、token、思考 token、延遲）；統計回覆的字數分布（是否精準簡潔、是否有編造平台沒有的功能）、單次耗時是否逼近 20 秒、輸入 token 在第 10 輪時的實際值；日誌找不到金鑰與文字；若遇到 429 或 503，確認備援與降級路徑在真實環境同樣運作；把結果與觀察（包含歷史放進資料物件的回覆品質）記入 README 與 RPG.md。
- [x] 8.5 本機開發資料庫 `project` 升級到 `0005`（**需使用者同意**；先 `COPY_ONLY` 備份並以 `RESTORE VERIFYONLY` 驗證）。驗證：`alembic current` 顯示 `0005_chat_core`、`/readyz` 回 200、三張表存在且為 0 筆。
- [x] 8.6 在 [RPG.md](../../../docs/RPG.md) 標註 C-3 已完成的範圍與對 T-2 的調整（備援切換的決策在 `LlmService`），並在 [report.md](../../../docs/report.md) 的 5.2 表標註 C-3 的狀態與對 D-01、D-05 的取捨（7 個意圖、不設 `deleted_at`）。驗證：以 Grep 在兩份文件找到更新後的說明。
- [x] 8.7 更新 docs/ 的設計文件：新增系統架構、資料模型、帳號與安全、客服對話、模型呼叫層、前端結構、營運與部署、路線圖共八份 Mermaid 設計文件與索引（涵蓋本 change 與先前已封存的能力），並新增 `tests/test_design_docs.py` 與技能 `design-docs-sync`。驗證：`tests/test_design_docs.py` 通過。

## Workflow follow-up

- 審查通過後提交，再封存（`/openspec-archive-change cs-chat-core`），封存時同步主規格：新增六個能力，修改 `app-configuration`、`application-layering`、`access-control`、`service-observability`。
- 交接給 C-4：提示詞的「平台資料」中會員額度目前是 AGENTS.md 第 7 節的初始值（`cs_prompt._QUOTA_FACTS`），額度改為可設定時須改取自同一份設定，否則模型會說出與實際不符的數字；帳本的 `counted` 與 `blocked`（含輸入或輸出被擋）供額度與「一日被擋 3 次暫停」使用；`error_detail` 的 14 天清除；帳號刪除前須先清除帳本的對話編號（`llm_call_logs.conversation_id` 沒有外鍵）；對話數上限；管理員檢視意見回饋與被標記對話的專用 Repository 方法與稽核；對話匯出。
- 交接給 C-6：新增 `order_query`、`refund_request` 兩個意圖與 `order_id`、`refund_reason` 兩個實體（常數、schema、提示詞各一處）與必填實體的追問規則。
- 交接給 RPG：沿用 `LLMProvider`、`parse_llm_json`、`LlmService`、帳本與斷路；新增串流呼叫（20 秒內沒有第一個片段才切備援，見 RPG.md T-9）。
- 對外開放前必須以實際使用的供應商條款重新確認年齡規則（見 RPG.md R-15.4）；C-3 完成後、C-4 完成前不得以真實金鑰對公開網路開放。
