# Tasks

> 分批與交接依 `openspec-batch-workflow`：apply 之前先確認分組。第 1 至 3 組是本機的程式與測試（不花錢、不碰 AWS）；第 4 組起是**只能在使用者在場時**對真實 AWS 執行的步驟，每個計費或不可逆的步驟都要使用者明確確認。

## 1. 計畫模組（純函式，不呼叫任何 AWS 指令）

- [x] 1.1 新增 `scripts/aws_demo_plan.py` 的 `Target`、`Step`、命名與標籤規則、區域守門（只接受 `ap-southeast-2`）、名稱不得含 `production`；`tests/test_aws_demo_plan.py` 驗證：其他區域被拒、名稱守門、每個資源都帶標籤
- [x] 1.2 變數歸類從 `scripts/launch_plan.py` 匯入（`SECRET_VARS`、`PLAIN_VARS`、`NOT_DEPLOYED`、`OBSOLETE_VARS`），產生機密參數名稱清單與一般設定；測試：`.env.example` 的每個變數恰好屬於一類、計畫輸出沒有任何機密值、應用程式單元的參數不含 RDS 主要使用者密碼
- [x] 1.3 資源步驟：預算與 SNS、安全群組（對外只開 80／443，沒有 22）、IAM 角色與執行個體設定檔（讀取權限限本專案的參數路徑）、S3 傳檔儲存貯體、RDS（不公開、加密、備份保留 7 天、只允許主機安全群組）、EC2（IMDSv2 強制、gp3 20 GB、無金鑰對）、Elastic IP、SSM 參數；測試：資源清單只含規格允許的類型、建立 RDS 與 EC2 的步驟在沒有 `--accept-cost` 時不輸出指令、預算步驟有兩個門檻且通知對象沒有預設值
- [x] 1.4 主機檔案產生函式：Caddyfile（`/api/game/*` 分流到 RPG 池、`flush_interval -1`、客服池其餘）、`rac-cs`／`rac-rpg`／`rac-caddy` 單元（回送位址綁定、非 root、`--cap-drop=ALL`、`no-new-privileges`、`Restart=always`、日誌循環）、機密取出腳本（寫到記憶體檔案系統、`0600`）；測試：應用程式埠不綁 `0.0.0.0`、沒有 `TrustServerCertificate`、兩個池使用各自的 gunicorn 設定、Caddyfile 對串流路徑沒有 `encode`
- [x] 1.5 排程產生：把 `SCHEDULED_COMMANDS` 的 cron 運算式轉成 `OnCalendar`（只支援實際用到的三種形式，其他形式丟錯，時區 Asia/Taipei，`Persistent=true`）；測試：與 `SCHEDULED_COMMANDS` 逐項一致、沒有手動類指令、不支援的運算式會失敗
- [x] 1.6 部署、回復與 `teardown` 步驟：快照 → 建置 → 遷移 → 重啟 → 驗證的順序；回復只重啟前一版標籤、沒有降級遷移；`teardown` 只選帶標籤的資源、RDS 刪除要求明確選擇最終快照（沒有預設）；測試：順序、回復內容、`teardown` 的清單範圍
- [x] 1.7 文件：在 `docs/營運與部署-operations.md` 新增「AWS 展示環境」章節的拓撲圖（Mermaid）、元件表與步驟總表（與 `Step` 清單一致）；執行 `tests/test_design_docs.py` 與 `tests/test_aws_demo_plan.py` 通過
- [x] 1.8 `teardown_plan` 的快照處理（design D14）：新增參數 `delete_snapshots`（`None`＝尚未決定，`td-delete-snapshots` 是沒有指令的 `blocked` 佔位；元組＝要刪的部署前快照名單，產生 `td-delete-snapshot-<名稱>` 步驟，排在 `td-wait-rds-deleted` 之後，每步先以標籤查詢把關）；名稱必須是 `<資料庫>-pre-<提交短碼>`，最終快照與其他名稱丟 `PlanError`；`snapshot_name()` 與列出快照的唯讀步驟（`td-list-remaining-snapshots` 沿用，改為也供執行器取得名單）；測試：未決定時沒有刪快照指令、被指定保留的不在名單、最終快照與別的資料庫的快照被拒、保留最終快照時也能刪部署前快照、每個刪除都先以標籤查詢、既有的拆除測試仍通過；營運文件第 8 節（規則與步驟總表）同步；`tests/test_design_docs.py` 與 `tests/test_aws_demo_plan.py` 通過

