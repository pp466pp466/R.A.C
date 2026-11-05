# Tasks

## 1. 角色驗證拆出與主角、錨定事實的純函式

- [x] 1.1 `script_spec` 新增 `validate_character(raw, *, path, forbidden_markers, collector=None)`：把 `_validate_characters` 迴圈內單一角色的邏輯抽出，劇本驗證改為逐一呼叫它（錯誤欄位路徑、訊息、標準化結果完全不變；姓名不重複的檢查仍屬劇本）。驗證：既有 `tests/test_script_spec.py` 全數不改而通過；新增測試以同一份角色資料分別當劇本角色與主角驗證，通過與失敗、錯誤欄位與標準化結果一致（`rpg-protagonist`「與劇本角色同一套規則」）。
- [x] 1.2 新增 `app/services/protagonist.py`（純函式，不碰資料庫、網路與時間）：常數（自我介紹 300 字、錨定事實文字 60 字）、`validate_protagonist(raw, *, forbidden_markers)`（路徑前綴 `protagonist`）、`validate_anchor(raw, *, script_content, protagonist_age, adult_age, lexicon, forbidden_markers)`（種類與參照存在、文字長度與標準化、未成年的戀愛／婚姻守衛）、`clean_intro(raw)`。驗證：`tests/test_protagonist.py` 逐一對應 `rpg-protagonist` 的角色卡、年齡、錨定事實、守衛情境，含**反向案例**（成年之間的戀愛聯繫通過、地點與里程碑只看主角年齡、「同門師弟」通過、關係詞出現在 `text` 以外的欄位不觸發）；錯誤訊息不回顯輸入。
- [x] 1.3 `app/data/review/lexicon.json` 新增群組 `romantic_relation`（戀人、情侶、未婚夫／妻、道侶、夫妻、婚約、定親、暗戀等的初稿，UTF-8，版本字串更新）；`load_lexicon` 與 `Lexicon` 讀入該群組，既有五組與 `check_rules` 的判定結果不變。驗證：`tests/test_script_rules.py` 與 `tests/test_script_lexicon.py` 不改而通過；新增測試確認新群組載入、版本字串變更、既有案例的判定不受影響。**詞表初稿提交前請使用者審閱。**

## 2. 資料模型、遷移與限制

- [x] 2.1 `RpgSession` 新增 `protagonist_json`（`JSON`、可為空）；`SessionRepository.create` 新增 `protagonist` 參數，同一個 INSERT 以角色卡的 `name`、`age` 寫入 `player_name`、`character_age`；`llm_call_log` 新增 `KIND_PROTAGONIST_GEN`、`KIND_PROTAGONIST_REVIEW` 並加入 `CALL_KINDS`；`LlmCallLogRepository.count_first_attempts(user_id, kind, start, end)`（僅 ORM）。驗證：`tests/test_rpg_models.py`、`tests/test_session_repository.py` 擴充；測試確認 `player_name`、`character_age` 與 `protagonist_json` 一致，且 AST 架構測試掃描除 `SessionRepository.create` 外沒有程式寫這兩欄（附反向驗證）。
- [x] 2.2 `runtime_limits.py` 新增 `SUGGEST_DAILY_PREFIX = "protagonist_suggest_daily"`、三個 `LimitSpec`（0 至 100，預設 5／15／40）與 `suggest_daily_for(tier)`（不認得的等級套用 free）。驗證：`tests/test_runtime_limits.py` 擴充：鍵集合為十個、範圍、每次現讀、缺值用保底預設、`flask set-script-limit protagonist_suggest_daily.vip 50` 成功並寫稽核、101 被拒；`show-script-limits` 輸出十個鍵。
- [x] 2.3 撰寫遷移 `0014_rpg_character_creation`：`sessions` 以 `batch_alter_table` 新增 `protagonist_json`；只插入缺少的三個限制鍵（先查再 `op.bulk_insert`，冪等）；降級移除欄位與三列（沿用 `recreate="never"` 說明）；只用 op API 與 ORM。驗證：`tests/test_migrations.py` 擴充（SQLite 與真實 SQL Server）：從 `0013` 升級保留既有存檔與七個舊限制、新欄位為空、三個新鍵存在且唯一、重複升級不重複、降級後欄位與三列消失；`alembic.ini` 與範本仍純 ASCII。
- [x] 2.4 同步 `docs/project資料結構文件.md`：以 `python -X utf8 -m tests.support.db_schema sessions` 印出的列更新第 5 節 `sessions` 的字典（含 `protagonist_json` 的說明與 JSON 結構），第 6 節固定值（`llm_call_logs.kind`、`runtime_limits` 的鍵）、第 7 節 JSON 結構、第 12 節 migration 歷程、標頭的表／欄位數與「最後更新」，並在**第 14 節最下方追加異動紀錄**（新增 `sessions.protagonist_json`、新增三個限制鍵、依據 `0014_rpg_character_creation`）。驗證：`tests/test_project_db_schema_doc.py` 與 `tests/test_design_docs.py` 通過。

