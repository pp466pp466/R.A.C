# Tasks

> 實作順序即依賴順序。每組都自帶它需要的測試與文件；最後一組只做跨組整合驗收。
> 全程遵守：機密（含資料庫密碼）只寫入已被 `.gitignore` 排除的 `.env`，不得出現在任何程式碼、設定範本、文件或日誌中。
> 本機沒有 Windows 原生 `docker`：容器指令一律透過 `wsl -d Ubuntu-2204 -u root -- ...`，使用前先執行 `service docker start`。

## 1. 版本控制與專案基礎

- [x] 1.1 執行 `git init`，在本專案層級設定提交者名稱 `HuangMin` 與使用者先前提供的信箱（只寫入 `.git/config`，不寫入任何文件）；建立 `.gitignore`（`.env`、虛擬環境、快取、SQLite 檔、`.claude/settings.local.json`）與 `.gitattributes`（`* text=auto eol=lf`，字型與圖片標為二進位）。驗證：`git check-ignore -v .env` 命中規則；`git check-attr eol -- Dockerfile` 顯示 `lf`；`git config --local user.name` 為 `HuangMin`；`git status` 不列出 `.env`
- [x] 1.2 建立 `.python-version`（3.14）與虛擬環境，撰寫 `requirements.txt`（精確版本：Flask、SQLAlchemy、Alembic、pyodbc、gunicorn、python-dotenv）與 `requirements-dev.txt`（pytest 等）。驗證：`pip install -r requirements-dev.txt` 成功，且 `python -c "import flask, sqlalchemy, alembic, pyodbc, dotenv"` 無錯誤
- [x] 1.3 建立套件骨架（`app/`、`app/db/`、`app/controllers/`、`app/services/`、`app/repositories/`、`app/models/`、`app/observability/`、`app/templates/`、`app/static/`、`tests/`、`wsgi.py`）與 pytest 設定（含 `sqlserver` 標記）。驗證：`pytest --collect-only` 可執行且不報錯
- [x] 1.4 建立 `README.md` 的骨架（繁體中文）：專案簡介、環境需求、安裝步驟、執行與測試指令、WSL 內使用 Docker 的方式。驗證：依文件逐步操作可完成虛擬環境安裝與執行 `pytest --collect-only`

## 2. 設定與機密（app-configuration）

- [x] 2.1 實作 `app/config.py`：四個環境的設定類別、`Secret` 遮罩包裝、`load_config()`、驗證函式（未設定或非法的 `APP_ENV` 拒絕啟動、`staging`/`production` 缺必要機密失敗、拒絕 `sa` 與 `TrustServerCertificate=yes`、禁用除錯）、僅 `development` 載入 `.env` 且行程環境變數優先、`development` 缺 `SECRET_KEY` 時使用隨機臨時金鑰並警告、`testing` 預設記憶體 SQLite。驗證：`tests/test_config.py` 涵蓋規格中全部情境並通過
- [x] 2.2 建立 `.env.example`（只含變數名稱與繁體中文說明，連線字串範例使用 `<user>`、`<password>` 佔位符）。驗證：測試斷言檔內每個必要變數都出現、且沒有任何非佔位符的實際值
- [x] 2.3 在 1.1 的忽略規則已生效後，建立本機 `.env`（`APP_ENV=development`、`SECRET_KEY`、指向本機 `localhost`、資料庫 `project`、帳號 `sa`、使用 ODBC Driver 18 並含 `TrustServerCertificate=yes` 的 `DATABASE_URL`；密碼由使用者先前提供，寫入時不得輸出到終端機或日誌）。驗證：`git status` 不列出 `.env`；以 `load_config()` 載入成功且 `repr` 不含密碼
- [x] 2.4 在 `README.md` 補上「環境變數與機密」章節。驗證：章節列出的變數與 `.env.example` 一致（以測試比對）

## 3. 資料庫存取與 migration（database-access）

