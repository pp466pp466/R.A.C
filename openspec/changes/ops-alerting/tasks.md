# Tasks

## 1. 資料表與設定

- [ ] 1.1 新增 `app/models/alert_state.py`（`AlertState`：`alert_key` 唯一、`state`、`severity`、`changed_at`、`last_notified_at`、`last_value`、`updated_at`；告警代碼與狀態的常數）與 migration `0009`（只新增 `alert_states`；沒有外鍵），模型加入 `app/models/__init__.py`。驗證：`tests/test_migrations.py` 新增 0009 的升降級（只多一張表、其他資料表與資料不變）、「遷移後的結構與模型無差異」在 SQLite 與 SQL Server 皆通過，`tests/architecture/test_user_foreign_keys.py` 與 `test_account_erasure_coverage.py` 仍通過（新表不指向使用者或對話）
- [ ] 1.2 `app/config.py`：`AlertSettings(recipients)` 與解析 `ALERT_EMAIL_TO`（逗號分隔、去除空白與重複、至多 5 個、單純地址、拒絕顯示名稱與換行）；設定了收件人就必須解析 `MAIL_*`（信箱驗證關閉時也一樣）；staging 與 production 沒有收件人啟動失敗（訊息只列變數名稱、不回顯值）；`.env.example` 補上說明；`tests/support/config_env.py` 的 `production_like_env` 補上假的收件人。驗證：`tests/test_config_alerts.py` 涵蓋每個情境（缺收件人、開發環境可省略、缺寄信設定、多個與重複、第六個、顯示名稱與換行、不回顯值）、`tests/test_env_example.py` 通過（每個讀取的變數都列在範本）、既有的設定與啟動測試全數仍通過

## 2. 規則（純函式）

- [ ] 2.1 新增 `app/services/alert_rules.py`：常數（80%／100%、10%、5 分鐘、15 分鐘、10,000 毫秒、5 個鎖定帳號、樣本下限 10 與 20、提醒間隔 6 小時）與五個規則函式（輸入數字、輸出「正常／警告／嚴重＋數值＋門檻」或 Hold），以及 `decide(previous, evaluation, now)`（回傳 `none`、`firing`、`escalate`、`remind`、`resolve`）。驗證：`tests/test_alert_rules.py` 以表格涵蓋每個邊界（79.9%／80%／99.9%／100%、剛好 10% 與超過、樣本 9／10 與 19／20、p95 的最近排名、鎖定 4／5、剛好 6 小時前後）、每個轉換（進入、升級、持續、提醒、恢復、降級不通知、Hold 維持原樣）

## 3. 資料存取

- [ ] 3.1 `LlmCallLogRepository` 新增唯讀查詢：`chat_status_counts(start, end)`（各結果狀態的筆數）與 `chat_latency_percentile(start, end, percentile)`（`COUNT` 後以 `ORDER BY … OFFSET` 取最近排名的那一列，只讀一列，回傳樣本數與值）；`UserRepository` 新增 `count_locked(now)`。新增 `app/repositories/alert_state_repository.py`：`get_all`、`ensure`（以 SAVEPOINT 處理並行建立）、`claim(key, expected, new, now)`（條件式 UPDATE，回傳是否搶到）、`restore(key, claimed_at, previous)`、`touch(key, value, now)`。驗證：`tests/test_alert_queries.py`（窗口邊界、結果狀態分類、p95 與手算相同、空資料、鎖定到期不計）與 `tests/test_alert_state_repository.py`（並行搶佔只有一個成功，使用 `concurrent_engine`；還原只在 `last_notified_at` 仍是自己搶到的值時成立；並行 `ensure`），SQLite 與 SQL Server 皆通過

## 4. 通知內容、事件與服務

