# Tasks

> **前提（不具備就不要開始）**：①`staging-deployment` 已封存；②使用者有可計費的 Google Cloud 專案，並已安裝、登入 `gcloud`；③使用者看過計價頁並同意 Cloud SQL 的固定月費。本 change 的第 3 組之後會在使用者的專案實際建立資源並**產生費用**：需要使用者在場，每一步執行前確認；不在場就停在上一個已完成的步驟並把狀態記在 README 的驗收紀錄，不得自行繼續。

## 1. 前提與費用確認

- [ ] 1.1 向使用者確認並記錄：Google Cloud 專案識別碼與已啟用計費（以唯讀的 `gcloud billing projects describe` 驗證）、區域、資料庫的版本／規格／儲存容量（使用者看過計價頁的估算並同意固定月費）、`gcloud` 已安裝並登入（`gcloud auth list`）、預備環境專用的 OAuth 用戶端（`staging-deployment` 已建立的那個，授權重新導向 URI 補上雲端服務網址）。驗證：README 的「預備環境前提」小節更新為雲端版本，逐項列出狀態（不含機密）；缺任何一項，後續各組暫停並回報

## 2. 腳本擴充（程式，預覽與測試，不碰雲端）

- [ ] 2.1 `create-admin` 新增 `--password-env NAME`（與 `--password-stdin` 互斥、要求 `--email` 與 `--display-name`、環境變數不存在或為空時只指出變數名稱、去除 BOM、密碼不出現在輸出與日誌）。驗證：新增 `tests/test_cli_create_admin_env.py`——成功建立且密碼以雜湊儲存、變數不存在或為空、與標準輸入互斥、缺信箱或名稱、輸出與日誌掃描找不到密碼；`app/cli.py` 開頭說明與 README 同步
- [ ] 2.2 `scripts/launch_plan.py` 與 `gcp_launch.py`：服務尺寸覆寫（CPU、記憶體、並行數、最小與最大執行個體數，驗證為正整數與合法單位）、機密可「以檔案掛載」（`--set-secrets=/路徑=機密:latest`）、`Step.interactive`、`--only STEP_ID --yes`（單步執行；`--yes` 必須搭配單一 `--only`；不存在的步驟被拒絕並列出可用編號；互動步驟不可用 `--yes` 略過；失敗即停止且不接續）。驗證：擴充 `tests/test_launch_plan.py` 與 `tests/test_gcp_launch_script.py`——覆寫值出現在部署指令且其餘維持預設、不合法的值被拒絕並指出參數名稱、檔案掛載的旗標正確且機密值不出現、互動步驟不擷取也不餵標準輸入、單步執行的所有拒絕情境
- [ ] 2.3 `database` 步驟組：私有服務存取（保留位址範圍與 VPC peering）、建立執行個體（版本／規格／儲存容量／區域必須由呼叫者提供，沒有預設值；需要 `--accept-cost`；無公開 IP、私有網路）、資料庫與專用登入（`--prompt-for-password` 的互動步驟，登入名稱不是 `sa`）、自動備份與時間點還原與保留備份數與**明確的備份位置**、日誌保存 30 天、`DATABASE_URL` 由擷取的私有 IP 與使用者輸入的密碼組成後經標準輸入寫入機密、伺服器 CA 取出存入機密並以檔案掛載、憑證驗證的連線測試工作（失敗即停止）。驗證：新增 `tests/test_launch_plan_database.py`——缺費用確認／缺規格／缺備份位置各被拒絕且不輸出建立指令、預覽標明持續計費、沒有公開 IP、登入不是 `sa` 且各環境名稱不相交、順序在 `build` 之後 `migrate` 之前、已存在的資源被略過或更新、所有步驟沒有密碼出現在參數
- [ ] 2.4 `admin` 步驟組（暫時機密經標準輸入、一次性工作以 `--password-env` 建立管理員、執行後依序刪除工作與機密）與 `loadtest` profile（自己的服務、資料庫與登入、機密與假金鑰，所有名稱帶壓測標記，`APP_ENV` 為 staging，`GEMINI_BASE_URL` 指向假模型伺服器服務；假模型伺服器的建置與部署步驟）。驗證：擴充 `tests/test_launch_plan.py`——`admin` 組密碼不在參數且用完即刪、壓測與預備環境的服務／資料庫／機密／排程名稱集合不相交、壓測服務的金鑰機密不是預備環境的、`GEMINI_BASE_URL` 與 `APP_ENV` 正確
- [ ] 2.5 `restore-drill` 步驟組（複製來源執行個體到名稱帶 `_restore_drill` 的目標、建立複製專用的連線機密與刪除紀錄檔案的機密檔案掛載、在複製上執行工作做預覽／實際／再一次、驗證、清除；不在完整計畫的預設順序；需要費用確認）。驗證：新增 `tests/test_launch_plan_restore.py`——目標名稱必須帶暫存標記且不得與來源相同、對來源只有唯讀或複製來源的操作、不在預設順序、缺費用確認被拒絕、檔案內容不在參數中、清除步驟一定存在
- [ ] 2.6 執行器支援「由部分組成的機密」（`DATABASE_URL`：擷取的私有 IP、登入名稱、資料庫名稱與一次輸入的密碼，密碼 URL 編碼）。驗證：擴充 `tests/test_gcp_launch_script.py`——組出的值只經標準輸入、參數與輸出沒有密碼、密碼含特殊字元時編碼正確、缺少擷取值時失敗並指出名稱
- [ ] 2.7 `loadtest/guard.py` 新增壓測標記檢查、新增 `loadtest/cloud_cpu.py`（向 Cloud Monitoring 取壓測期間該服務 CPU 使用率的第 95 百分位最大值，取不到就是未驗證）、`evaluate.py` 可接收這個數字。驗證：新增 `tests/test_loadtest_cloud.py`——標記檢查（接受壓測服務、拒絕預備環境本身與 production）、以罐頭回應解析 CPU 數字、沒資料／沒權限時為未驗證、輸出只有彙總值；營運文件「負載測試」補上雲端步驟，`pytest tests/test_design_docs.py tests/test_launch_docs.py` 通過