## 3. 規則層與模型層審查

- [x] 3.1 `script_rules` 把 `check_rules` 的核心拆成共用的內部函式；新增 `check_protagonist_rules(character, anchor_text, intro, *, tier, adult_age)`（角色卡文字、錨定事實與自我介紹各自切句、不跨欄位；`minor_suspected` 依主角年齡判定）。驗證：既有 `tests/test_script_rules.py` 全數不改而通過；新增測試涵蓋主角版本的紅線命中（同句露骨加未成年）、**誤擋防線**（年齡 10 的主角個性「天真愛哭的學童」通過）、自我介紹中的紅線、`flag` 情境、結果不回顯命中的詞。
- [x] 3.2 新增 `app/services/protagonist_prompt.py`：審查用與生成用兩組 `system_instruction`、結構描述與語意規則；不公開標記 `HMAC-SHA256(SECRET_KEY, "protagonist-marker-v1")` 前 24 碼；生成的資料區塊只含劇本背景、人物（姓名、年齡、職業、個性，**不含隱藏特質**）、地點、里程碑與自我介紹；審查的資料區塊只含主角角色卡與錨定事實（及被指向項目的名稱）。驗證：`tests/test_protagonist_prompt.py`：玩家與劇本文字只在使用者內容區、`system_instruction` 不含任何輸入文字、含標記的輸出視為失敗、`hidden_trait` 不出現在生成的資料區塊、內容模式三種各自的提示、**提示詞無真實姓名與秘密**。**提示詞用語提交前請使用者審閱。**
- [x] 3.3 新增 `app/services/protagonist_review_service.py`：`review(user_id, protagonist, anchor, tier)` 呼叫類別 `protagonist_review`（RPG 用途模型、非串流、`run_structured`），未設定／預算用盡／降級／被過濾一律回「不可用」，結果 `pass／flag／reject` 與類別；不快取。驗證：`tests/test_protagonist_review.py`（假供應商，不打真實 API）涵蓋四種結果、四種不可用、帳本類別、日誌只含列舉與數字（`script-review` 風格的對應情境）。

## 4. 生成預覽與開存檔的服務