## 2. 執行器（預設預覽、逐步確認）

- [x] 2.1 新增 `scripts/aws_demo.py`：預設只預覽、`--execute` 逐步確認、`--only STEP --yes` 單步、`--accept-cost`、`--region` 守門；呼叫一律以引數清單，runner 可注入；`tests/test_aws_demo_script.py` 以假 runner 驗證：預設沒有呼叫、拒絕即停止、單步只執行一步、輸出與引數沒有機密值
- [x] 2.2 機密寫入：值來自 `--secrets-file`（本機被忽略的檔案）或隨機產生（`SECRET_KEY`、資料庫密碼），經暫存檔（權限限本人、用後刪除）傳給 `aws ssm put-parameter`；`DATABASE_URL` 由腳本以 URL 編碼組成；測試：值不出現在引數、輸出與例外訊息，暫存檔一定被刪除（含失敗時）
- [x] 2.3 輪詢等待（RDS、快照、SSM 命令）有上限與逾時訊息；DuckDNS 更新用標準函式庫呼叫、權杖來自環境變數、任何輸出與例外都不含權杖或含權杖的網址；測試覆蓋逾時與權杖不外洩
- [x] 2.4 `teardown` 執行：只刪帶標籤的資源、逐步確認；執行器先唯讀列出部署前快照（D14），要求使用者明確回覆要保留的例外（或「沒有例外」），之後才以 `delete_snapshots` 重新產生計畫；沒有回覆就不輸出刪除快照的指令；測試以假 runner 驗證不會刪到未標籤的資源、被指定保留的快照與最終快照不被刪
- [x] 2.5 文件：營運文件補執行器的用法、步驟的人工確認點與 `--accept-cost` 說明；`tests/test_design_docs.py` 通過

## 3. 資料庫初始化腳本

- [x] 3.1 新增 `scripts/aws_demo_bootstrap_db.py`（在主映像內以一次性容器執行）：用主要使用者不存在才建立資料庫與專屬登入並授予擁有者角色，可重複執行；名稱經白名單驗證、密碼從環境變數讀取、不寫入日誌；`tests/` 驗證名稱驗證與語句產生（不連資料庫），並以 `TEST_SQLSERVER_URL` 的本機 SQL Server 做一次冪等整合測試（連線不可得時標為跳過而不是通過）
- [x] 3.2 一次性探測腳本（同一個容器內執行 `SELECT 1`）：使用應用程式的連線設定，輸出只有成功或失敗與錯誤類別，不含密碼；測試驗證輸出不含連線字串

## 4. 雲端執行 A：預算、網路、機密、RDS 與 EC2（使用者在場、費用確認）

- [x] 4.1 前提確認並記錄（不含機密）：`aws sts get-caller-identity` 的帳號、區域 `ap-southeast-2`、通知信箱、DuckDNS 權杖已放入環境變數、`--secrets-file` 內容齊全；使用者看過 D12 的費用估算並明確同意 `--accept-cost`
- [x] 4.2 預覽完整計畫後，逐步執行預算與 SNS、安全群組、IAM 角色、S3 儲存貯體、SSM 參數；每一步以唯讀指令驗證（標籤、規則、政策範圍）並記錄；使用者確認 SNS 訂閱信
- [x] 4.3 建立 RDS（等待可用，期間進行 4.4）；驗證：不公開、加密、備份保留天數、安全群組只有主機來源
- [x] 4.4 建立 EC2 與 Elastic IP、主機初始化（交換檔、docker、機密取出腳本與單元檔放置）；驗證：只能經 SSM 連入、沒有 22、`/run/rac` 為記憶體檔案系統。**主機初始化沒有預先寫成計畫步驟**（使用者 2026-11-16 決定：不先猜，現場撰寫）：在這一步以 SSM 逐步撰寫並確認——建立 `HOST_DIRECTORIES`、2 GB 交換檔、安裝並啟動 docker、取得 RDS 的 CA 套件放到 `RDS_CA_BUNDLE_PATH`、放置 `host_files()` 的檔案（含繁體中文內容，以 base64 傳，不直接放進 `commands` 的 JSON）、`systemctl daemon-reload` 與啟用單元與計時器
- [x] 4.5 發現的 AWS CLI 與主機指令差異全部回寫到 `scripts/aws_demo_plan.py` 並補測試（含 4.4 現場撰寫的主機初始化：寫成 `host-init` 計畫步驟與測試，營運文件第 8 節的步驟總表同步），結果記入 README「驗收紀錄（aws-demo-environment）」