## 3. 預備環境建置（雲端；使用者在場；逐步確認；產生費用）

- [ ] 3.1 準備組：以 `gcp_launch.py secrets` 啟用 API、建立服務帳號、機密容器、寫入機密版本（機密從 `.env.staging` 載入，值不印出）、授權；以唯讀指令驗證。驗證：`gcloud secrets list`、`gcloud iam service-accounts list` 的名稱與計畫一致；任何與計畫不符之處已回寫 `launch_plan.py` 並補測試；README 記錄實際執行的步驟與結果
- [ ] 3.2 資料庫與憑證探測：以 `database` 組建立 Cloud SQL 執行個體、資料庫、登入、備份與日誌保存，隨即執行憑證驗證的連線測試工作。驗證：連線測試工作成功（`SELECT 1` 經加密並驗證憑證）、連線字串型態與採用的做法（design D3 的哪一種）寫回計畫與營運文件；**兩種做法都失敗就停止並回報使用者，不得改用 `TrustServerCertificate=yes`**；備份與時間點還原、保留數、備份位置、日誌保存期限以唯讀指令驗證並記錄
- [ ] 3.3 建置、migration 與部署：`build` → `migrate`（含備份）→ `deploy`。驗證：Cloud Build 成功、migration 工作成功且 `alembic_version` 為最新、服務的 `/healthz` 與 `/readyz` 回 200；記錄映像摘要與修訂名稱
- [ ] 3.4 管理員、冒煙與 Google 登入：以 `admin` 組建立預備環境的首位管理員（密碼用畢即刪），把 `<服務網址>/auth/google/callback` 加進預備環境專用的 OAuth 用戶端、補上 `BASE_URL` 與 Google 設定並重新部署，執行 `staging_smoke.py`，再請使用者以真實 Google 帳號人工驗收。驗證：冒煙腳本全部通過；暫時機密與工作已刪除；Google 登入的人工驗收結果記入 README，使用者不在場則標為未驗證
- [ ] 3.5 排程與平台告警：執行 `schedule` 與 `alerts` 組。驗證：四個排程工作與 Scheduler 存在、`check-alerts` 至少成功執行一次且日誌有 `alert.check_completed`、日誌型指標與六類告警政策與通知管道存在（唯讀列出）；任何旗標或 JSON 的差異已回寫並補測試

## 4. 告警演練、壓測與還原演練（雲端；使用者在場；產生費用）

- [ ] 4.1 告警演練：依 design D8 對六類告警各觸發一次並復原。驗證：每一類在 README 的演練表有觸發方式、觸發時間、收到通知的時間、復原方式與時間；沒收到通知的標為未驗證並寫明推測原因；演練後所有設定（ingress、機密版本、排程）已回到演練前
- [ ] 4.2 壓測環境：建置並部署假模型伺服器與壓測服務（自己的資料庫、機密與假金鑰），對壓測服務執行冒煙腳本（含一則經假模型的對話）。驗證：壓測服務回覆不是降級文案、`GEMINI_BASE_URL` 指向假伺服器、預備環境的服務與資料庫完全沒被動到
- [ ] 4.3 容量量測與調整：從基準設定開始，依 design D7 逐輪階梯式加壓、判定、記錄、調整一個變數。驗證：README 的壓測紀錄表每一輪都有伺服器設定、負載參數與判定結果；最終採用的設定達到通過標準，或明確記錄最佳結果與瓶頸；採用值已寫回 `gunicorn.conf.py` 的預設與計畫的尺寸預設，相關測試更新並通過
- [ ] 4.4 雲端還原演練：以 `restore-drill` 組複製預備環境的執行個體，在複製上重新抹除並驗證，清除。驗證：預覽與實際筆數相同、第二次為 0、已刪除的帳號不存在、複製與演練資源已刪除、複製耗時記入營運文件的還原章節；來源執行個體全程只讀
- [ ] 4.5 清理與費用盤點：刪除所有暫存資源，列出仍在計費的資源（資料庫執行個體、Cloud Run 服務、Scheduler、儲存桶…）。驗證：README 的「仍在計費的資源」清單與 `gcloud` 唯讀列出的結果一致，並註明如何關閉

## 5. 關卡與整體驗證

- [ ] 5.1 更新上線前關卡：「託管資料庫成本」「負載測試」兩項依實際結果與依據更新狀態（沒有依據不得標為已通過），其餘維持；路線圖更新。驗證：`pytest tests/test_launch_docs.py tests/test_design_docs.py tests/test_readme.py` 通過，關卡表每個「已通過」都有日期與紀錄位置
- [ ] 5.2 全量驗證：`pytest` 全量（含 SQL Server，不略過）、`node --test`、`openspec validate cloud-staging-run --strict`。驗證：三者通過，數字填入 README 驗收紀錄與路線圖

## Workflow follow-up

- 檢視後提交，再以 `/openspec-archive-change cloud-staging-run` 同步主規格並封存。
- C-5d（production 資源與對外開放）開始前，使用者需決定：自有網域與信件的 SPF／DKIM／DMARC、法務覆核、模型供應商方案（客服 Gemini、RPG OpenRouter）、入口層 IP 限流是否補做、成年驗證方式（若要開放分級 3）。
