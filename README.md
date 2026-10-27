# 開放世界文字 RPG × 會員制智能客服

以 LLM 驅動的雙核心平台。開發規範以根目錄的 [AGENTS.md](AGENTS.md) 為唯一權威來源；設計決策見 [docs/report.md](docs/report.md)，變更提案見 `openspec/changes/`。

目前進度：C-1 `platform-foundation`（專案地基）已實作完成，待審查；後續為 C-2 會員驗證、C-3 客服對話（見 `docs/report.md` 第 5 章）。

## 環境需求

| 項目 | 需求 |
|---|---|
| 作業系統 | Windows 10（開發）；Linux 容器（部署） |
| Python | 3.14（見 `.python-version`） |
| 資料庫（開發） | 本機 Microsoft SQL Server，並安裝 ODBC Driver 18 |
| 版本控制 | Git |
| 容器（可選） | WSL 2 內的 Docker Engine（見下方） |
| Node.js（可選） | 僅用於前端打字機緩衝區的單元測試（`node --test`） |

## 安裝

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 執行與測試

```powershell
# 本機開發伺服器（Windows 無法執行 gunicorn；正式環境與容器才使用 gunicorn）
.\.venv\Scripts\python.exe -m flask --app wsgi run

# 測試（預設使用記憶體 SQLite，不需要任何機密）
.\.venv\Scripts\python.exe -m pytest
```

## 環境變數與機密

設定以環境變數 `APP_ENV` 選擇，**沒有預設值**：未設定或不合法會拒絕啟動。可用的值為 `development`、`testing`、`staging`、`production`。

| 變數 | 必填 | 說明 |
|---|---|---|
| `APP_ENV` | 是 | 執行環境，必須由行程環境變數提供 |
| `SECRET_KEY` | staging、production | 工作階段簽章金鑰；development 缺漏時使用臨時金鑰 |
| `DATABASE_URL` | development、staging、production | 資料庫連線字串；testing 預設為記憶體 SQLite |
| `LOG_LEVEL` | 否 | 日誌層級，預設 `INFO` |
| `FLASK_DEBUG` | 否 | 本機除錯模式；staging 與 production 設定即啟動失敗 |
| `TEST_SQLSERVER_URL` | 否 | SQL Server 整合測試用連線字串，必須指向 `project_test` |

規則：

- 只有 `development` 會讀取專案根目錄的 `.env`，且行程環境變數優先；`staging` 與 `production` 只使用行程環境變數（由平台機密功能注入）。
- 複製 `.env.example` 為 `.env` 後填入實際值。`.env` 已被 `.gitignore` 排除，**絕不可提交**；機密也不得寫入程式碼、文件或日誌。
- `staging` 與 `production` 缺少必要機密、資料庫連線使用 `sa` 帳號、連線字串含 `TrustServerCertificate=yes`、或開啟除錯模式，都會啟動失敗。後兩項只允許在本機開發使用。
- 設定物件與連線字串在任何輸出中都會遮罩密碼。

## 資料庫與 migration

系統同時支援 **SQLite**（快速單元測試）與 **SQL Server**（本機開發、整合測試、正式環境），兩者共用同一套 ORM 程式碼。

| 項目 | 規則 |
|---|---|
| 原生 SQL | 全面禁止，唯一例外是 `app/db/dialect.py` 的 SQLite 連線初始化；由 `tests/architecture/` 的靜態掃描強制 |
| 連線初始化 | 只有 SQLite 會自動套用預寫日誌、外鍵強制檢查與忙碌等待；SQL Server 不執行任何 SQLite 設定 |
| 交易 | 每個請求一個 Session：成功（狀態碼小於 400）時提交，否則回復，一定釋放連線 |
| 唯一字串 | 先經 `normalize_unique_text()` 正規化再寫入與查詢（SQLite 區分大小寫、SQL Server 不區分） |
| 可為空的唯一欄位 | 使用 `unique_when_not_null()`；直接 `unique=True` 會讓 SQL Server 只允許一個空值 |
| 資料表結構 | 只能透過 Alembic migration 變更；應用程式啟動時不會建立資料表，也不會自動執行 migration |

### 指令

