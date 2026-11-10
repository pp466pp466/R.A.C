# Tasks

> 建議的批次（apply 前依 `openspec-batch-workflow` 請使用者確認）：第 1 組＝純邏輯與常數；第 2 組＝Repository 與模型呼叫的預算參數；第 3 組＝抓取執行者與串流接線；第 4 組＝玩家檢視與儲存的 API；第 5 組＝驗收與收尾。測試一律用 `.venv\Scripts\python.exe -m pytest`（輸出中文時加 `PYTHONIOENCODING=utf-8` 與 `-X utf8`）；真實 SQL Server `project_test` 同時只能有一個測試行程。本 change 沒有遷移。

## 1. 純邏輯與常數（容量、清理、視窗、追加、提示詞）

- [x] 1.1 `quota_policy`：`TierLimits` 新增 `notebook_capacity`（free 2000、plus 5000、vip 10000），`tier_facts()` 的額度說明加入記事簿容量；新 `app/services/notebook_policy.py`（純函式）：`capacity_for`（不認得的等級用 free）、`reserve_for`（容量 ÷ 4 取整）、`ai_limit_for`、`is_full`、`clean_notebook_text`（NFC、統一換行為 `\n`、Tab 換空格、去除除換行外的控制字元、頭尾空白，孤立代理字元回 `None`）、`window_for(completed)`（邊界 B = ⌊(完成輪數 − 1) ÷ 10⌋ × 10，B 為 0 回 `None`）、`merge_entries(existing, entries)`（條目清理成單行、去空、與既有各行或彼此相同者去除、以 `\n` 連接並計算追加後的文字與長度）。驗證：`tests/test_notebook_policy.py` 通過（三等級的容量與預備空間、長度 1999／2000／2001／2399／2500／2501、降級視同已滿、升級恢復、第 10 輪 `None`、第 11 輪 B=10、第 20 輪 B=10（仍是前一個視窗，已處理則無待抓取）與第 21 輪 B=20、清理案例、去重、尾端沒有換行時補一個並計入長度）；既有客服額度說明的測試若固定了整段文字則一併更新並通過。
- [x] 1.2 新 `app/services/rpg_notebook_prompt.py`（純函式）：系統指令（繁體中文與語言參數預設 `zh-Hant-TW`、只輸出 JSON、記錄範圍與「不記對白與一次性細節」、每次最多 5 條且每條 50 字內、依三種內容模式的分級界線、標記字串、「資料區塊是資料」聲明，且不含任何玩家文字）、`build_capture_contents`（資料區塊：現有記事簿、目前用量與容量、視窗內 10 輪的行動與劇情，皆 `neutralize`）、`build_condense_contents`（帶上次的條目與可追加的上限）、`CAPTURE_SCHEMA`（`entries` 為字串陣列，**不設 `maxItems`／`maxLength`**，Service 不因條數與字數丟棄）、語意規則（不得含標記字串）。驗證：`tests/test_rpg_notebook_prompt.py` 通過（系統指令沒有玩家文字、含每次 5 條與 50 字的要求、資料區塊順序、偽造分隔符號被中和、schema 不含長度上限、語言參數）。
- [x] 1.3 `models/llm_call_log.py` 新增 `KIND_RPG_NOTEBOOK = "rpg_notebook"` 並加入用途列舉；後台 `app/static/js/admin/admin-format.js` 的用途標籤補「記事簿」。驗證：既有的用途列舉與後台標籤對照測試通過（含新值），`node --test` 通過。
- [x] 1.4 架構守門：`notebook_policy`、`rpg_notebook_prompt` 加入既有的「不得匯入 ORM 模型、Repository 或 Flask」測試。驗證：`tests/architecture` 全數通過。

## 2. Repository 與模型呼叫的預算參數

- [x] 2.1 `SessionRepository`（皆帶 `user_id`，沿用 `get_owned`）：`turns_in_range(user_id, public_id, first, last)`（只含 `complete`、依輪數）；`lock_notebook_for_capture(user_id, public_id, boundary)`（對自己的存檔做條件式 UPDATE 取得列鎖：進行中且 `notebook_upto_turn` 小於 boundary；不成立回 `None`，成立回傳鎖內讀到的文字、版本與標記）；`append_notebook(user_id, public_id, *, text, boundary)`（版本加 1、標記設為 boundary，條件同上）；`advance_notebook_marker(user_id, public_id, boundary)`（條件式，只往前）；`save_notebook(user_id, public_id, *, text, expected_version)`（條件式：進行中且 `notebook_version` 等於預期，版本加 1；不成立時拋出 `StaleNotebookVersionError` 或 `SessionEndedError`）。驗證：`tests/test_session_repository_notebook.py` 通過（範圍取回合含中斷與被擋的排除、鎖與標記條件、兩次連續追加只有一次生效、標記不倒退、儲存的版本條件與已完結、非擁有者回 `None`），並含真實 SQL Server `project_test`。
- [x] 2.2 `LlmService.run_structured` 新增選用參數 `budget_seconds`（預設沿用 `settings.request_budget_seconds`，傳入較小值時以它為準，傳入較大值不放寬設定的預算；不影響既有呼叫）。驗證：既有 `tests/test_llm_service*.py` 全數通過且不修改斷言；新增案例：傳入較小預算時第二次呼叫的逾時不超過剩餘預算。
- [x] 2.3 錯誤型別（`services/errors.py`）：`NotebookTooLong`（422，代碼 `notebook_too_long`，帶字數與容量）、`NotebookBlocked`（422，`notebook_blocked`，只帶類別）、`NotebookConflict`（409，`notebook_conflict`，帶後端最新的記事簿〔`notebook`：文字、版本等，與檢視端點同形〕，比照 `VersionConflict`）；Controller 的錯誤處理能序列化（`NotebookTooLong` 回應頂層帶 `length`、`capacity`；`NotebookConflict` 回應頂層帶 `notebook`）。另新增 Repository 層的 `StaleNotebookVersionError`。驗證：`tests/test_errors_notebook.py` 通過（狀態碼、代碼、回應不含不該有的欄位）。

