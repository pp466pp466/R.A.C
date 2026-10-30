# Tasks

## 1. 安全標頭

- [ ] 1.1 新增 `app/security/security_headers.py` 並在 `app/__init__.py` 註冊：每個回應加上 `X-Content-Type-Options`、`Referrer-Policy`、`X-Frame-Options`、`Permissions-Policy`，`cfg.trust_proxy` 為真時再加 HSTS（`max-age=31536000`，無 `includeSubDomains`／`preload`）；已存在的同名標頭不覆蓋。驗證：新增 `tests/test_security_headers.py`——`production` 與 `staging` 的頁面、API、靜態檔、404、被 CSRF 拒絕、真的觸發的 500 都帶五個標頭；`development` 與 `testing` 沒有 HSTS 但有其餘四個；端點自訂的 `Referrer-Policy` 不被覆蓋；HSTS 值不含 `includeSubDomains` 與 `preload`；回應沒有 `Content-Security-Policy`
- [ ] 1.2 營運文件新增「傳輸安全」小節：標頭清單與原因、HSTS 為何只設一年且不含子網域與預載入、**不做 CSP 的原因與重新評估的時機**；同步 `docs/系統架構-architecture.md` 的模組表。驗證：`pytest tests/test_design_docs.py tests/test_security_headers.py` 通過，且一個測試斷言營運文件含「Content-Security-Policy」的說明

## 2. 排程指令清單與心跳事件

- [ ] 2.1 在 `app/cli.py` 宣告 `SCHEDULED_COMMANDS`（`check-alerts`、`execute-account-deletions`、`purge-expired-data`、`purge-inactive-accounts`，各附頻率）。驗證：一致性測試比對 Click 註冊的指令——凡是不屬於「人工操作」集合（`llm-probe`、`create-admin`、`age-stats`、`replay-deletions`）的指令必須在 `SCHEDULED_COMMANDS` 中，新增指令而兩邊都沒登記則測試失敗
- [ ] 2.2 新增 `alert.check_completed` 事件（`app/observability/alert_events.py`），`check-alerts` 在非預覽、非唯讀且沒有評估失敗時記錄。驗證：擴充 `tests/test_cli_check_alerts.py`——成功一次有一筆且只有事件名稱欄位；預覽、沒有收件人、某項評估失敗時沒有；`tests/test_alerting_flow.py` 的日誌掃描仍通過
- [ ] 2.3 營運文件的事件清單與指令表補上 `alert.check_completed` 與「排程／人工」分類。驗證：`pytest tests/test_design_docs.py` 通過

## 3. 部署計畫腳本

- [ ] 3.1 新增 `scripts/launch_plan.py`（純函式，回傳步驟資料）：機密／一般設定／不部署三類的環境變數表、備份、建置映像、migration Job、部署服務、四個排程 Job 與 Scheduler 的步驟，資源名稱帶環境名稱，建立機密的值以 `stdin_from` 參照而非參數。驗證：新增 `tests/test_launch_plan.py`——`.env.example` 的每個變數恰好屬於一類；機密只以名稱出現；`TEST_SQLSERVER_URL` 與 `FLASK_DEBUG` 不部署；步驟順序為備份 < migration < 部署；`staging` 與 `production` 的資源名稱集合不相交；排程清單等於 `SCHEDULED_COMMANDS` 且 `check-alerts` 為 `*/5 * * * *`
- [ ] 3.2 新增 `scripts/gcp_launch.py`：子命令 `plan`／`secrets`／`build`／`migrate`／`deploy`／`schedule`／`alerts`，預設只印出指令，`--execute` 才以清單參數逐步確認執行，專案與區域必須明確提供，migration 失敗即停止。驗證：新增 `tests/test_gcp_launch_script.py`——預設預覽不呼叫任何程序（以假的 runner 斷言零次呼叫）；缺專案以非零代碼結束；`--execute` 時拒絕確認會停止且不執行後續；migration 步驟失敗後不執行部署；輸出不含任何測試用的機密字串
- [ ] 3.3 營運文件新增「部署與回復」：以腳本的子命令寫出備份 → migration → 部署 → 驗證，回復為映像回到前一版（migration 為 expand-only 不回復結構）；README 補腳本用法。驗證：`tests/test_launch_docs.py`（見 8.1）會檢查引用的腳本與子命令存在；`pytest tests/test_design_docs.py tests/test_readme.py` 通過