## 5. 映像建置、資料庫初始化、憑證探測與遷移

> 第一次部署**分組執行**（`deploy_plan(groups=…)`，`prepare` 會自動帶上）：5.1 與 5.3 用 `build` 組（5.3 重建，多一層 RDS 的 CA，見 design D7）；5.3 的資料庫初始化與探測用 `db-init` 組（只在第一次部署執行，例行部署不含）；5.4 用 `snapshot`、`migrate` 組；5.5 用 `create-admin` 組（乾跑、人工步驟、清理；只在第一次部署，例行部署不含）；`restart`、`verify` 組**不在本組執行**，留到 5.5 `create-admin` 之後與 6.1 一起。資料庫初始化與探測要在含 RDS CA 的主映像內以一次性容器執行，所以映像（5.3 重建）先於它們；例行的後續部署則一次跑完整份計畫（快照 → 建置 → 遷移 → 重啟 → 驗證）。
>
> **2026-11-17 修訂**：原 5.2 的候選 A（`ServerCertificate`）在真實主機實測失敗，查證是原理性不相容（design D7）；5.2 因此改為本機實作的憑證方案修訂，原本的資料庫初始化與探測移到 5.3（編號不變動，5.4 以後不受影響）。5.1 建出的 `rac-app:5681983` 沒有 RDS CA，不可用於資料庫連線，但它的建置與非 root 檢查仍有效。

- [x] 5.1 在主機上建置映像（`deploy_plan` 的 `build` 組：`git archive <提交短碼>` 經 S3、標記提交短碼），確認映像內沒有 `.env`、以非 root 執行
- [x] 5.2 憑證方案實作（design D7 修訂；**本機的程式、測試與文件，不碰 AWS**；共用的 `Dockerfile`、`app/`、`gunicorn*.conf.py`、migration 與 AGENTS.md 不動）：①`scripts/aws_demo_plan.py`：`build-image` 改為先建基底映像 `rac-app-base:<標籤>`，再以計畫模組函式產生的衍生 Dockerfile（只含 `FROM`、`USER root`、`COPY` 主機上的 RDS CA 套件、`update-ca-certificates`、`USER app`）從主機的 `/etc/rac/rds-ca-bundle.pem` 建成 `rac-app:<標籤>`；`check-image` 維持 `10001 clean`，並加上「憑證庫的憑證數＝基底映像的憑證數＋套件的憑證數」的檢查；`_app_run_args` 不再掛載 CA 套件，`RDS_CA_BUNDLE_PATH` 只留給主機初始化 B 段與建置使用；②`scripts/aws_demo.py` 的 `compose_database_url` 只留 `Encrypt=yes`（去掉 `ServerCertificate`）；既有的 SSM `DATABASE_URL` 仍帶舊值，新增覆寫的計畫步驟（值不印出，驗證只看名稱與型別，需使用者確認）；③`scripts/aws_demo_bootstrap_db.py` 與 `scripts/aws_demo_probe_db.py`：去掉 `ServerCertificate`、`--ca-bundle`、`DEFAULT_CA_BUNDLE`、`validate_ca_bundle` 與 CA 檔存在檢查（兩支都只留 `Encrypt=yes`）；探測的守門改為：`TrustServerCertificate` 為真、`Encrypt` 關閉、或連線字串含 `ServerCertificate` 都拒絕（結束代碼 2，不連線）；保留錯誤分類與不輸出例外訊息的規則；④測試同步（`tests/test_aws_demo_plan.py`、`test_aws_demo_script.py`、`test_aws_demo_bootstrap_db.py`、`test_aws_demo_probe_db.py`），並新增：連線字串不含 `ServerCertificate` 與 `TrustServerCertificate`、衍生 Dockerfile 只含允許的指令且不含機密、容器沒有 CA 掛載、既有 SSM 參數的覆寫步驟不印值；⑤文件：營運文件第 8 節（步驟總表、憑證方案、已否決的候選 A 與原因）、README「驗收紀錄」的相關「未驗證」句、其他受影響的設計文件同步；`tests/test_design_docs.py`、aws-demo 相關測試與 `openspec validate aws-demo-environment --strict` 通過；提交之後才做 5.3
- [x] 5.3 （雲端，使用者在場；5.2 提交之後）以新提交的短碼重跑 `build` 組（基底層加 RDS CA 衍生層），確認 `10001 clean` 與「憑證庫＝基底＋RDS 套件」；覆寫 SSM `DATABASE_URL`；執行資料庫初始化（3.1），再執行憑證探測（3.2）；**成功才繼續**。失敗（`tls_certificate` 或其他）即停止並回報使用者，**不退讓**到 `TrustServerCertificate=yes`、`ServerCertificate` 或 `SSL_CERT_FILE`。**回寫**：這兩支腳本沒有計畫步驟（它們不是執行器的子命令），現場在主機上執行並逐項驗證後，把實際用過的指令回寫成計畫步驟與測試（腳本經傳檔儲存貯體的 `ops/` 前綴放到主機的暫存目錄、比對 sha256、環境檔取出、一行輸出的驗證，同 `host-init` 的做法；2026-11-17 嘗試過的現場做法在 HANDOFF.md），營運文件第 8 節的步驟總表同步；結果（採用的連線字串型態，不含密碼）記入營運文件與 README「驗收紀錄（aws-demo-environment）」
- [x] 5.4 建立 RDS 快照，執行 `alembic upgrade head`，驗證 `alembic current` 為最新版（`0018`），再執行 `seed-scripts`（`deploy_plan` 的 `snapshot`、`migrate` 組）；任何一步失敗即停止，不啟動服務
- [x] 5.5 互動步驟：使用者在 SSM 終端機執行 `create-admin`（密碼自己輸入，不經腳本與對話）