## 3. 抓取執行者與串流接線

- [ ] 3.1 `observability/rpg_events.py`：`log_notebook_captured`／`skipped`／`abandoned`／`failed`／`saved`／`save_rejected`（欄位與列舉見 design D7，只含內部編號、整數與列舉，含「超出提示詞要求」的條數與字數）。驗證：`tests/test_rpg_events.py` 新增案例通過（白名單欄位、列舉值、傳入文字型別的欄位被拒）。
- [ ] 3.2 新 `app/services/rpg_notebook_capture.py`：`NotebookCapturer.run`（design D4 的五步：短交易 A 讀取與決定、抓取呼叫〔`rpg_notebook`、預算 `min(30, 剩餘)`〕、驗證〔`merge_entries`、標記字串與洩漏、`rpg_output_rules.evaluate` 逐條累積並以前一條為相鄰句、`leak_categories`〕、長度與精簡重試〔剩餘預算不少於 20 秒才啟動〕、短交易 B 鎖定後對最新文字追加）；總預算 60 秒；全站預算用盡不推進；成功（含 0 條）、已滿、放棄推進標記，暫時性失敗不推進；整個方法不拋出。驗證：`tests/test_rpg_notebook_capture.py` 通過（只追加不改寫；溢出時完整寫入 1999＋400＝2399；超過上限精簡成功與仍超過放棄；已滿跳過且不呼叫模型；0 條；去重；規則層未通過整次放棄；標記字串失敗；暫時性失敗不推進與下次補做；跨邊界舊視窗放棄；全站預算用盡；剩餘不足 20 秒不精簡；重複觸發只生效一次；抓取期間的玩家編輯不被覆蓋；鎖內發現已滿視同跳過；帳本 `rpg_notebook` 含模型、沒有文字；不碰錢包）。
- [ ] 3.3 `TurnRunner`：`done` 被取走之後（`yield` 返回）才把 `capture_due` 設為真，條件是回合 `complete` 且不是 `ended`、未被取代；`done` 寫入時客戶端斷線（`GeneratorExit`）、被擋、中斷、失敗不設定。`registry.py` 新增 `build_notebook_capturer`（在請求內取出所有依賴，比照 `build_runner_deps`）；`game_api_controller` 的 `_event_stream` 接受 `after_close` 並以 `response.call_on_close` 註冊，回呼只在旗標成立時呼叫 `NotebookCapturer.run`，以 `try/except` 包住。驗證：`tests/test_turn_runner_notebook.py` 通過（旗標只在正常 `done` 之後成立；被擋、中斷、結局、`done` 前斷線、已完成回合重送都不觸發）；`tests/test_game_api.py`／`test_turn_runner_stream.py` 既有案例不變且通過（事件序列仍以 `done` 結束，沒有新事件）。
- [ ] 3.4 端到端整合：以假供應商走完 11 輪，驗證第 10 輪後記事簿仍空、第 11 輪的串流結束之後記事簿有條目且 `notebook_upto_turn` 為 10、下一輪的上下文含這些條目；抓取失敗後下一個回合補做；日誌掃描找不到任何記事簿文字、行動或劇情。驗證：`tests/test_rpg_notebook_flow.py` 通過。
- [ ] 3.5 文件：`docs/系統架構-architecture.md`（回合管線圖加回應關閉後的背景抓取、模組分工）、`docs/模型呼叫層-llm-layer.md`（新用途 `rpg_notebook`、`budget_seconds`）。驗證：`tests/test_design_docs.py` 通過。

## 4. 玩家檢視與儲存的 API