- [x] 3.1 實作 `app/db/dialect.py`：引擎工廠（SQL Server 設 `pool_pre_ping`、`pool_recycle`；記憶體 SQLite 用單一共享連線）與方言檢查（僅 SQLite 註冊連線事件執行 WAL、外鍵、忙碌等待；此檔案是唯一允許原生 SQL 字串之處）。驗證：測試斷言 SQLite 連線的 `foreign_keys` 為開啟且違反外鍵時被拒絕、檔案型 SQLite 的日誌模式為 WAL；SQL Server 引擎沒有註冊該連線事件
- [x] 3.2 實作請求級 session（`before_request` 建立、無例外且狀態碼小於 400 時提交、`teardown` 回復並關閉）與 `BaseRepository`。驗證：測試涵蓋「成功提交」「例外回復」「連線一定被歸還」三個情境
- [x] 3.3 實作 `app/models/base.py`（`public_id` UUID 字串欄位、UTC 的 `created_at`/`updated_at` 由 Python 端維護）與 `normalize_unique_text()`（NFC、去頭尾空白、轉小寫）。驗證：以僅供測試使用的模型驗證繁體中文與表情符號往返、JSON 往返、只差大小寫的值被視為重複、更新時間會更新而建立時間不變
- [x] 3.4 建立 Alembic（`render_as_batch=True`，連線取自應用程式設定）與只含版本機制的基線 revision；`create_all()` 只允許出現在 `tests/`。驗證：對全新的檔案型 SQLite 執行 `upgrade head` 再 `downgrade base` 皆成功；啟動應用程式時資料庫沒有任何新資料表
- [x] 3.5 實作原生 SQL 靜態檢查 `tests/architecture/test_no_raw_sql.py`（以 `ast` 掃描 `app/` 與 `migrations/`，白名單僅 `app/db/dialect.py`）。驗證：測試通過；暫時加入一行 `text("SELECT 1")` 後測試失敗（驗證後移除）
- [x] 3.6 請使用者在本機 SQL Server 建立測試用資料庫 `project_test`，於 `.env` 加入測試連線字串，並加入 `sqlserver` 標記的整合測試（連線、中文與 JSON 往返、唯一性、外鍵、`upgrade head`）。驗證：`pytest -m sqlserver` 全數通過；對開發用 `project` 執行 `alembic upgrade head` 後 `alembic current` 顯示最新版本
- [x] 3.7 在 `README.md` 補上「資料庫與 migration」章節（雙軌說明、`sqlserver` 測試的前置條件、升級指令）。驗證：依文件指令可成功執行升級與測試

## 4. 應用工廠與分層（application-layering）

- [x] 4.1 實作 `create_app()`（可依環境建立彼此隔離的實例）、Page 與 API 兩個 Blueprint、基礎樣板 `base.html`（UTF-8、`lang="zh-Hant-TW"`、`data-bs-theme="dark"`）、一個僅供驗證用的最小首頁，以及僅在 `staging`/`production` 啟用的 `ProxyFix`。驗證：測試涵蓋「兩個實例互不影響」「頁面含正確 meta/lang/theme」「正式環境採信 `X-Forwarded-Proto`、開發環境忽略」
- [x] 4.2 實作統一錯誤處理：API 回 JSON（含錯誤代碼與繁體中文訊息）、頁面回繁體中文錯誤頁，皆不含堆疊追蹤與路徑。驗證：測試涵蓋「`/api/` 不存在路徑為 JSON 404」「未預期例外為 500 且不含堆疊」「頁面 404 為繁體中文」
- [x] 4.3 實作層級依賴檢查 `tests/architecture/test_layering.py`（以 `ast` 解析 import，端點不得直接用 Repository/session、Repository 不依賴 Service 與端點、Service 不依賴 HTTP 請求物件）。驗證：測試通過；暫時在端點加入對 Repository 的 import 後測試失敗（驗證後移除）

## 5. 日誌與健康檢查（service-observability）

- [x] 5.1 實作 JSON 日誌（欄位白名單、未列入欄位丟棄、例外只記類型與位置）、請求識別碼（驗證 `^[A-Za-z0-9_-]{8,64}$`，否則重新產生並回傳於回應標頭）、請求日誌（路徑不含查詢字串）。驗證：`tests/test_logging.py` 涵蓋規格情境，並以含 `Authorization`、Cookie、密碼欄位、含機密字串的例外餵入，斷言輸出不含這些值
- [x] 5.2 實作 `/healthz`（不碰資料庫）與 `/readyz`（以 ORM 表達式確認資料庫；失敗回 503 且內容不含任何內部資訊），兩者回應 `Cache-Control: no-store`。驗證：測試涵蓋「正常 200」「資料庫停止時 healthz 仍 200 而 readyz 為 503」「503 內容不含連線資訊」「無快取標頭」
- [x] 5.3 在 `README.md` 補上「日誌欄位與健康檢查」章節。驗證：文件列出的允許與禁止欄位與格式器白名單一致（以測試比對）

## 6. 離線前端資產（offline-frontend-assets）

- [x] 6.1 下載 Vue 3（全域生產版）、Bootstrap 5.3（僅 CSS）、Axios（瀏覽器版）到 `app/static/vendor/`，固定版本並寫入 `MANIFEST.json`（名稱、版本、來源、授權、SHA-256）。下載工具只在取得資產時使用，不成為專案相依。驗證：`tests/test_assets_manifest.py` 重算校驗碼與清單一致，竄改任一檔後測試失敗（驗證後還原）
- [x] 6.2 取得 Noto Sans TC（改用可變字型，涵蓋字重 100 至 900，比兩個靜態字重更小）的 woff2 字元範圍切片到 `app/static/fonts/`，撰寫 `fonts.css`（`unicode-range`、`font-display: swap`、後備字型 Microsoft JhengHei 與 PingFang TC），並把字型也列入資產清單。驗證：`fonts.css` 引用的每個檔案都存在且在清單內；記錄字型總大小
- [x] 6.3 讓 `base.html` 以 `url_for('static', ...)` 載入上述資產與樣式，並實作禁用技術檢查 `tests/architecture/test_frontend_rules.py`（無 `.vue`、無打包工具設定、樣板無外部 `http(s)://` 資源、無 Bootstrap JavaScript、`data-bs-*` 僅 `data-bs-theme="dark"`、無外部字型網址）。驗證：測試通過；暫時加入 CDN 連結後測試失敗（驗證後移除）
- [x] 6.4 在 `README.md` 補上「離線資產與更新方式」章節（如何取得新版本並更新清單）。驗證：依文件步驟更新清單後校驗測試仍通過