```powershell
# 升級到最新版本（開發者在 .env 寫了 APP_ENV=development，工具會自動採用；應用程式本身不會）
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current

# 新增 migration（只使用 Alembic op API 與 ORM 模型，不得含原生 SQL）
.\.venv\Scripts\python.exe -m alembic revision -m "說明"

# 測試：預設只跑 SQLite；加上 -m sqlserver 才跑 SQL Server 整合測試
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest -m sqlserver
```

### SQL Server 整合測試的前置條件

1. 本機 SQL Server 已啟動，並安裝 ODBC Driver 18。
2. 已建立**獨立的** `project_test` 資料庫（測試會建立並刪除自己的資料表，**絕不可指向開發用的 `project`**）。
3. 在 `.env` 設定 `TEST_SQLSERVER_URL` 指向 `project_test`。資料庫名稱不是以 `_test` 結尾時，測試會直接失敗而不是略過。

### 注意事項

- `alembic.ini` 必須是**純 ASCII**：Alembic 用系統地區編碼讀取它（繁體中文 Windows 為 cp950），中文註解會造成解析失敗。
- `alembic.ini` 刻意不含連線字串，連線一律取自 `DATABASE_URL`。
- 基線 revision 只建立 Alembic 版本機制，不建立業務資料表。

## 日誌欄位與健康檢查

日誌是結構化 JSON，每行一筆，輸出到標準輸出。採**白名單**：沒有列在下表的欄位一律丟棄。

| 欄位 | 說明 |
|---|---|
| `time` | UTC 時間（ISO 8601，毫秒） |
| `level` | 日誌層級 |
| `logger` | 記錄器名稱 |
| `message` | 訊息文字（會再遮罩密碼、權杖、連線字串中的帳密） |
| `request_id` | 請求識別碼 |
| `path` | 路由規則（例如 `/verify/<token>`）；未比對到路由時為路徑本身。**永遠不含查詢字串** |
| `method` | HTTP 方法 |
| `status` | 狀態碼 |
| `latency_ms` | 處理延遲（毫秒） |
| `user_id` | 內部使用者編號（整數，已登入時才有） |
| `conversation_id` | 內部對話編號（整數） |
| `intent` | 客服意圖（列舉值） |
| `model` | 模型名稱 |
| `attempt` | 嘗試次數 |
| `prompt_tokens` | 輸入 token 數 |
| `completion_tokens` | 輸出 token 數 |
| `finish_reason` | 模型結束原因 |
| `error_type` | 例外類型 |
| `error_location` | 例外發生的檔名與行號 |

**絕不記錄**：密碼與密碼雜湊、工作階段 Cookie、CSRF 與驗證／重設權杖、授權標頭、完整 API 金鑰、使用者 email、使用者訊息全文、模型回覆與原始輸出，以及含權杖的完整網址。例外只記錄類型與位置，**不記錄例外訊息與堆疊**（訊息可能夾帶機密）。

### 請求識別碼

每個請求都有識別碼，回傳於回應標頭 `X-Request-ID`，並出現在該請求的所有日誌。上游提供的值必須符合 `^[A-Za-z0-9_-]{8,64}$` 才會沿用，否則重新產生。

### 健康檢查

| 端點 | 用途 | 成功 | 失敗 |
|---|---|---|---|
| `/healthz` | 存活：只表示行程還在，**不碰資料庫** | 200 | — |
| `/readyz` | 就緒：實際確認資料庫可用 | 200 | 503 |

兩者都不需登入，回應只有 `{"status": "ok"}` 或 `{"status": "unavailable"}`，不含版本、設定、連線字串或錯誤細節，並帶有 `Cache-Control: no-store`。

## 離線資產與更新方式

前端完全離線：所有第三方函式庫與字型都存放在本機，頁面載入時**不會連線任何外部網站**。禁止 CDN、Google Fonts、`.vue`、Vite、外部彈窗套件與 Bootstrap 的 JavaScript，由 `tests/architecture/test_frontend_rules.py` 強制。

### 目前的資產

