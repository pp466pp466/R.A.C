# Proposal

## Why

本專案要做成「會員制智能客服」與「開放世界文字 RPG」雙核心平台，但目前只有規範文件（`AGENTS.md`、`docs/`），沒有任何程式碼。後續所有 change（會員驗證、客服對話、額度與後台、雲端部署、RPG）都依賴同一套地基：應用骨架、設定與機密管理、雙軌資料庫存取、離線前端資產與共用元件、日誌與健康檢查，以及可重現的容器化執行環境。地基不先定案，後續各 change 會各自發明做法，導致規範（離線前端、純 ORM、機密不入版本庫、多 worker 無行程內狀態）被逐一破壞。

## What Changes

- 建立 Flask 應用工廠與四層目錄（MVC Page Controller、Web API Controller、Service、Repository），並讓分層規則可被測試。
- 建立設定類別與機密管理：以 `APP_ENV`（development、testing、staging、production）選擇設定；未設定即拒絕啟動；staging 與 production 缺少必要機密時啟動失敗；提供只含變數名稱的 `.env.example`。
- 建立資料庫存取基礎：SQLAlchemy 引擎工廠、方言檢查（僅 SQLite 註冊 PRAGMA 連線事件，SQL Server 不註冊並設定連線池參數）、Repository 基底類別與 Session 生命週期、Unicode 與 JSON 欄位型別慣例、email 等唯一字串的正規化慣例；本機開發連線 MS SQL Server（`localhost`、資料庫 `project`、帳號 `sa`、`TrustServerCertificate=yes`），密碼只存於 `.env`。
- 建立 Alembic 基線（啟用 `render_as_batch`），並以測試約束 migration 不含方言專屬原生 SQL。
- 下載並本地化前端資產：Vue 3、Bootstrap 5.3、Axios 置於 `static/vendor/`，Noto Sans TC（woff2、依字元範圍切片）置於 `static/fonts/`；模板一律以 `url_for` 引用，並以測試禁止 CDN、Google Fonts、`.vue`、Vite。
- 建立共用 UI 元件：自製 Modal、Toast（Vue 3 全域變數模式、`v-if`/`v-show` 接管、不使用 Bootstrap JavaScript 與 `data-bs-*`，唯一例外為 `data-bs-theme="dark"`）、打字機緩衝區（Typewriter Buffer）。
- 建立結構化 JSON 日誌（遵守日誌欄位白名單與黑名單）、`request_id`，以及 `/healthz`（存活）與 `/readyz`（含資料庫檢查）。
- 建立容器化執行環境：Dockerfile（Python 3.14 釘選、非 root、不含 `.env`）、gunicorn（gthread worker）、`ProxyFix`；容器驗收透過 WSL 內的 Docker。
- 建立版本控制基礎：`git init`、`.gitignore`（含 `.env`）、`.gitattributes` 固定 LF、提交者名稱 `HuangMin`（僅設於本專案）。
- 不在範圍內：會員驗證（C-2）、客服對話（C-3）、額度與管理後台（C-4）、雲端部署與負載測試（C-5）、訂單（C-6）、RPG。

## Capabilities

### New Capabilities

- `app-configuration`: 依 `APP_ENV` 選擇設定類別、必要機密缺漏時的啟動行為、`.env.example` 與機密不外洩的規則。
- `database-access`: 雙軌（SQLite、SQL Server）引擎建立、方言檢查、連線池設定、純 ORM 的 Repository 與 Session 生命週期、欄位型別與唯一字串正規化慣例、Alembic 基線與 migration 約束。
- `application-layering`: Flask 應用工廠與四層分流（Page Controller 只回 HTML、API Controller 只回 JSON、Service、Repository）、反向代理標頭信任。
- `offline-frontend-assets`: 前端第三方資產與字型的本地化、模板資源引用規則、禁止 CDN 與禁用技術清單。
- `shared-ui-components`: 自製 Modal、Toast 與打字機緩衝區的行為與樣式契約，以及 Vue 狀態完全接管的規則。
- `service-observability`: 結構化 JSON 日誌（欄位白名單與黑名單）、`request_id`、`/healthz` 與 `/readyz`。
- `container-runtime`: 容器映像的組成與執行方式（Python 版本釘選、gthread、非 root、不含機密與 `.env`）。

### Modified Capabilities

<!-- 專案尚無任何既有規格（openspec/specs 為空），沒有需要修改的能力。 -->

## Impact

- **新增程式碼**：整個應用骨架（`app/` 下的設定、擴充、controllers、services、repositories、models、templates、static）、`migrations/`、`tests/`、`Dockerfile`、`.dockerignore`、`.env.example`、`.gitignore`、`.gitattributes`、相依套件清單。
- **新增相依套件**（Python 3.14，已確認 pyodbc、SQLAlchemy、argon2、greenlet、cryptography 有 Windows 預編譯版本）：Flask、SQLAlchemy、Alembic、pyodbc、gunicorn、python-dotenv、pytest；前端資產為下載的靜態檔，無 Node 建置步驟。
- **不修改**：`AGENTS.md`（唯一權威來源，僅依其實作）、既有 skill。
- **環境前提**：本機 SQL Server 服務已執行並可登入（使用者已確認）；Windows 無原生 `docker` 指令，容器驗收需先在 WSL `Ubuntu-2204` 執行 `service docker start`。
- **後續 change 的相依**：C-2 至 C-6 與 RPG 皆建立在本 change 的設定、資料庫存取、分層、前端資產與日誌之上。
