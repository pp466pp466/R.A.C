# Design

## Context

動機與範圍見 proposal.md。目前專案是全新的：沒有任何程式碼、沒有既有規格，只有 `AGENTS.md`（唯一權威來源）與 `docs/`。實作時須遵守的環境事實：

- 開發機為 Windows 10（21H2），Python 3.14.8；SQL Server 以 Windows 服務執行，**只監聽 `127.0.0.1:1433`**，ODBC Driver 17 與 18 皆已安裝；使用者已確認 `project` 資料庫可用 `sa` 登入。
- Windows 原生沒有 `docker` 指令。容器透過 WSL 發行版 `Ubuntu-2204`：先 `service docker start`，再以 `wsl -d Ubuntu-2204 -u root -- docker ...` 操作；專案在 WSL 內的路徑為 `/mnt/c/Users/User/Desktop/newprojet`。
- gunicorn 無法在 Windows 原生執行，本機開發用 `flask run`，正式與容器用 gunicorn。
- 前端不可有 Node 建置步驟（無 Vite、無 `.vue`）。Node 24 雖存在於開發機，但只可作為可選的測試工具。

## Goals / Non-Goals

**Goals:**
- 一套所有後續 change 共用、且規範被自動化檢查守住的地基：設定、資料庫存取、分層、離線資產、共用元件、日誌與健康檢查、容器。
- 規範以測試強制，而非只寫在文件：原生 SQL、層級依賴、禁用前端技術、模板外部網址、`data-bs-*`、日誌敏感欄位，都有失敗即擋下的檢查。
- 在 SQLite（快速）與 SQL Server（真實）兩種資料庫上持續驗證雙軌相容。

**Non-Goals:**
- 不建立任何業務資料表（`users` 屬 C-2，客服表屬 C-3）；基線 migration 只含 Alembic 版本機制。
- 不做登入、CSRF、速率限制、Gemini 呼叫、管理後台、雲端部署與負載測試。
- 不決定最終的 gunicorn worker 與執行緒數（屬 C-5 依負載測試調整）。

## Decisions

### D1. 專案佈局：單一套件 `app/`，Blueprint 分流

```
app/
  __init__.py        # create_app(env)
  config.py          # 各環境設定類別、驗證、機密遮罩
  db/                # engine 工廠、方言檢查（唯一允許 PRAGMA 之處）、session、模型基礎慣例
  controllers/       # page_controller.py、api_controller.py、health_controller.py
  services/          # 業務邏輯（C-1 僅放共用工具）
  repositories/      # BaseRepository
  models/            # 基礎 mixin；業務模型由後續 change 新增
  observability/     # JSON 日誌、請求識別碼
  templates/         # base.html、error pages
  static/            # vendor/、fonts/、css/、js/ui/
migrations/          # Alembic
tests/
wsgi.py              # gunicorn 進入點
```

**理由**：與 AGENTS.md 第 3 節的四層對應，路徑沿用 spec_report 的 `controllers/page_controller.py`、`controllers/api_controller.py`。**替代方案**：多個套件或 monorepo——對單一網站過度設計。

### D2. 設定：類別 + 驗證函式，機密以 `Secret` 包裝

每個 `APP_ENV` 一個設定類別；`load_config()` 讀 `APP_ENV`（缺漏或非法即拋出明確錯誤），僅在 `development` 以 python-dotenv 載入 `.env`（`override=False`，行程環境變數優先）。驗證函式在 `staging`/`production` 檢查：必要機密非空、除錯關閉、資料庫連線不使用 `sa`、不含 `TrustServerCertificate=yes`。機密值包在 `Secret` 型別內，`repr`/`str` 一律遮罩；連線字串以 SQLAlchemy 的 `URL.render_as_string(hide_password=True)` 輸出。`development` 缺 `SECRET_KEY` 時產生隨機臨時金鑰並記錄警告。**替代方案**：pydantic-settings——多一個相依，需求（約 6 個變數）不值得。

### D3. 資料庫：方言檢查集中在 `app/db/dialect.py`

