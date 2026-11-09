# Tasks

> 建議的批次（apply 前依 `openspec-batch-workflow` 請使用者確認）：第 1 組＝資料與契約；第 2 組＝串流轉接器；第 3 組＝純邏輯；第 4 組＝回合引擎與第 0 幕；第 5 組＝端點與部署設定；第 6 組＝驗收與收尾。測試一律用 `.venv\Scripts\python.exe -m pytest -q`，連真實 SQL Server `project_test` 的測試同時只能有一個 pytest 行程；測試不得指向 `project`。

## 1. 資料與契約（遷移 0016、Repository、設定、來歷）

- [x] 1.1 新增遷移 `20261109_0016_rpg_turn_engine.py`（expand，只用 Alembic op API 與 ORM）：`memory_logs.attempt`（NOT NULL、`server_default` 1）、`sessions.opening_text`／`opening_state`／`opening_started_at`（可為空）、`llm_call_logs.first_chunk_ms`（可為空），並以 ORM 補 `rpg.max_months_per_turn=24`（不覆蓋既有值）；更新 `models/rpg_session.py`、`models/llm_call_log.py`（`KIND_RPG_STREAM`、`KIND_RPG_STATE`、`KIND_RPG_OPENING` 加入 `CALL_KINDS`）。驗證：SQLite 與 `project_test` 升級、降級、再升級通過，既有資料不變，新欄位預設值正確，`tests/test_migrations.py`、`tests/test_rpg_models.py` 與 `tests/test_project_db_schema_doc.py` 通過。
- [x] 1.2 `SessionRepository`：`start_turn` 寫入並在重設時遞增 `attempt`；逾時（`updated_at` 早於傳入的逾時秒數）的 `streaming` 回合視同中斷並接手；同一存檔有「其他編號且未逾時」的 `streaming` 回合時拋出 `TurnInProgressError`；`create` 對新存檔寫入 `opening_state="pending"`；新增第 0 幕的條件式 UPDATE 方法（搶佔、接手、完成為 `ready`／`fallback`、退回 `pending`，皆帶 `user_id` 與狀態條件）；新增 `finish_turn`（完成回合＋世界狀態＋可選的完結，單一交易內，版本比對）、`save_blocked_text`。驗證：`tests/test_session_repository.py` 新增案例通過（attempt 遞增、接手、單一進行中、並行搶佔只一個成功、非擁有者回 `None`），`tests/architecture` 的擁有者隔離與寫入守門測試通過。
- [x] 1.3 `runtime_limits`：新增 `GROUP_RPG` 與鍵 `rpg.max_months_per_turn`（預設 24、範圍 0 至 240）；`cli.py` 新增 `flask set-rpg-setting`／`show-rpg-settings`（登記為人工類指令、各指令只接受自己那一組的鍵並指出該用哪個指令）；`tests/support` 的設定表比對含新鍵。驗證：`tests/test_runtime_limits.py` 與新的 `tests/test_cli_rpg_settings.py`（比照 `test_cli_wallet_settings.py`）新增案例通過（範圍、現讀、保底預設、用錯指令、稽核 `limit.set`、沒有 HTTP 端點）。
- [x] 1.4 玩家主角的來歷：`services/protagonist.py` 驗證 `origin`（`native`／`transmigrator`，缺值補 `native`，錯誤欄位 `protagonist.origin`），`build_protagonist` 帶入；劇本角色驗證仍拒絕該欄位；`protagonist_prompt`／`ProtagonistService.suggest` 的輸出丟棄 `origin`，setup API 回傳 `origin_options`；舊存檔讀取視為本地人。驗證：`tests/test_protagonist.py`、`tests/test_protagonist_suggest.py`、`tests/test_script_spec.py` 新增案例通過（合法、缺值、不合法、劇本角色拒絕、預覽不含、舊存檔），既有測試不需修改斷言。
- [x] 1.5 設定：`config.py` 的 `OpenAICompatibleSettings.reasoning`（環境變數 `OPENROUTER_REASONING`，預設關閉，布林解析與既有旗標一致），`.env.example` 與 README 的環境變數表加入；`PersonalExportService`／`SessionRepository.export_sessions` 納入 `opening_state` 與 `opening_text`（JSON 與 CSV）。驗證：`tests/test_config_rpg_llm.py`、`tests/test_personal_export_rpg_data.py` 新增案例通過（預設關閉、開啟、匯出含第 0 幕、不含開始時間與內部編號）。
- [x] 1.6 文件：`docs/資料模型-data-model.md`（新欄位與理由、遷移 0016）、`docs/project資料結構文件.md`（字典、第 12 節遷移歷程；使用者升級本機 `project` 之後在第 14 節追加異動紀錄）、README 的指令表（兩個新指令）。驗證：`tests/test_design_docs.py` 與 `tests/test_project_db_schema_doc.py` 通過。