## 4. 平台告警定義

- [ ] 4.1 在 `scripts/launch_plan.py` 加入平台告警的資料結構與 `alerts` 子命令的產生：5xx 比率（5 分鐘、5%、請求數下限 20）、`/healthz` 與 `/readyz` 探測（間隔 1 分鐘、連續 3 次）、`mail.send_failed`（5 分鐘 3 次）、排程工作失敗與 `check-alerts` 15 分鐘無心跳；通知管道只有電子郵件，收件人由 `--notify-email` 提供。驗證：新增 `tests/test_platform_alerts.py`——每項的門檻與窗口與規格一致；沒有提供收件人時拒絕；定義中沒有預設收件人；日誌型指標的篩選字串引用的事件名稱（`mail.send_failed`、`alert.check_completed`）在程式碼中真實存在
- [ ] 4.2 營運文件的「平台告警」表改為由此結構驗證：新增測試比對文件表格的門檻與窗口和腳本產生的定義相同。驗證：`pytest tests/test_platform_alerts.py tests/test_design_docs.py` 通過；README 的 C-4d 章節對平台三項的描述更新為指向腳本

## 5. 還原後重新抹除

- [ ] 5.1 `UserRepository` 新增條件式刪除 `delete_if_created_not_after(user_id, deleted_at)`（ORM 表達式：編號、建立時間不晚於、角色非管理員）。驗證：擴充 `tests/test_user_repository*.py`——建立時間早於刪除時間者被刪；晚於者不刪；管理員不刪；回傳是否真的刪到；SQL Server 同樣通過
- [ ] 5.2 新增 `app/services/deletion_replay_service.py`（資料庫紀錄 ∪ 檔案紀錄、先驗證檔案再抹除、走 `AccountErasureService.erase`、已有相同編號較新紀錄時不重複寫 tombstone、回傳抹除／略過（編號已被重新發出）／不存在的筆數）與 `app/observability/lifecycle_events.py` 的 `account.deletion_replayed`。驗證：新增 `tests/test_deletion_replay_service.py`——復活的帳號被抹除並匿名化帳本；重複執行第二次為 0；新帳號沿用舊編號時略過；管理員不抹除；檔案第 3 行格式錯誤時在任何抹除之前失敗且訊息只有行號；檔案內紀錄抹除成功後新增 tombstone；預覽不改任何資料也不記事件
- [ ] 5.3 新增 `flask replay-deletions [--dry-run] [--ids-file 檔案]`（檔案以 `utf-8-sig` 讀取，輸出只有筆數）並登記為人工操作指令。驗證：新增 `tests/test_cli_replay_deletions.py`——輸出只有筆數、結束代碼、檔案不存在、格式錯誤；擴充 `tests/` 的日誌掃描流程（走完還原、預覽、實際抹除後）找不到 email、名稱、生日、檔案內容
- [ ] 5.4 文件：營運文件新增「資料庫還原」小節（順序：還原 → 結構檢查 → 取出日誌中的刪除紀錄 → `replay-deletions --dry-run` → 實際執行 → 驗證 → 開放流量）與日誌取出步驟；`docs/資料模型-data-model.md` 的 tombstone 說明補上「重新發出編號」的防護；README 補指令。驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過，且 `tests/test_launch_docs.py`（8.1）檢查還原順序

## 6. 還原演練腳本

- [ ] 6.1 新增 `scripts/restore_drill.py`：把備份檔還原成 `<原名>_restore_drill`、比對 `alembic` 版本、依序預覽與實際執行 `replay-deletions`、檢查已刪除者不存在、只輸出筆數與通過／失敗、預設結束後刪除暫存資料庫；目標名稱不符 `*_restore_drill` 一律拒絕；附一個把日誌 JSON 轉成「編號,時間」檔案的子命令（只取這兩個欄位）。驗證：新增 `tests/test_restore_drill.py`——拒絕非暫存名稱（不論參數怎麼組合）、日誌轉檔只輸出編號與時間並丟棄其他欄位、輸出不含使用者資料；架構測試確認腳本不在 `app/` 之內
- [ ] 6.2 在本機 SQL Server 實跑一次演練：以暫存資料庫 `project_test` 建立含「後來被刪除的帳號」的備份，執行 `scripts/restore_drill.py`，確認通過。驗證：把結果（筆數、通過、耗時）記入 README「驗收紀錄」；演練後 `project_test_restore_drill` 已清除，`project` 資料庫完全沒有被動到（前後筆數相同）

