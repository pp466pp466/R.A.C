# Design

## Context

現況（已查證）：`Dockerfile` 與 `gunicorn.conf.py` 已存在，`scripts/verify_container.py` 驗收映像；`app/__init__.py` 只在 `trust_proxy` 時掛 ProxyFix，沒有任何安全標頭，`app/security/cache_headers.py` 已示範 `after_request` 掛鉤的慣例；`create_app(..., llm_provider=...)` 可注入模型供應商；`deletion_tombstones` 只存使用者編號、時間與原因，而 `account.deleted_by_request` 與 `account.inactive_purged` 兩個日誌事件帶內部使用者編號。專案沒有任何雲端部署檔、沒有 Locust、沒有重新抹除的指令。動機與範圍見 proposal.md；C-5 依使用者決定拆成 C-5a（本 change，程式庫內）與 C-5b（雲端實作），所以本 change **沒有任何一步會碰到雲端帳號**。

## Goals / Non-Goals

**Goals:**
- 每一項腳本與指令都能在沒有 `gcloud`、沒有雲端專案的機器上以測試驗證其「輸出與決策」。
- 把「文件裡的一句話」變成「會因漂移而失敗的測試」：環境變數歸類、排程清單、平台告警門檻、營運手冊引用的指令。
- 還原後重新抹除是**唯一**會刪資料的新功能，以條件式刪除與演練守住。

**Non-Goals:**
- 在 Google Cloud 實際建立資源、壓測預備環境、調整 gunicorn（C-5b）。
- IP 限流（使用者決定先不做）、CSP、RPG 專用 worker 池。
- 宣告式的基礎設施工具（Terraform）。

## Decisions

### D1：安全標頭放在 `app/security/security_headers.py`，以 `after_request` 掛鉤

沿用 `cache_headers.py` 的慣例。標頭用 `setdefault` 式寫入（已存在則不覆蓋），所以端點自訂的值勝出。`after_request` 對錯誤回應與靜態資源同樣執行，但**未處理的例外**走 Flask 的 500 處理器後仍會經過 `after_request`，因此不另寫例外路徑；測試會以真的觸發 500 驗證。HSTS 是否送出取決於 `cfg.trust_proxy`（只有 staging 與 production 為真），不新增設定項，也就沒有「在 production 忘了開」的情況。

- **HSTS 一年、不含 `includeSubDomains` 與 `preload`**：HSTS 被瀏覽器記住後無法由伺服器撤回；網域尚未取得，不知道旁邊會有哪些子網域。日後要升級是放寬而非撤回。
- **不做 CSP**：`app/static/vendor/vue` 是全域建置，模板在瀏覽器以 `new Function` 編譯，CSP 必須允許 `unsafe-eval`，加上 `style=` 屬性還需 `unsafe-inline`，保護力趨近於零。替代做法是預先編譯模板（等於改前端建置，違反「不使用打包」的規範），不在此範圍。記為已知限制並在前端改版時重新評估。
- **替代方案**：讓 Nginx 或 Cloud Run 前面的層加標頭——否決，Cloud Run 沒有此功能，而 IP 限流已決定不做，加一個前置容器只為標頭不划算；放在應用程式內，行為隨程式一起測試。

### D2：部署計畫是純函式，執行器很薄

`scripts/launch_plan.py` 只回傳資料（`Step(name, argv, stdin_from, ...)` 的清單），**不呼叫任何程序**；`scripts/gcp_launch.py` 是命令列包裝：預設把每個 `argv` 以 `shlex` 風格印出，`--execute` 才以 `subprocess.run(list)` 執行（不經 shell，符合第 8 節「參數以清單傳遞」）。這樣測試只比對資料，不需要 `gcloud`，也不可能在測試中誤建資源。