## 6. 服務、入口與網域

> **2026-11-17 補充（第 2 批，本機，不新增任務編號，同 5.2 的做法）**：`deploy_plan` 新增兩個只在第一次部署以 `--group` 明確執行的組（例行部署不含；排在 `verify` 之後）：`ingress`（`start-ingress`、`check-ingress`，對應 6.2：啟動 Caddy，在主機內以 `--resolve` 連回送位址、仍經 Caddy，驗證憑證、HTTP/2、HTTP 導向 HTTPS 與 HSTS／四個安全標頭）與 `schedule`（`start-timers`、`run-check-alerts`、`check-schedule`，對應 7.1：啟動五個計時器、手動跑一次 `check-alerts`、核對計時器清單與 `systemctl --failed`）。6.4 另寫 `scripts/aws_demo_probe_stream.py`（只用標準函式庫；必須加 `--allow-model-call`；開一個測試存檔並量測第 0 幕串流各事件的到達時間，判斷入口有沒有緩衝）。這三項在本機以嚴格的假指令與本機假伺服器測試；雲端執行後的差異回寫成計畫步驟與測試。**第 3 批（2026-11-17，雲端）**：`ingress` 組已對真實主機執行並通過（第一次就成功，沒有需要回寫的差異），6.2 勾選；6.3 的分流驗證在現場找到依據（暫停客服池：兩個池跑同一份應用程式，回應本身分不出誰處理，只有「沒人在聽」的池能區分）後回寫成第三個第一次部署專用的組 `routing`（`check-routing`，一定恢復客服池），對真實主機執行通過，6.3 勾選。`schedule` 組與探測腳本尚未對真實主機執行，所以 6.4、7.1 仍未勾選。**第 4 批（2026-11-17，雲端）**：探測腳本已對真實入口 `https://rac001.duckdns.org` 與真實模型執行（測試玩家 cookie 由本機一次性執行器經 SSM 在 `rac-rpg` 容器內簽出，只存在於執行器的記憶體）：第一次執行因免費模型供應商端塞車（開存檔的主角審查 30 秒逾時）回 503，與入口無關；模型恢復後第二次 7 項通過、1 項未驗證：**第一個事件 0.78 秒、`done` 6.11 秒，入口沒有緩衝串流**；**心跳沒有驗證**（`PING_SECONDS` 是程式常數，模型持續有輸出時不會觸發 `ping`），6.4 在這個限度下勾選。`schedule` 組仍未對真實主機執行，7.1 仍未勾選。