| 資產 | 版本 | 授權 | 位置 |
|---|---|---|---|
| Vue（全域生產版） | 3.5.44 | MIT | `app/static/vendor/vue/` |
| Bootstrap（只用 CSS） | 5.3.8 | MIT | `app/static/vendor/bootstrap/` |
| Axios | 1.20.0 | MIT | `app/static/vendor/axios/` |
| Noto Sans TC（可變字型，字重 100 至 900） | 5.3.1 | OFL-1.1 | `app/static/fonts/` |

字型共 108 個依字元範圍（`unicode-range`）切片的 woff2，總量約 **4.1 MB**；瀏覽器只會下載頁面實際用到的切片。字型堆疊為 `Noto Sans TC Variable`，後備為 Microsoft JhengHei、PingFang TC。

每個資產的版本、來源、授權與 SHA-256 都記錄在 `app/static/MANIFEST.json`，並由測試重算比對；清單以外的第三方檔案不得出現在 `vendor/` 與 `fonts/`。

### 更新函式庫

以 Axios 為例（其他函式庫相同）。**下載的檔案一律視為不可信資料**：放在全新的空資料夾，不要在裡面執行任何東西。

```powershell
# 1. 查詢新版本
npm view axios dist-tags.latest

# 2. 在全新的空資料夾下載發行包，並比對 npm 登錄庫記載的完整性校驗碼（必須為 True）
$work = Join-Path $env:TEMP "asset-update"; New-Item -ItemType Directory $work | Out-Null; Set-Location $work
$tgz = (npm pack axios@<新版本> --silent | Select-Object -Last 1).Trim()
$expected = (npm view axios@<新版本> dist.integrity).Trim()
$actual = 'sha512-' + [Convert]::ToBase64String([Security.Cryptography.SHA512]::Create().ComputeHash([IO.File]::ReadAllBytes((Resolve-Path $tgz).Path)))
$actual -eq $expected
tar -xzf $tgz

# 3. 把需要的檔案（本體、.map、LICENSE）複製到 app/static/vendor/axios/
# 4. 編輯 app/static/MANIFEST.json：更新該資產的 version、source、upstream_integrity
# 5. 回到專案根目錄，重算校驗碼並檢查
.\.venv\Scripts\python.exe scripts\update_asset_manifest.py
.\.venv\Scripts\python.exe scripts\update_asset_manifest.py --check

# 6. 驗證，並在瀏覽器確認畫面正常、網路面板沒有外部請求
.\.venv\Scripts\python.exe -m pytest tests/test_assets_manifest.py tests/test_fonts_and_static.py tests/architecture/test_frontend_rules.py
```

字型的更新方式相同，來源套件為 `@fontsource-variable/noto-sans-tc`：複製 `files/` 內的 `*-wght-normal.woff2`，並把官方 `wght.css` 裡的 `url(./files/` 改成 `url(./` 後存成 `app/static/fonts/fonts.css`（開頭保留 `@charset "UTF-8";`），最後同樣重算清單。

## 共用元件

所有頁面的基礎樣板 `base.html` 都已載入共用的色彩（`theme.css`）、元件樣式（`components.css`）與三個腳本：`typewriter.js`、`toast-store.js`、`ui-components.js`。元件以 Vue 3 全域變數模式撰寫，**不使用 Bootstrap 的 JavaScript 與 `data-bs-*`**，所有文字一律以純文字渲染（不使用 `v-html`／`innerHTML`），內建文字為繁體中文。

```html
<script>
const { createApp, ref } = Vue;
const app = createApp({ setup() { const open = ref(false); return { open }; } });
UI.install(app);            // 註冊 <ui-modal> 與 <ui-toast-region>
app.mount('#app');
</script>
```

| 元件 | 用法 | 重點 |
|---|---|---|
| Modal | `<ui-modal v-model="open" title="..." status-code="SYSTEM_PROMPT">...</ui-modal>` | 由 `v-if` 接管；遮罩 `rgba(15, 16, 18, 0.85)` 加 `blur(8px)`；容器 `rgba(22, 24, 28, 0.95)` 加 1px `#2A2D34` 邊框；左上角小字狀態碼；Esc、點遮罩可關閉（`:closable="false"` 可停用）；焦點限制在對話框內，關閉後還原；開啟時背景不可捲動 |
| Toast | 頁面放一個 `<ui-toast-region>`，之後 `UI.toast('訊息', { type, duration })` | 右下角、左側 2px `#FF6B35` 線、由下往上淡入；預設 4 秒消失，`duration: 0` 不自動消失；多則依序堆疊；`type: 'error'` 使用 `role="alert"` |
| 打字機 | `new UI.TypewriterBuffer({ onUpdate, onDone })`；客服 `play(完整文字)`，RPG 串流 `push(片段)` … `finish()` | 以字素為單位，不切開中文、表情符號與組合字；積壓越多越快；`finish()` 後最晚 3 秒內顯示完；`flush()` 略過動畫；`cancel()` 取消 |