- `create_engine_for(config)` 依 URL 建立引擎。SQL Server 使用 `mssql+pyodbc` 與 ODBC Driver 18，`pool_pre_ping=True`、`pool_recycle=1800`；SQLite 記憶體資料庫使用 `StaticPool` 並允許跨執行緒，確保測試中多連線看到同一個資料庫。
- 只有 `engine.dialect.name == "sqlite"` 時才 `event.listen(engine, "connect", ...)`，執行 `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout`。此檔案是全專案唯一允許出現原生 SQL 字串的位置。（記憶體 SQLite 的 WAL 會自動退回 `memory`，測試只斷言 `foreign_keys` 與 `busy_timeout`，WAL 在檔案型 SQLite 上驗證。）
- 請求級 session：`before_request` 建立、`after_request` 在回應狀態小於 400 且無例外時提交、`teardown_request` 回復未提交內容並關閉。提交失敗轉為 500。Repository 只接收 session，不自行提交。
- 欄位慣例（`models/base.py`）：`public_id`（`Unicode(36)` 存 UUID 字串）、`created_at`/`updated_at`（UTC，由 Python 端預設值與 `onupdate` 維護，**不使用伺服器端函式**以避免兩種資料庫語法差異）；文字用 `Unicode`/`UnicodeText`，結構化資料用 `JSON`。
- `normalize_unique_text()`：NFC、去頭尾空白、轉小寫，供 C-2 的 email 使用。
- **可為空的唯一欄位用部分唯一索引**（`unique_when_not_null()`）：實作時在真實 SQL Server 上發現，SQL Server 的 UNIQUE 約束只允許一個 NULL，SQLite 卻允許多個，直接用 `unique=True` 會讓雙軌行為分歧。改以「只涵蓋非 NULL 列」的部分唯一索引（`sqlite_where`/`mssql_where`，用 ORM 表達式而非原生字串）。C-2 的 `email_canonical`、`provider_subject` 等可為空的唯一欄位須使用它。
- **`alembic.ini` 必須是純 ASCII**：Alembic 以系統地區編碼讀取（這台機器是 cp950），中文註解會造成解析失敗；由測試守住。`migrations/env.py` 等 Python 檔不受影響。

**替代方案**：以 SQLAlchemy `Uuid` 型別存 UUID——SQL Server 對應 `UNIQUEIDENTIFIER`，行為與 SQLite 文字儲存不同，改用字串以保持雙軌一致。

### D4. Alembic：基線只含版本機制

`migrations/env.py` 從應用程式設定取得連線，`render_as_batch=True`。基線 revision 不建立業務資料表（避免提前綁定 C-2 的設計）。`create_all()` 只允許出現在 `tests/`。應用程式啟動不碰結構；升級是獨立指令 `flask db upgrade`（或 `alembic upgrade head`）。

### D5. 規範用自動化檢查守住（`tests/architecture/`）

| 檢查 | 做法 |
|---|---|
| 無原生 SQL | 以 `ast` 掃描 `app/` 與 `migrations/`：禁止 `text()`、`exec_driver_sql`、`op.execute`、對字串呼叫 `.execute()`；白名單僅 `app/db/dialect.py` |
| 層級依賴 | 以 `ast` 解析 import，依 D1 的方向規則驗證；不新增 import-linter 相依 |
| 禁用前端技術 | 掃描檔案：無 `.vue`、無 `vite.config.*`/`webpack.config.*`/`package.json` 的打包腳本、樣板無外部 `http(s)://` 資源、無 Bootstrap JS、`data-bs-*` 僅允許 `data-bs-theme="dark"`、無 `#FFFFFF` 文字色 |
| 日誌敏感欄位 | 對格式器餵入含敏感值的紀錄，斷言輸出不含 |

### D6. 日誌與健康檢查

使用標準 `logging` 與自製 JSON 格式器，**欄位白名單**：未列入的 `extra` 欄位一律丟棄。例外只記錄類型與 `模組:行號`，不記訊息內容。請求日誌在 `after_request` 輸出，路徑取 `request.path`（不含查詢字串）。請求識別碼以 `^[A-Za-z0-9_-]{8,64}$` 驗證上游 `X-Request-ID`，否則產生新值。`/readyz` 以 `select(literal(1))`（SQLAlchemy 表達式，非原生字串）確認資料庫，失敗回 503 且內容僅 `{"status":"unavailable"}`。健康端點加 `Cache-Control: no-store`。

### D7. 前端資產與元件

- **資產**（下載一次、版本固定、記錄於 `app/static/MANIFEST.json` 的名稱/版本/來源/授權/SHA-256；由 `scripts/update_asset_manifest.py` 維護，來源為 npm 發行包，並比對登錄庫記載的完整性校驗碼）：Vue 3 全域生產版、Bootstrap 5.3 **只取 CSS**、Axios 瀏覽器版（連同 `.map` 檔，避免開發者工具產生 404）。字型 Noto Sans TC（SIL OFL）取自 fontsource 發佈的**可變字型** woff2 字元範圍切片（108 個切片、共 4.1 MB，涵蓋字重 100 至 900），`font-display: swap`，`fonts.css` 以 `unicode-range` 宣告。**實作時由「400 與 700 兩個靜態字重」改為可變字型**：靜態兩個字重要 218 個檔案、6.3 MB，可變字型更小且涵蓋所有字重。下載工具只在取得資產時使用，**不成為專案相依**。
- **元件**（`static/js/ui/`，Vue 全域變數模式，不用 SFC）：`ui-modal`、`ui-toast`（含 `useToast()` 的響應式佇列）以全域元件註冊；`TypewriterBuffer` 為與框架無關的純 JavaScript 類別，以 `Intl.Segmenter`（後備 `Array.from`）依字素切分，速率 = 基礎速度 + 積壓量 × 係數，時鐘可注入以便測試。顏色集中於 `theme.css` 的 CSS 變數。
- **測試**：`TypewriterBuffer` 以 Node 內建 `node --test` 驗證（可選，無 Node 時略過）；Modal 與 Toast 以靜態檢查（無 `data-bs-*`、無 Bootstrap 物件、色碼與尺寸存在）加上僅在 `development`/`testing` 註冊的元件示範頁供人工目視驗收。**替代方案**：jsdom 或 Playwright——需要新增 npm 相依與設定，延到 C-3 有真實聊天介面時再評估。