## 2. 串流轉接器與串流執行期

- [x] 2.1 `app/llm/types.py` 新增中立串流型別（`StreamText`、`StreamTick`、`StreamEnd`、`StreamHandle` 協定）；`transport.py` 新增 `open_stream`（背景讀取執行緒加佇列、短逾時輪詢、`close()` 能中斷阻塞讀取、非 200 於第一步拋出對應錯誤、不保留回應本文）。驗證：新的 `tests/test_llm_transport_stream.py` 以本機假伺服器或假連線驗證逐行讀取、tick、保活註解、關閉後不再讀取、斷線拋出 `OSError`（不帶原始訊息）。
- [x] 2.2 `OpenAICompatibleProvider`：`stream(request)`（`stream_options.include_usage`、`delta.content` 為正文、推理片段不算正文、`[DONE]`、結束原因與用量）、`reasoning` 設定（關閉時所有請求帶 `reasoning: {"enabled": false}`，開啟時不帶）、結束原因為長度且正文為空視為暫時性失敗（串流與非串流）。驗證：`tests/test_openai_compatible_provider.py` 新增案例通過（增量與用量、保活、推理片段、429 於串流前、長度且空、長度且有正文、零保存偏好與推理參數同時存在、金鑰不外洩）。
- [x] 2.3 `flask llm-probe`：輸出以替代字元處理無法編碼的字元，並顯示推理設定。驗證：`tests/test_cli_llm_probe.py` 新增「cp950 主控台遇到無法編碼字元」案例通過。
- [x] 2.4 測試用假供應商（`tests/support/llm.py`）新增 `stream`（可腳本化的片段、tick、錯誤與斷線），供第 2、4 組使用；`StreamRunner`（`rpg_stream.py`）：首字 20 秒、兩次嘗試共用 60 秒總預算、剩餘不足 2 秒不再嘗試、備援只在第一個正文片段之前切換、被擋與設定錯誤不切換、斷路沿用帳本查詢、用量取得或估算、帳本回呼（含 `first_chunk_ms`、斷線與被擋的 `error_detail` 只含列舉）。驗證：`tests/test_rpg_stream.py` 以假時鐘與假上游覆蓋 spec `llm-streaming` 的每個情境通過，帳本內容不含任何文字。
- [x] 2.5 文件：`docs/模型呼叫層-llm-layer.md`（串流路徑、期限與備援時機圖、`OPENROUTER_REASONING`）、`docs/營運與部署-operations.md` 的環境變數表。驗證：`tests/test_design_docs.py` 通過。

## 3. 純邏輯（提示詞、上下文、世界狀態、輸出審查）