### 示範頁與測試

- **元件示範頁**：`/dev/components`，**只在 `development` 與 `testing` 環境註冊**；`staging` 與 `production` 沒有這個路徑，連開發用程式碼都不會被載入。
- **自動化測試**：`pytest` 會檢查樣式數值、結構與規則，並自動執行 Node 的前端單元測試（有安裝 Node 才執行）。也可單獨執行：`node --test "tests/js/**/*.test.js"`（Node 24 需要用萬用字元，不能傳資料夾）。

### 人工驗收紀錄（2026-10-27）

以 Edge 156 無頭模式，經 DevTools 協定對 `/dev/components` 做真實的點擊、按鍵與量測，共 **54 項全數通過**，並檢視截圖：

| 項目 | 結果 |
|---|---|
| 色彩與字型 | 主背景 `#0F1012`、文字 `#E2E4E9`；頁面上沒有任何純白文字；字型為本站的 Noto Sans TC Variable，108 個切片中只載入了頁面用到的 18 個 |
| Modal 外觀 | 遮罩 `rgba(15, 16, 18, 0.85)` 加 `blur(8px)` 且覆蓋全螢幕；容器 `rgba(22, 24, 28, 0.95)`，四邊 `1px solid #2A2D34`；左上角 10px 的 `[ SYSTEM_PROMPT ]` |
| Modal 互動 | 開啟後焦點自動進入；Tab 與 Shift+Tab 在對話框內循環；Esc 關閉且焦點回到「開啟 Modal」；點遮罩關閉；背景捲動在開啟時鎖定、關閉後恢復；不可關閉的版本對 Esc 與點遮罩都沒有反應 |
| Toast | 起點在下方、逐格上升並淡入；左側 2px `#FF6B35`；距右下角各 16px；三則依序堆疊且不重疊；約 4 秒後自動消失；`duration: 0` 的不消失，按 × 可關閉；錯誤使用 `role="alert"`；含 HTML 標記的文字以原樣顯示，沒有被解析 |
| 打字機 | 逐字增加（0.3 秒約 15 字、0.7 秒約 37 字，共 58 字）；顯示期間有游標；完成後內容與原文逐字相同（含中文、表情符號、換行）；模擬串流結束後內容相同；略過動畫立刻顯示全部；取消後不再增加；每一刻都停在字素邊界 |
| 網路與主控台 | 整個過程 28 個請求全部指向本站，沒有外部請求；沒有 4xx 或 5xx；主控台沒有錯誤或警告 |

驗收中發現並修正的問題：打字機原本調速過快（一次到達的 58 字不到 0.6 秒就全部出現，幾乎沒有逐字效果），且 `finish()` 的補完期限對一次到達的文字也生效；已調整為基本速度 40 字／秒、加速係數 0.2、補完期限 3 秒（58 字約 1.3 秒、300 字約 2.7 秒）。

**仍需你目視確認**：整體質感與美感是否符合你想要的電影感。可執行 `flask run` 後開啟 `/dev/components` 自行操作。

## 容器建置與驗收

正式環境以 Docker 映像部署（見 AGENTS.md 第 9 節）。映像以 `python:3.14-slim-bookworm` 為基底（Python 版本與 `.python-version` 一致；明確指定 Debian 12，因為 Microsoft ODBC Driver 18 官方支援它），內含 `unixodbc` 與 `msodbcsql18`、前端資產與 108 個字型切片，約 253 MB。