### D8. 容器

- 基底映像 `python:3.14-slim`（與開發機主要/次要版本一致，另以 `.python-version` 記錄）；安裝 `unixodbc` 與 Microsoft ODBC Driver 18（`msodbcsql18`，需接受 EULA）；非 root 使用者；`.dockerignore` 排除 `.env`、`.git`、`tests`、`openspec`、`docs`、快取。
- 啟動：`gunicorn -k gthread --workers 2 --threads 8 --timeout 60 --graceful-timeout 30 --preload --bind 0.0.0.0:${PORT:-8080} wsgi:app`。數值是 D-13 的初始值，C-5 依負載測試調整。`--preload` 讓設定驗證錯誤在主程序即中止並以非零結束碼退出，符合「缺機密即啟動失敗」。
- **驗收限制**：SQL Server 只監聽 `127.0.0.1`，WSL 內的容器連不到它。因此容器驗收使用 `APP_ENV=testing` 的 SQLite 驗證 gunicorn、非 root、映像內容、`/healthz`、`/readyz`、SIGTERM，並檢查 pyodbc 與 ODBC Driver 18 在映像內可載入；對真實 SQL Server 的連線由 Windows 主機上的整合測試（標記 `sqlserver`）驗證。**不**為此放寬 SQL Server 的網路綁定。

### D9. 版本控制與環境檔

`git init`，本專案層級設定提交者名稱 `HuangMin`（信箱設於本機 `.git/config`，不寫入任何文件）。`.gitignore` 排除 `.env`、虛擬環境、快取、SQLite 檔、`.claude/settings.local.json`。`.gitattributes`：`* text=auto eol=lf`，並標記字型與圖片為二進位。`.env.example` 只列變數名稱與中文說明。

### D10. 相依與測試

`requirements.txt`（精確版本，安裝時以 Python 3.14 確認有可用版本）與 `requirements-dev.txt`（pytest 等）。pytest 預設在記憶體 SQLite 執行；標記 `sqlserver` 的整合測試只在設定了測試用連線字串時執行，**使用獨立的 `project_test` 資料庫**，避免測試資料表污染開發用的 `project`。

## Risks / Trade-offs

- **Python 3.14 太新** → 已確認主要套件有 3.14 預編譯版本；Dockerfile 與 `.python-version` 釘版，若發現相依問題可整體退到 3.13 且只需改這兩處。
- **字型切片總量大** → 只保留兩個字重並以 `unicode-range` 按需載入；若總量過大再評估減為單一字重（紀錄於資產清單）。
- **容器驗收無法連 SQL Server** → 以 SQLite 驗證容器行為、以主機整合測試驗證 SQL Server；殘餘風險是「容器內的 SQL Server 驅動連線」要到 C-5 的雲端環境才首次實際驗證。可選補強：在 WSL 內執行 SQL Server Linux 容器做整合測試（映像大、需 2 GB 以上記憶體），本 change 不做。
- **舊版 WSL（不支援 systemd）** → Docker 需手動啟動；已記錄於任務與 `docs/report.md`，不影響映像本身。
- **`sa` 帳號與簡單密碼用於本機** → 僅限 `development`/`testing`，驗證函式在 `staging`/`production` 強制拒絕；密碼只存於 `.env`。建議（非必須）日後改用專用帳號。
- **架構檢查是靜態掃描** → 可能漏掉動態組字串的原生 SQL；以程式碼審查補強，掃描規則寫在測試中並保持嚴格。
- **在提交時才加入 `.env` 排除** → 專案目前不是 git 儲存庫，因此「`git init` 與 `.gitignore`」必須是第一批任務，且在任何 `.env` 建立之前完成。

## Migration Plan

全新專案，沒有既有資料。順序：版本控制與忽略規則 → 套件與設定 → 資料庫存取與 migration 基線 → 對本機 `project` 執行 `upgrade` → 前端資產與元件 → 日誌與健康檢查 → 容器。回復方式：本 change 不改動既有系統，失敗時刪除新增檔案並 `alembic downgrade base` 即可。

## Open Questions

- 是否於 C-3 導入 Playwright 做端對端測試？（不影響本 change 的規格與任務。）
- `requirements.txt` 是否加入雜湊鎖定（`--require-hashes`）？可在 C-5 的供應鏈強化時一併決定。