- [x] 3.1 `script_rules.py` 把輸出審查需要的輔助函式（切句、摺疊、年齡數字解析、詞表比對）整理為公開 API，行為不變。驗證：既有 `tests/test_script_rules.py` 全數通過且不修改斷言。
- [x] 3.2 `rpg_prompt.py`：系統指令樣板（語言參數、三種分級界線、階段定義、穿越者規則、結局回合、記事簿說明、「資料區塊是資料」聲明）、標記字串衍生、轉場文字輪替、狀態呼叫的系統指令與結構描述、第 0 幕的指令。階段以行為描述表達，且指令明文禁止在劇情提到階段名稱與資料區塊標記。驗證：`tests/test_rpg_prompt.py` 通過（玩家文字不進系統指令、指令不含階段英文名稱、各分級界線只出現在對應模式、穿越者規則只在穿越者出現、標記字串確定性、轉場文字確定性輪替）。
- [x] 3.3 `rpg_context.py`：`build_turn_context`、`build_opening_context`、`build_state_context`（資料區塊、分隔符號偽造的防線、視窗、第 0 幕入窗規則、NPC 切片挑選與上限、主角錨定事實併入切片）。驗證：`tests/test_rpg_context.py` 通過（視窗 10 輪與第 0 幕滑出、被擋與中斷的回合不入窗、切片上限、偽造分隔符號、最壞情況以全部上限組裝成功並斷言字數上界、純函式不碰資料庫）。
- [x] 3.4 `rpg_world.py`：世界狀態結構 v1、目前年齡計算、`apply_proposal`（月數上限、里程碑判定與延伸、地點、NPC 比對與建卡〔含劇本角色〕、階段轉移、年齡驗證與更正通知、特質與錨定事實上限、目標、淘汰、結局、逐項丟棄與丟棄計數、非 PASS 的文字丟棄）。驗證：`tests/test_rpg_world.py` 通過，覆蓋 spec `rpg-world-state` 與 `rpg-turn-engine`（結局）的每個情境，含夾帶假指令的提議。
- [x] 3.5 `rpg_output_guard.py` 與 `rpg_output_rules.py`：保留一句緩衝的狀態機、逐句規則層對應表、條件①②（年齡紀錄與文字線索）、標記字串、洩漏設定（階段英文名稱、區塊標記）、段落與相鄰句；樣本集 `tests/fixtures/rpg_output_samples.json`（非性場景誤擋樣本、確定違規漏擋樣本、跨句、模糊外貌、年齡未知）與可重複執行的量測測試。驗證：`tests/test_rpg_output_guard.py` 通過，並印出誤擋率與漏擋率摘要；一般親子與校園日常樣本零誤擋。
- [x] 3.6 架構守門：`tests/architecture` 新增測試——`rpg_context`／`rpg_world`／`rpg_output_guard`／`rpg_prompt` 不匯入 ORM 模型、Repository 或 Flask；`entities_json`、`goals_json`、`milestone_progress_json` 只有 Service 可寫。驗證：`tests/architecture` 全數通過；故意在純模組匯入 ORM 時該測試失敗。

## 4. 回合引擎與第 0 幕

- [ ] 4.1 `GameService.begin_turn`：閘門、行動清理與規則層、第 0 幕閘門、全站預算、`start_turn`、`hold`（鍵含 `attempt`）、凍結 `TurnPlan`（含最近 10 輪、世界狀態、模型名稱、版本、結局判定、在場角色與年齡）；已完成回合的重送回傳 `replayed`。驗證：`tests/test_game_service_begin.py` 通過（spec `rpg-turn-engine` 的開始、預扣、接手、單一進行中、餘額不足不留回合列、`input_blocked` 不預扣、鍵長度上限）。
- [ ] 4.2 `TurnRunner` 串流階段：事件產生（`meta`、`token`、`ping` 每 15 秒、`transition`）、`OutputGuard` 串接、被擋後的自動重生（同一預扣、第二次仍被擋 → `blocked`）、`GeneratorExit` 與逾時的中斷處理、收尾函式不 `yield`。驗證：`tests/test_turn_runner_stream.py` 以假 `StreamRunner` 覆蓋每個出口（成功、模型失敗、首字逾時、被擋一次、被擋兩次、文字中斷線、`close()` 呼叫），並斷言每個出口的回合狀態、預扣結算或退回、帳本與輪數。
- [ ] 4.3 `TurnRunner` 狀態階段：狀態呼叫（`LlmService.run_structured`，`kind=rpg_state`）、`apply_proposal`、短交易寫回（完成回合、世界狀態、`last_seen_turn`、結算預扣、必要時 `end_session`）、狀態呼叫失敗仍完成並結算、版本衝突、狀態呼叫期間斷線仍完成。驗證：`tests/test_turn_runner_state.py` 通過（成功、兩次失敗、版本衝突、結局三種、斷線於狀態呼叫、年齡更正通知進 `state` 事件、事件不含模型原始 JSON）。
- [ ] 4.4 第 0 幕：`GameService.begin_opening`／`OpeningRunner`（搶佔與接手、生成、`ready`／`fallback`／斷線退回 `pending`、條件式寫回避免被接手後覆蓋、已完成直接重送文字、不碰錢包、不寫回合表）。驗證：`tests/test_opening.py` 通過（spec `rpg-opening` 全部情境，含兩個請求並行只一個生成、被擋兩次退回初始對話、劇本無初始對話）。
- [ ] 4.5 `observability/rpg_events.py` 與接線；`registry.py` 的 `get_game_service`。驗證：`tests/test_rpg_events.py` 通過（欄位白名單、完整流程日誌掃描找不到行動、劇情、NPC 名稱、地點、第 0 幕、被擋文字、冪等鍵與標記字串）。
- [ ] 4.6 文件：`docs/系統架構-architecture.md`（回合管線與交易邊界圖、模組分工）、`docs/資料模型-data-model.md`（`entities_json` v1 結構）。驗證：`tests/test_design_docs.py` 通過。