- [x] 4.1 新增 `app/services/protagonist_service.py` 的 `suggest(current, version_public_id, intro)`：依 design D5 的順序執行，規則層拒絕與未呼叫模型的失敗不計次，次數以 `count_first_attempts` 計（台灣日界）；輸出經角色驗證、錨定事實驗證、規則層與標記檢查（皆為 `SemanticRule`）；新增錯誤 `ProtagonistRejected`（422 `protagonist_rejected`，類別列舉）、`SuggestionLimit`（429 `suggestion_limit`）、`SuggestionUnavailable`（503 `suggestion_unavailable`）並登記於錯誤轉換。驗證：`tests/test_protagonist_suggest.py` 逐一對應 `protagonist-suggestion` 的情境：預覽不寫入存檔與資料庫（只有帳本）、自我介紹只在資料區塊且不落地、規則層拒絕不呼叫模型也不計次、重試只計一次、兩次不合法回 503、模型未設定回 503 不計次、用盡回 429、隔日重置、修改限制立即生效、全站預算用盡回 503、不影響每日對話輪數。
- [x] 4.2 `SessionService.start_session` 改版為 `start_session(current, version_public_id, *, protagonist, anchor, skip_anchor)`：依 design D6 的順序（既有四步之後接主角驗證、錨定事實存在性與守衛、規則層、模型層、寫入），所有欄位錯誤一次回報，失敗不留存檔列；錨定事實與 `skip_anchor` 二選一；`ReviewGate` 之外新增主角審查的注入點。驗證：`tests/test_session_service.py` 與新增的 `tests/test_session_start_protagonist.py` 逐一對應 `rpg-protagonist` 與 `rpg-sessions` 的情境：成功、略過錨定事實、既沒有也沒略過、指向不存在、未成年戀愛守衛、規則層拒絕不呼叫模型、模型層 `reject`／`flag`／不可用、失敗後存檔數量不變、姓名與年齡同步、非擁有者草稿 404、劇本分級 403。
- [x] 4.3 將既有呼叫舊介面 `start_session(player_name=…, character_age=…)` 的測試與輔助改用新介面：`tests/test_session_service.py`、`test_script_review.py`、`test_seed_scripts.py`、`test_script_repository.py`、`test_session_repository.py`、`test_account_erasure_rpg_data.py`、`test_data_retention_rpg.py`、`test_personal_export_rpg_data.py`、`test_log_scan_full_flow.py` 與共用輔助；在 `tests/support/` 提供一個建立合法主角的輔助。驗證：這些測試檔全數通過，且沒有任何測試仍傳入 `player_name`／`character_age` 開存檔。
- [x] 4.4 `registry.py` 組裝 `ProtagonistService`、`ProtagonistReviewService` 與改版後的 `SessionService`（RPG 用途的 `LlmService` 與標記）。驗證：registry 測試確認 RPG 模型未設定時服務仍可建立並回報不可用；測試確認 `GEMINI_*` 不被這些服務使用（沿用既有的分用測試）。

## 5. API 與觀測

- [x] 5.1 新增 `app/controllers/session_api_controller.py`（Blueprint `/api/sessions`）：`GET /setup/<version_id>`（序章與創角資料，人物只給姓名與職業，限制值取自常數單一來源，含今日剩餘次數）、`POST /protagonist/suggest`、`POST ""`；固定路徑在前、`require_login`、`read_json_object`、統一錯誤轉換；`/api/sessions/` 加入 `cache_headers.py` 的禁止快取前綴並於 `create_app` 註冊。驗證：`tests/test_session_api.py`：三支端點的成功與各種錯誤代碼與狀態碼（401、403 受限登入、404、422、429、503）、CSRF 與 Origin、`Cache-Control: no-store`、回應不含內容雜湊與審查結果與內部編號、他人草稿 404。
- [x] 5.2 新增事件（`app/observability/`）：`protagonist_suggested`、`protagonist_rejected`、`session_started`，函式簽名不接收任何文字；白名單欄位登記；`log_scan` 掃描測試擴充涵蓋「生成預覽、修改、開存檔」整個流程與失敗路徑。驗證：`tests/test_log_scan_protagonist.py`（對應 `service-observability` 的新情境）與既有 `tests/test_log_whitelist.py` 通過；日誌中找不到自我介紹、角色卡、錨定事實的任何文字。
- [x] 5.3 架構測試：`/api/sessions/*` 控制器不含權限判斷與資料庫匯入、`ProtagonistService` 每個公開方法先 `require_chat_access`、`SessionRepository` 簽名第一個參數為 `user_id`、沒有任何程式在開存檔之後更新主角年齡（附反向驗證）。驗證：`tests/test_architecture_*.py` 通過。