## 7. 負載測試

- [ ] 7.1 新增 `loadtest/fake_app.py`（以假模型注入 `create_app`，真的睡眠、延遲與各類錯誤率可由環境變數設定，`staging` 與 `production` 啟動即失敗）、`requirements-dev.txt` 加入 Locust（僅開發用），`.dockerignore` 排除 `loadtest/`。驗證：新增 `tests/test_loadtest_harness.py`——`APP_ENV=production`／`staging` 載入入口會失敗；錯誤率設定生效（固定亂數種子下比例落在預期範圍）；`Dockerfile` 沒有複製 `loadtest/`、`.dockerignore` 排除它；`requirements.txt` 沒有 Locust
- [ ] 7.2 新增 `loadtest/locustfile.py`（註冊含年齡確認與條款、登入並取得 CSRF 權杖、開對話、`between(30, 45)` 送訊息、混入列表與 `since_seq`，信箱使用保留網域；預設只對本機執行、其他位址需確認參數、拒絕文件所列的正式網域）。驗證：`tests/test_loadtest_harness.py` 以 Flask 測試客戶端驅動同一組動作函式，確認一位虛擬使用者的流程可完成；位址守門測試涵蓋本機、其他位址無確認、正式網域
- [ ] 7.3 新增 `loadtest/evaluate.py`（讀取 Locust 的 CSV，p95 小於 8 秒、錯誤率小於 1%、日誌無資料庫鎖定特徵、CPU 由參數提供否則「未驗證」，只輸出彙總）。驗證：新增 `tests/test_loadtest_evaluate.py`——全達標、p95 超過、錯誤率超過、鎖定錯誤、CPU 未提供不判通過、輸出無使用者資料（表格式測試）
- [ ] 7.4 本機冒煙執行：假模型入口加 Locust 20 位虛擬使用者跑 1 分鐘，再以 `evaluate.py` 判定。驗證：測試流程跑通、錯誤率與延遲設定在結果中可見；README 記下結果並標明「本機結果不是容量依據，gunicorn 參數留待 C-5b」；營運文件新增「負載測試」小節（用法、通過標準、環境限制）

## 8. 營運手冊與上線前關卡

- [ ] 8.1 營運文件補齊「機密輪替」「事故處理」「上線前關卡」「未滿足的規範項目」四節（關卡八項各有負責人、狀態、依據；未滿足項目至少含 IP 限流、CSP、RPG 獨立 worker 池，各載明原因、風險、重新評估時機）。驗證：新增 `tests/test_launch_docs.py`——手冊引用的每個 `flask --app wsgi <指令>` 與 `scripts/*.py` 都存在；八項關卡齊全且負責人與狀態非空、狀態為已通過時依據非空；未滿足項目含 IP 限流；還原順序正確
- [ ] 8.2 把 report.md 的 4.2「上線前關卡」與路線圖對照一次，確認沒有關卡漏列。驗證：`pytest tests/test_launch_docs.py tests/test_design_docs.py tests/test_readme.py` 通過

## 9. 整體驗證與收尾

- [ ] 9.1 全部驗證：`pytest` 全量（含 SQL Server，不略過）、`node --test` 前端測試、`openspec validate launch-readiness --strict`。驗證：三者皆通過，數字填入 README「驗收紀錄」與路線圖
- [ ] 9.2 同步路線圖：C-5 拆為 C-5a（本 change，標示「實作完成，待檢視與封存」）與 C-5b（預備環境、壓測、還原演練、正式上線，標示規劃中並列出需要使用者提供的東西：Google Cloud 專案與計費、網域、寄信服務、法務覆核、Gemini 方案決定），並更新相關文件標頭的「對應 change」。驗證：`pytest tests/test_design_docs.py` 通過；路線圖節點與表格一致

## Workflow follow-up

- 檢視後提交，再以 `/openspec-archive-change launch-readiness` 同步主規格並封存。
- C-5b 開始前，使用者需決定：Google Cloud 專案與計費、Cloud SQL 費用（N-9）、網域、寄信服務、入口層 IP 限流是否補做。