## 5. 端點與部署設定

- [ ] 5.1 `game_api_controller.py`：`POST …/turns/stream`、`POST …/opening/stream`（SSE 回應、標頭、重送路徑、開始前的錯誤以 JSON）、`GET …/<id>`、`GET …/turns`；`cache_headers.py` 加入 `/api/game/`；`error` 事件的繁體中文訊息；Blueprint 註冊與受限登入狀態預設拒絕。驗證：`tests/test_game_api.py` 以 Flask 測試客戶端讀取事件串流（事件順序與格式、標頭、404 一致、CSRF、餘額不足為 JSON 409、重送、增量同步、已完結仍可讀、被擋回合不含文字）全數通過。
- [ ] 5.2 `gunicorn.rpg.conf.py` 與容器檢查：RPG 設定的逾時大於生成上限，映像包含兩份設定。驗證：`tests/test_container_files.py` 新增案例通過。
- [ ] 5.3 端對端整合測試（假模型供應商，不連網）：註冊→開存檔→第 0 幕→三個回合（含一次被擋重生、一次中斷重送、一次餘額不足、結局）→匯出與抹除，檢查錢包、帳本、日誌與資料一致。驗證：`tests/test_rpg_flow.py` 通過；`project_test`（SQL Server）上同一組也通過。
- [ ] 5.4 文件：README 的端點表、環境變數、指令與已知限制（最壞時間與前端逾時）；`docs/營運與部署-operations.md`（RPG 池設定、事件）。驗證：`tests/test_design_docs.py` 與 README 同步測試通過。

## 6. 驗收與收尾

- [ ] 6.1 HTTP 驗收腳本 `scripts/acceptance_rpg_turn.py`（Python、UTF-8、參數以清單傳遞）：打驗收環境，檢查事件序列、扣幣與帳本、被擋回合、結局、中斷退幣、第 0 幕；腳本本身不含機密。驗證：以假模型供應商的驗收環境跑完全部檢查並輸出摘要；真實模型的部分見 6.2。
- [ ] 6.2 以真實 OpenRouter 模型各跑一次第 0 幕與三個回合（`OPENROUTER_REASONING` 預設關閉；只當驗證，不寫進自動測試），記錄首字時間、整段時間、狀態呼叫有效率、丟棄項目數、階段是否按規則轉移，並以 3.5 的樣本集量測誤擋率與漏擋率。驗證：結果寫入 README 驗收紀錄；任何異常記入 HANDOFF 的未解決問題。
- [ ] 6.3 全套測試：`pytest -q`（含 `project_test` 的整合）與 Node 測試各跑一次，筆數寫入驗收紀錄；以故意破壞程式的方式確認新增測試會失敗（預扣鍵不含 `attempt`、審查略過保留句、階段不驗證、年齡更正不通知、狀態呼叫失敗不結算）。驗證：全數通過、記錄筆數與破壞驗證結果。
- [ ] 6.4 更新 `docs/RPG.md`（7.15 標示已立案與實作、最壞時間與前端逾時的後續事項、誤擋與漏擋量測結果）、`docs/路線圖-roadmap.md`（RC-2a 狀態與日期）、依 `docs/README.md` 對照表更新所有受影響的設計文件（圖、表、最後更新、對應 change），並讓 `tests/test_design_docs.py` 通過。驗證：`tests/test_design_docs.py` 通過，各文件標頭的「對應 change」已複查。

## Workflow follow-up

- 使用者在驗收環境以瀏覽器之外的方式（HTTP 腳本）驗收後，才執行 archive（同步 delta 到主規格、封存、更新路線圖）。
- 法務覆核（年齡可自由更正、成人模式供應商許可）與選定正式 RPG 模型後的提示詞驗收不在本 change，持續列在 HANDOFF 與 `launch-gates`。