## 6. 生命週期與管理員守門

- [x] 6.1 個人資料匯出：`personal_export_service` 的存檔區塊加入 `protagonist`（角色卡各欄位與錨定事實），`sessions.csv` 加對應欄；舊存檔沒有主角資料時只輸出姓名與年齡且不出錯。驗證：`tests/test_personal_export_protagonist.py` 對應 `personal-data-export` 的新情境（含主角與錨定事實、舊存檔、不含自我介紹），既有匯出測試通過。
- [x] 6.2 抹除、保存期限與管理員守門：`tests/test_account_erasure_protagonist.py`（有主角資料的存檔隨帳號一併消失）、`tests/test_data_retention_protagonist.py`（逾期存檔清除時主角資料一併消失）、既有「管理員讀不到存檔」測試加斷言（讀不到主角資料）。驗證：這些測試通過。
- [x] 6.3 `scripts/verify_container.py`：新增檢查——遷移到 `0014`、`runtime_limits` 有十個鍵（含三個新的）、`sessions.protagonist_json` 欄位存在、詞表版本與新群組載入、`/api/sessions/setup/<id>` 對未登入回 401。驗證：容器驗收腳本的單元測試通過；實際執行容器驗收（如環境可用）全數通過並記錄筆數。

## 7. 整合驗證與文件

- [x] 7.1 端對端整合測試（`tests/test_protagonist_flow.py`，假供應商）：已完成年齡確認的玩家讀取創角資料、寫自我介紹生成預覽、修改角色卡、開存檔；以及用盡次數後自創、略過錨定事實、模型不可用時自創並開存檔、被拒絕後修改再開。驗證：流程測試通過，且每個失敗路徑結束時存檔數量與預期一致、日誌中沒有任何輸入文字。
- [x] 7.2 完整驗證：`pytest` 全套（含真實 SQL Server `project_test`）、`npm test` 的 Node 測試、`openspec validate rpg-character-creation --strict`。驗證：全數通過並記錄筆數；README 的「驗收紀錄」新增本 change 的結果。
- [x] 7.3 更新 README：端點表（三支新端點與其錯誤代碼）、`show-script-limits` 的十個鍵、開存檔最壞約 60 秒與 RC-3 前端逾時 65 秒的提醒、驗收紀錄。驗證：`tests/test_readme.py` 與 `tests/test_launch_docs.py` 通過。
- [x] 7.4 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`資料模型-data-model.md`（`sessions.protagonist_json` 與設計理由）、`系統架構-architecture.md`（`/api/sessions`、新服務與依賴方向）、`模型呼叫層-llm-layer.md`（兩個新呼叫類別、標記、提示詞結構）、`帳號與安全-auth-security.md`（把第 7e 節由「尚未實作」改為實作現況）、`營運與部署-operations.md`（新事件、新限制鍵、逾時）、`RPG.md`（T-13 與注意事項改為已實作）、`路線圖-roadmap.md`（RC-9 狀態與日期、RC-2／RC-3 的相依描述），並讓 `tests/test_design_docs.py` 通過。驗證：`pytest tests/test_design_docs.py tests/test_project_db_schema_doc.py` 通過。

## Workflow follow-up

- 實作完成並由使用者驗收後再 `/openspec-archive-change rpg-character-creation`；封存時同步 2 個新主規格（`rpg-protagonist`、`protagonist-suggestion`）與 5 個既有規格的修改。
- 升級開發用 SQL Server `project` 到 `0014` 之前先備份；升級後執行 `alembic current`、`alembic check`、`flask show-script-limits`，並在 `docs/project資料結構文件.md` 第 14 節追加一列「套用」。
- 序章、聯繫、創角三個畫面屬 RC-3；第 0 幕屬 RC-2。