| 面向 | 規則 |
|---|---|
| 執行方式 | `gunicorn`（`gthread`），設定在 `gunicorn.conf.py`；**不使用** Flask 開發伺服器 |
| 初始設定 | 2 個 worker、每個 8 條執行緒、worker 逾時 60 秒、終止時最多等 30 秒讓進行中的請求完成；可用 `WEB_CONCURRENCY`、`GUNICORN_THREADS`、`PORT` 覆寫。數值是初始值，C-5 依負載測試調整 |
| 使用者 | 非 root（uid 10001） |
| 機密 | 映像內沒有 `.env`、`.git`、測試與文件（由 `.dockerignore` 排除，連已刪除的檔案在任何一層都找不到）；所有設定與機密只來自環境變數 |
| 缺少機密 | 使用 `--preload`，設定驗證錯誤會讓主程序以非零碼結束，輸出只列出缺漏的變數名稱 |
| 日誌 | 請求日誌由應用程式輸出 JSON；gunicorn 的存取日誌關閉（避免記下查詢字串） |
| 資料庫升級 | 映像內含 `alembic.ini` 與 `migrations/`，升級是獨立步驟：`docker run --rm -e APP_ENV=... -e DATABASE_URL=... <映像> alembic upgrade head`（應用程式啟動時不會自動升級） |

### 建置與驗收

Docker 在 WSL 內，每次使用前先啟動服務（見下一節）。驗收腳本會建置映像並檢查 34 個項目：

```powershell
.\scripts\verify-container.ps1               # 建置並驗收
.\scripts\verify-container.ps1 --no-build    # 只驗收既有映像
```

檢查內容：Docker 可用、映像建置成功、預設使用者不是 root、映像每一層都沒有 `.env`／`.git`／測試／文件、前端資產與字型齊全、Python 版本等於 `.python-version`、`pyodbc` 可載入且有 ODBC Driver 18、缺少必要機密時以非零碼結束且不洩漏機密值、未設定 `APP_ENV` 時拒絕啟動；並以 `testing` 與「正式環境設定（SQLite 記憶體）」各啟動一次，確認 `/healthz` 與 `/readyz` 回 200、首頁與靜態檔可取得、**正式環境沒有 `/dev/components`**、主行程是非 root 的 gunicorn、日誌是 JSON 且不含機密、收到終止訊號後以結束碼 0 正常結束。

不需要 Docker 的靜態檢查（`tests/test_container_files.py`）也包含在一般的 `pytest` 中。

### 已知限制

- **容器連不到本機 SQL Server**：本機 SQL Server 只監聽 `127.0.0.1`，WSL 內的容器無法連線，所以容器的執行行為以 **SQLite** 驗收；真實的 SQL Server 連線由主機上的整合測試（`pytest -m sqlserver`）驗證。我們**不**為此放寬 SQL Server 的網路綁定。容器內的 SQL Server 驅動「實際連線」要到 C-5 的雲端環境才會首次驗證（本機只確認驅動已安裝且可載入）。
- 終止訊號的驗收確認「正常結束（結束碼 0）並依序關閉」，**沒有**測試「進行中的慢請求會被等完」（目前沒有慢的端點可用來測）。
- 映像只建置 `amd64`。
- 舊版 WSL 不支援 systemd，Docker 需要手動啟動，且每次重新開機後都要再啟動一次。

## 在 WSL 內使用 Docker

本機沒有 Windows 原生的 `docker` 指令。Docker Engine 安裝在 WSL 發行版 `Ubuntu-2204` 內；舊版 WSL 不支援 systemd，所以每次使用前要先啟動服務（可重複執行）：

```powershell
wsl -d Ubuntu-2204 -u root -- service docker start
wsl -d Ubuntu-2204 -u root -- docker ps
```

專案目錄在 WSL 內的路徑是 `/mnt/c/Users/User/Desktop/newprojet`。

## 規範摘要（細節見 AGENTS.md）

- 前端完全離線：不使用 CDN、Google Fonts、`.vue`、Vite 或 Bootstrap 的 JavaScript。
- 資料存取一律透過 SQLAlchemy ORM，不寫原生 SQL。
- 機密（含資料庫密碼）只放在不進版本庫的 `.env`，絕不寫入程式碼或文件。
- 全系統使用繁體中文與 UTF-8。