- **機密分類**：計畫內有一張 `SECRET_VARS`／`PLAIN_VARS`／`NOT_DEPLOYED` 三類表，測試以 `.env.example` 的全部變數比對「恰好一類」。新增環境變數而忘了歸類 → 測試失敗，和既有的 `tests/test_design_docs.py` 檢查同一種漂移。機密：`SECRET_KEY`、`DATABASE_URL`、`GEMINI_API_KEY`、`OPENROUTER_API_KEY`、`MAIL_SMTP_PASSWORD`、`GOOGLE_OAUTH_CLIENT_SECRET`、`SECRET_KEY_FALLBACKS`；`MAIL_SMTP_USER`、`GOOGLE_OAUTH_CLIENT_ID` 當一般設定。不部署：`TEST_SQLSERVER_URL`、`FLASK_DEBUG`。
- **建立機密的值不走命令列**：`gcloud secrets versions add NAME --data-file=-` 從標準輸入讀，腳本以 `getpass` 取得（或 `--from-env` 讀呼叫者環境中同名變數），計畫資料裡只有 `stdin_from="prompt:NAME"` 這種參照。命令列參數會出現在行程列表與 shell 歷史。
- **環境名稱進資源名稱**：`{service}-{env}`，staging 與 production 各自的 Cloud SQL 與機密；測試對兩個環境各產生一次計畫，斷言資源名稱集合不相交。
- **替代方案**：Terraform——使用者已選 Python 加 gcloud；缺點（非宣告式、重複執行要自己保證冪等）以「每個建立步驟先用 `describe` 判斷是否已存在、存在則跳過」緩解，仍記為限制。

### D3：migration 與排程都用 Cloud Run Job，與服務共用映像

migration 工作：`alembic upgrade head`，`--task-timeout` 與 `--max-retries=0`（失敗不自動重試，避免半套 migration 重跑）。計畫順序固定為「備份 → migration → 部署服務 → 驗證」，由測試斷言步驟名稱的相對順序；migration 失敗時執行器停止（沒有「繼續」的選項）。排程用 Cloud Scheduler 觸發 Job 的 `:run`。四個排程類指令的清單由 `app/cli.py` 的標記取得：指令以一個集合 `SCHEDULED_COMMANDS`（名稱）宣告在 `app/cli.py`，計畫從同一個集合產生排程，**一致性測試比對 Click 註冊的指令與該集合**，所以新增一個排程類指令只改一處，忘了登記則測試失敗。

- 時間：`check-alerts` 每 5 分鐘（`*/5 * * * *`），其餘每日凌晨 03:00、03:20、03:40（`Asia/Taipei`），錯開避免同時佔用資料庫連線。
- **不用 Cloud Run 服務本身的內部計時器**（第 11 節：不使用行程內計時器）。

### D4：平台告警以 Cloud Monitoring 的 JSON 定義表達

腳本產生日誌型指標（`mail.send_failed` 計數、5xx 計數與總請求數）、uptime check（`/healthz`、`/readyz`）與 alert policy 的 JSON，再交給 `gcloud monitoring ...`／`gcloud alpha monitoring policies create --policy-from-file`。定義集中在一個資料結構，門檻（5% 與 20 個請求下限、3 次失敗、15 分鐘）與營運文件的表以測試比對（和 C-4d 的 README 與程式常數比對相同做法）。

- **5xx 比率**用「兩個日誌型指標相除」的 MQL／比率條件表達，請求數下限 20 以另一個條件並列（AND），避免低流量誤報。
- **`check-alerts` 停擺偵測**：Cloud Run Job 的最後成功時間沒有現成指標，改在日誌型指標計數「成功的 `check-alerts` 完成事件」並設「15 分鐘內計數為 0」的缺漏條件。為此 `check-alerts` 成功結束時要有一筆可計數的日誌——C-4d 的 `alert.*` 事件只在狀態改變時才記，**不足以當心跳**，所以新增一個極小的 `alert.check_completed` 事件（只有事件名稱，只在非預覽、非唯讀且沒有評估失敗時記錄；見 service-observability 規格）。這是本 change 對 C-4d 程式的唯一回頭修改，已列入任務，並同步 `service-observability` 的測試掃描。
- **通知管道只有電子郵件**：收件人由 `--notify-email` 提供，不寫預設值。