- [x] 6.1 在 5.5 之後啟動客服池與 RPG 池（`rac-cs`、`rac-rpg`；`deploy_plan` 的 `restart`、`verify` 組），驗證兩者都以非 root、各用自己的 gunicorn 設定、只綁回送位址；記錄兩個池的常駐記憶體與 `free -m`
- [x] 6.2 把 `rac001.duckdns.org` 指向 Elastic IP（執行器的 DuckDNS 步驟，使用者確認），啟動 Caddy 並確認憑證簽發、HTTP/2、HTTP 導向 HTTPS；驗證應用程式的 HSTS 與安全標頭經入口仍在
- [x] 6.3 驗證分流：`/api/game/…` 由 RPG 池處理、其餘由客服池；停止客服池時 RPG 池的 `/healthz` 仍正常；從主機外部直連應用程式埠被拒絕
- [x] 6.4 驗證串流不被緩衝：用 `scripts/acceptance_rpg_turn.py` 的串流情境（或等效的最小請求）經入口量測第一個事件與心跳的到達時間；若入口緩衝，修正 Caddy 設定並補測試（記錄 `scripts/acceptance_rpg_turn.py` 只接受本機位址，需要另寫最小的串流探測或在主機內對回送位址執行並記錄限制）。**限度：第一個事件的到達時間已量（入口沒有緩衝）；心跳沒有觸發、未驗證**（見第 6 組開頭的補充）
- [ ] 6.5 文件：營運文件補入口設定、憑證與分流的實際結果；更新 `docs/營運與部署-operations.md` 中「`/api/game/` 分流待部署」的敘述與缺口表；`tests/test_design_docs.py` 通過

## 7. 排程與有限的監控

- [ ] 7.1 安裝排程單元（1.5 產生），驗證 `systemctl list-timers` 與清單一致、手動執行一次 `check-alerts` 成功；`systemctl --failed` 為空
- [ ] 7.2 建立 EC2 狀態檢查告警；使用者確認 SNS 訂閱；以 `aws cloudwatch set-alarm-state` 觸發一次並確認信件送達（演練後恢復狀態）
- [ ] 7.3 告警信驗證：在展示環境製造一項告警條件（`.invalid` 測試帳號）並執行 `check-alerts`，由使用者確認信件送達、是否進垃圾郵件匣；沒收到則標為未驗證並記錄檢查過的地方；驗證後清除測試資料
- [ ] 7.4 在營運文件列出展示環境未達 production 的缺口（5xx、外部探測、寄信失敗與排程失敗的平台告警、備份保留 30 天與跨區域、日誌保存 30 天），每項附原因與重新評估時機

## 8. 驗收

- [ ] 8.1 `scripts/staging_smoke.py https://rac001.duckdns.org` 全部通過（模型呼叫略過）；需要登入的項目由使用者在自己的 SSM 終端機以 `scripts/mint_test_sessions.py` 簽出 cookie 後提供（不經對話貼出明文時標為「未驗證」，不算通過）；驗收後清除測試玩家
- [ ] 8.2 Google 登入人工驗收（使用者本人）：把 `https://rac001.duckdns.org/auth/google/callback` 加入預備環境專用的 OAuth 用戶端，驗證新使用者未勾選條款不建立帳號、勾選後完成年齡確認、回訪一鍵登入、管理員 Google 加密碼；Google 不接受該網域時標為未通過並回報，不放寬任何檢查
- [ ] 8.3 小規模真實模型連通性驗證：以 `scripts/acceptance_rpg_real_model.py` 的做法（或手動）完成一次劇本開存檔與一回合、一次客服對話；只記錄是否成功與生成時間，不當容量依據；確認一般與成人模式沒有使用免費模型的設定
- [ ] 8.4 在 README 新增「驗收紀錄（aws-demo-environment）」：分欄列出已驗證與未驗證項目、日期、資源清單與仍在計費的項目、每日費用觀察；確認 `GET /readyz` 不回傳內部細節

## 9. 收尾

- [ ] 9.1 依 `docs/README.md` 的對照表更新受影響的設計文件（營運與部署、路線圖：新增本 change 並說明 RC-4 拆成兩個 change、`rpg-ops-loadtest` 以本環境為量測基礎），複查各文件標頭的「對應 change」，並執行 `tests/test_design_docs.py` 通過
- [ ] 9.2 全套測試通過（`pytest` 與 Node 測試），確認沒有修改 `app/`、`Dockerfile`、`gunicorn*.conf.py`、migration 與 AGENTS.md（憑證方案以衍生映像層實作，不改這些，見 design D7）

## Workflow follow-up

- 封存前請使用者核對 README 的驗收紀錄與仍在計費的資源；封存後更新路線圖的狀態與日期。
- 展示期結束（2026-12-31）後執行 `teardown`，或決定保留環境給 `rpg-ops-loadtest`（design 的 Open Questions）。
- 每個階段結束更新 HANDOFF.md（不進 commit）。