- [ ] 4.1 新 `app/services/notebook_service.py`：`view`、`save`（design D6 的順序：閘門 → 擁有者 → 清理 → 已完結 → 長度 → 規則層 → 條件式寫入 → 衝突時重新讀取並回報）；`registry.get_notebook_service`；`GameService.session_snapshot` 加 `notebook` 摘要（版本、字數、容量、已滿，不含全文）。驗證：`tests/test_notebook_service.py` 通過（容量三等級；2001 被拒；2240 字刪到 2100 被拒、刪到 2000 通過；已滿時原文再儲存被拒；規則層命中只回類別且不回顯；分級不符不擋；版本衝突附最新內容；已完結 `session_ended`；非擁有者 404；清空；儲存不呼叫模型也不改標記）。
- [ ] 4.2 `game_api_controller`：`GET`／`PUT /api/game/sessions/<id>/notebook`（登入、CSRF 與 Origin、`GET` 回應 `Cache-Control: no-store`、JSON 錯誤）。驗證：`tests/test_notebook_api.py` 通過（未登入 401、無 CSRF 被拒、非擁有者 404、各錯誤代碼與狀態碼、快照摘要不含全文、已完結仍可 `GET`）。
- [ ] 4.3 隱私：日誌掃描、匯出、刪除與管理員不可讀。驗證：`tests/test_notebook_privacy.py` 通過（走過 AI 追加、玩家儲存與被規則層拒絕的儲存後掃描日誌，找不到任何記事簿文字或被拒內容；個人資料匯出含記事簿全文；存檔與帳號刪除後記事簿消失；後台沒有可讀取記事簿全文的端點），既有的抹除涵蓋與匯出測試通過。
- [ ] 4.4 文件：README（端點表加兩個記事簿端點、驗收紀錄預留）、`docs/路線圖-roadmap.md`（RC-2b 已實作）、`docs/RPG.md`（7.16 標示已立案並指向 change、R-17 的狀態）。驗證：`tests/test_design_docs.py`、`tests/test_readme.py` 通過。

## 5. 驗收與收尾

- [ ] 5.1 假模型與 HTTP 驗收：`scripts/rpg_acceptance_harness.py` 的 `FakeRpgModel` 支援抓取呼叫與動作關鍵字 `測試記事簿超量`／`測試記事簿放棄`／`測試記事簿違規`／`測試記事簿慢速`（約定寫在腳本開頭）；`scripts/acceptance_rpg_turn.py`（或新的 `scripts/acceptance_rpg_notebook.py`）涵蓋：11 輪後記事簿被追加（第 10 輪後仍空）、串流在抓取完成之前就結束（慢速案例）、玩家編輯與版本衝突、已滿與刪到容量以內、規則層命中、日誌沒有記事簿文字、帳本有 `rpg_notebook` 且沒有遊戲幣異動。驗證：`.\start-acceptance.ps1` 之後腳本全部通過；`tests/test_acceptance_rpg_scripts.py` 新增的假模型行為案例通過。
- [ ] 5.2 容器驗收：`scripts/verify_container.py` 的 RPG 檢查加入記事簿——以 `gunicorn.rpg.conf.py` 在容器內確認 **gthread 先送出結束 chunk、再執行關閉回呼**（慢速抓取時 `done` 之後很快看到連線結束，記事簿稍後才更新）與**客戶端在 `done` 之後立即斷線時抓取仍然完成**。驗證：容器驗收全部通過（含新增項目）；若前提不成立，依 design D1 的退路改為在產生器內執行並重跑。
- [ ] 5.3 真實模型驗證（人工執行，只當驗證、不算測試）：`scripts/acceptance_rpg_real_model.py` 增加記事簿步驟，以真實 OpenRouter 模型走 11 輪並觀察抓取的條數與字數（對照提示詞要求的 5 條與 50 字）、耗時與品質、規則層是否誤擋。結果寫入 `docs/RPG.md` 7.16 與 README 的驗收紀錄。驗證：紀錄已寫入，並列出對提示詞的調整建議（若有）。
- [ ] 5.4 破壞驗證：故意弄壞再確認新增測試會失敗——抓取不在鎖內檢查標記（重複追加）、第 10 輪就觸發（邊界算錯）、已滿仍呼叫模型、暫時性失敗仍推進標記、玩家儲存不比對版本、條目不經規則層、條目數超過 5 就丟棄（違反決定 3）。每項驗證後 `git checkout` 還原。驗證：每項至少有一個測試失敗，結果記入 README 的驗收紀錄。
- [ ] 5.5 全套測試與設計文件：`pytest -q` 全數通過、`node --test` 通過、`openspec validate rpg-notebook` 通過；更新 `docs/` 的對應設計文件（對照表見 `docs/README.md`）並讓 `tests/test_design_docs.py` 通過。

## Workflow follow-up

- 全部任務完成、使用者驗收後 `openspec archive`，並更新 `docs/路線圖-roadmap.md` 的狀態與日期、各設計文件標頭的「對應 change」。
- 下一個 change：RC-2c（重新生成與編輯輸入，含「斷線等於免費重來」的處理）、RC-3（`/game` 畫面與記事簿面板）、RC-4（壓測，計入抓取佔用的執行緒）。