## 7. 共用 UI 元件（shared-ui-components）

- [x] 7.1 建立 `theme.css`（色彩變數：`#0F1012`、`rgba(28, 30, 35, 0.75)`、`#E2E4E9`、`#8A8F99`、`#FF6B35`、`#2A2D34`），全站不使用 `#FFFFFF` 文字色。驗證：測試解析樣式，斷言變數值正確且全站樣式找不到 `#FFFFFF` 文字色
- [x] 7.2 實作與框架無關的 `TypewriterBuffer`（以 `Intl.Segmenter` 依字素切分、積壓越多速度越快、時鐘可注入、純文字輸出、結束時加速補完並通知）。驗證：以 `node --test` 撰寫的測試涵蓋「完整文字逐字顯示且最終相同」「片段陸續到達順序正確」「中文與表情符號不被切開」「HTML 標記以文字呈現」「結束補完並回呼」，且全部通過
- [x] 7.3 實作 `ui-modal`（Vue 全域元件，`v-if`/`v-show` 控制；遮罩 `rgba(15, 16, 18, 0.85)` 加 `blur(8px)`；容器 `rgba(22, 24, 28, 0.95)` 與 `1px solid #2A2D34`；左上角系統狀態碼；對話框角色、焦點限制、Esc 關閉並還原焦點；預設按鈕繁體中文）。驗證：靜態測試確認樣式數值與無 `data-bs-*`/Bootstrap 物件；開發示範頁（7.5）人工確認 Esc 與焦點行為
- [x] 7.4 實作 `ui-toast` 與 `useToast()`（右下角、2px `#FF6B35` 左側線、由下往上淡入、預設時間後自動消失、多則依序堆疊）。驗證：靜態測試確認樣式與無 Bootstrap 互動依賴；示範頁人工確認堆疊與自動消失
- [x] 7.5 建立僅在 `development`/`testing` 註冊的元件示範頁 `/dev/components`，並在 `README.md` 補上「共用元件」章節。驗證：測試確認 `production`/`staging` 下該路徑為 404；在瀏覽器逐項勾選 Modal、Toast、打字機的人工驗收清單並把結果記錄於 `README.md`

## 8. 容器執行環境（container-runtime）

- [x] 8.1 撰寫 `Dockerfile`（`python:3.14-slim`、安裝 `unixodbc` 與 `msodbcsql18`、非 root 使用者、`--preload` 的 gunicorn gthread 啟動）與 `.dockerignore`（排除 `.env`、`.git`、`tests`、`openspec`、`docs`、快取）。驗證：經由 WSL 執行 `docker build` 成功
- [x] 8.2 撰寫容器驗收腳本 `scripts/verify-container.ps1`（透過 WSL 先啟動 Docker 服務），檢查：行程使用者非 root、映像內沒有 `.env` 與 `.git`、前端資產與字型存在、Python 主要與次要版本等於 `.python-version`、`pyodbc` 可載入且 `odbcinst` 列出 ODBC Driver 18、缺少必要機密時容器以非零碼結束且輸出只含變數名稱、`APP_ENV=testing` 下 `/healthz` 與 `/readyz` 回 200、送出 SIGTERM 後正常結束。驗證：腳本全部項目通過
- [x] 8.3 在 `README.md` 補上「容器建置與驗收」章節，並明確記載限制（SQL Server 只監聽 `127.0.0.1`，容器內無法連線，故容器驗收使用 SQLite，真實 SQL Server 由主機整合測試驗證）。驗證：依文件指令可重現 8.1 與 8.2 的結果

## 9. 整合驗收

- [x] 9.1 完整執行所有檢查：`pytest`（SQLite）、`pytest -m sqlserver`、`node --test`、`scripts/verify-container.ps1`、`openspec validate platform-foundation`。驗證：全部通過，且 `git status` 顯示沒有 `.env` 或任何機密被追蹤
- [x] 9.2 以全文搜尋確認機密沒有進入任何被追蹤的檔案（資料庫密碼、`SECRET_KEY` 實際值）。驗證：搜尋結果為零筆；如有命中，修正後重跑 9.1
- [x] 9.3 在本機實際啟動（`flask run`，連本機 SQL Server）並以瀏覽器開啟首頁、`/healthz`、`/readyz`、`/dev/components`。驗證：頁面皆正常，字型來自本站，瀏覽器開發者工具的網路面板沒有任何外部請求