### D5：`replay-deletions` 走既有的單一抹除出口

新增 `DeletionReplayService`，輸入是「(使用者編號, 刪除時間) 的集合」：資料庫的 `deletion_tombstones` ∪ `--ids-file`（每行 `編號,ISO 8601 時間`）。對每個編號呼叫 `AccountErasureService.erase`，傳入新的條件式刪除 `UserRepository.delete_if_created_not_after(user_id, deleted_at)`——一個 `DELETE ... WHERE id = ? AND created_at <= ? AND role != 'admin'`（ORM 表達式，不是原生 SQL）。條件由資料庫在刪除當下判斷，沿用 C-4c「條件式刪除、不成立就整個回復」的模式，所以**不需要先讀再刪**，也沒有檢查與刪除之間的競態。

- **為什麼要比對建立時間**：SQL Server 的 IDENTITY 在還原後從備份時的種子繼續，備份之後註冊再被刪除的使用者的編號，會被還原後的新使用者重新使用。以編號直接刪除會刪到別人。`created_at <= deleted_at` 保證「被刪的帳號在刪除時間之前就存在」，新帳號（建立時間晚於舊的刪除時間）不會被誤傷。此規則的前提是時鐘大致正確；可疑時以預覽先看「略過（編號已被重新發出）」的數字。
- **執行時機**是還原後、對外開放之前：這讓「重新發出編號」的視窗最小，文件載明順序，演練驗證。
- **檔案先整體驗證再動手**：格式錯誤使指令在任何刪除之前失敗，只回報行號（內容可能是任何東西，不回顯）。
- **檔案內的紀錄抹除成功後寫入新的 tombstone**（`erase` 本來就會寫），之後再還原仍涵蓋。資料庫內本來就有的紀錄再抹除一次時，會多寫一筆重複的 tombstone——表本來就不要求唯一（見模型註解），但為避免無限增長，已有相同編號與較新時間的紀錄時不再寫（在服務層判斷，不加約束）。
- **日誌來源**：`gcloud logging read` 取 `account.deleted_by_request`／`account.inactive_purged` 的 `user_id` 與時間；取出步驟寫在營運手冊，並由還原演練腳本提供一個把日誌 JSON 轉成檔案的子命令（只取編號與時間，丟棄其餘欄位）。日誌保存 30 天與備份相同，所以涵蓋範圍一致。
- **替代方案**：把 tombstone 同步到資料庫之外（例如另一個資料庫）——多一個要維運的東西，個資法下仍需保護，否決；點對點還原（PITR）可把還原點設在事故之前，缺口只剩事故之後的刪除，同樣靠日誌補。

### D6：還原演練腳本只在名稱帶 `_restore_drill` 的暫存資料庫上動手

`scripts/restore_drill.py` 以 SQL Server 的 `RESTORE DATABASE ... WITH MOVE` 把備份檔還原成 `<原名>_restore_drill`，**目標名稱不符合 `*_restore_drill` 就拒絕**。這是維運腳本，允許使用原生 T-SQL（AGENTS.md 第 4 節的 ORM 規則適用於業務邏輯與 Repository；先例是 C-4c／C-4d 驗收用的備份與還原腳本）；但腳本不得出現在 `app/` 之內，由架構測試守住。演練流程：還原 → `alembic current` 比對預期版本 → `replay-deletions --dry-run` → 實際執行 → 查詢檢查已刪除者不存在 → 輸出筆數 → 刪除暫存資料庫（可用旗標保留）。

### D7：負載測試入口與判定

`loadtest/fake_app.py` 呼叫 `create_app(load_config(), llm_provider=LoadTestProvider(...))`；供應商實作與 `FakeLLMProvider` 相同的介面，但**真的睡眠**（`time.sleep`）以模擬延遲，延遲與錯誤率由環境變數 `LOADTEST_LATENCY_SECONDS`、`LOADTEST_ERROR_RATES` 設定。模組載入時檢查 `APP_ENV`，`staging` 與 `production` 直接 `raise SystemExit`。`loadtest/` 不在 Dockerfile 的 `COPY` 清單，`.dockerignore` 再排除一次（雙重防線，測試檢查兩者）。