- [ ] 4.1 新增 `app/services/alert_messages.py`（五項告警的名稱與處理建議、四種信件：進入、升級、提醒、已恢復；常數加 `str.format`、台灣時間、只有數字）與 `app/observability/alert_events.py`（四個事件，告警代碼與嚴重度為固定列舉，列舉外立刻出錯；`severity` 加入日誌白名單）。驗證：`tests/test_alert_messages.py`（繁體中文、標題與內文含名稱與數值與門檻與台灣時間、沒有任何使用者資料欄位、收件人地址不在內文）與 `tests/test_alert_events.py`（欄位白名單、列舉外的值被拒絕、預覽不記事件）
- [ ] 4.2 新增 `app/services/alert_service.py`：評估五項（各自 `try`、失敗回復該項並記 `alert.check_failed`）、讀取與建立狀態、依 `decide` 搶佔、提交後逐一寄信（至少一位成功即維持已通知；全部失敗則還原並記 `alert.delivery_failed`）、沒有收件人時只評估不改狀態、`dry_run` 不寄信不寫狀態不記事件；`registry.py` 組裝。驗證：`tests/test_alert_service.py`（進入只寄一封、持續三次檢查不再寄、升級、剛好 6 小時的提醒、恢復、降級不寄、寄信全部失敗後下次重試、部分失敗不重寄、並行兩個服務只有一個寄出、單項查詢失敗不影響其餘、Hold 維持原樣、沒有收件人不改狀態、預覽什麼都不改），全部使用假寄信器、假時鐘與真的 Repository

## 5. 指令

- [ ] 5.1 `flask check-alerts [--dry-run]`：印出每項告警的名稱、狀態與數值（沒有任何個資），沒有收件人時說明原因；寄信失敗或某項評估失敗時以非零代碼結束並標明項目。驗證：`tests/test_cli_check_alerts.py`（預覽不寄信不改狀態、重複執行第二次不寄信、失敗類別的結束代碼、輸出與日誌不含收件人與機密）；指令清單測試涵蓋新指令
- [ ] 5.2 端到端：以假模型供應商、假寄信器與手動推進的時鐘走完「正常 → 用量達 80%（寄信）→ 持續（不寄）→ 達 100%（升級）→ 模型錯誤率與設定錯誤同時成立 → 5 個帳號被鎖定 → 全部恢復（各寄已恢復）→ 寄信失敗後重試」，並掃描日誌與指令輸出。驗證：`tests/test_alerting_flow.py` 通過，且整段流程找不到任何收件人地址、寄信密碼與使用者資料

## 6. 升級、驗收與文件

- [ ] 6.1 本機開發資料庫 `project`：備份（`COPY_ONLY, CHECKSUM`，`RESTORE VERIFYONLY` 通過；備份必須讀完所有結果集再確認檔案存在）後 `alembic upgrade head` 到 `0009`，確認資料表、既有資料筆數不變、`/readyz` 回 200。以本機的郵件測試信箱（見 README「本機測試信箱驗證」）實際執行一次 `flask check-alerts`，把某項條件人為製造（例如暫時把用量上限調低），確認收到信、恢復後再收到「已恢復」。驗證：`alembic current` 為 `0009`，`flask check-alerts --dry-run` 與正式執行的輸出符合預期，收到的信件繁體中文不亂碼
- [ ] 6.2 全部測試通過（`pytest`、`node --test tests/js`）；README 新增「營運告警（C-4d）」章節（指令、五項條件與門檻表、設定、已知限制、驗收紀錄）、`.env` 設定說明。驗證：`pytest tests/test_readme.py` 通過（含新章節的指令與門檻與程式常數逐一比對）
- [ ] 6.3 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`營運與部署-operations.md`（告警條件一覽：本指令的五項與平台負責的三項、指令、事件、排程與相依；`usage.global_budget_warning` 不再是通知來源）、`資料模型-data-model.md`（`alert_states` 與 migration `0009`）、`系統架構-architecture.md`（模組對照）、`路線圖-roadmap.md`（C-4d 狀態）；並讓 `tests/test_design_docs.py` 通過

## Workflow follow-up

- 實作完成後以 `/openspec-archive-change ops-alerting` 同步主規格並封存（新增 `operational-alerts`，補充 `app-configuration`、`service-observability`）。
- C-5 `production-launch` 才設定排程（每 5 分鐘 `check-alerts`、每日 `execute-account-deletions` 與 `purge-expired-data`），以及平台負責的三項告警（5xx、存活、寄信失敗）。