- 情境 `loadtest/locustfile.py`：每位虛擬使用者 `on_start` 註冊（保留網域 `@loadtest.invalid`）並登入，取得 CSRF 權杖（頁面 meta）後，開對話並以 `wait_time = between(30, 45)` 送訊息；混入列表與 `since_seq`。Locust 是**僅開發用**依賴，加進 `requirements-dev.txt`，不進 `requirements.txt` 與映像。
- 判定 `loadtest/evaluate.py`：讀 Locust 的 `--csv` 輸出，p95、失敗率；再掃日誌檔找資料庫鎖定特徵字串；CPU 由 `--cpu-percent` 提供，缺少則「未驗證」。標準是純函式，表格測試。
- **本機結果的解讀**：gunicorn 不支援 Windows，本機只能用 `werkzeug` 的 threaded 伺服器，和正式環境的 gunicorn gthread 不同。**決定**：本機只驗證「測試本身能跑通、判定腳本正確、假模型與錯誤率生效」，結果標示為非容量數據；真正的容量量測放在 C-5b 的預備環境（或容器內的 gunicorn，經 WSL）。

### D8：營運手冊放在既有的營運文件，不新增文件

`docs/營運與部署-operations.md` 新增「部署與回復」「機密輪替」「還原」「事故處理」「上線前關卡」「未滿足的規範項目」六節，保持單一文件，避免 `DESIGN_DOCS` 登記與索引的額外成本。新增的測試（`tests/test_launch_docs.py`）：解析手冊中以 `flask --app wsgi <指令>` 與 `python scripts/<檔>` 出現的引用，逐一確認存在；關卡表逐項檢查負責人與狀態欄位非空，狀態為「已通過」時依據欄不為空；未滿足項目表含 IP 限流。

## Risks / Trade-offs

- **腳本沒有對真實 `gcloud` 驗證過** → 預覽輸出以測試鎖定；C-5b 第一件事就是在預備環境逐步 `--execute`，並把發現的差異回寫到計畫。proposal 與營運文件都明講「未對真實雲端驗證」。
- **`gcloud` 指令與旗標會隨版本變動** → 計畫集中在一個檔案，旗標拼錯只影響該步驟；C-5b 驗證時修正。不在本機 `gcloud` 缺席時猜測輸出格式。
- **IP 限流缺口** → 使用者決定先不做；入口只有帳號層級的鎖定與每分鐘訊息數。風險寫進手冊並列為上線前關卡，不修改 AGENTS.md（規範仍然要求，這是「尚未滿足」而非「規範改了」）。
- **`replay-deletions` 誤刪** → 條件式刪除（建立時間、非管理員）、預覽、演練、只在流量關閉時執行；最壞情況是時鐘錯誤導致少抹除（安全方向）或對極少數編號重用判斷錯誤，因此預覽的「略過」數字要人工看過。
- **心跳事件改動 C-4d 的程式** → 只新增一行事件、無資料欄位；既有掃描測試（`ALLOWED_FIELDS`）會把關。
- **HSTS 難以撤回** → 一年、無子網域、無預載入；網域取得前不會被任何真實使用者的瀏覽器記住（development 與 testing 不送）。
- **本機負載數字不可當容量** → 結果檔與文件標示；容量決策移到 C-5b。

## Migration Plan

沒有資料表變動、沒有 migration。部署：合併後正常部署；新的標頭在 staging／production 立刻生效，development 不受影響。回復：還原到前一版映像即可（標頭與指令都是新增）。`flask replay-deletions` 與腳本不會被任何排程呼叫，只在還原時手動執行。

## Open Questions

- 本機驗收 `replay-deletions` 與 `restore_drill.py` 使用本機 SQL Server 的暫存資料庫（`project_test`）：備份檔放哪個目錄需確認伺服器帳號有寫入權限——實作時先以唯讀查詢確認 `SERVERPROPERTY('InstanceDefaultBackupPath')`，不影響規格。
