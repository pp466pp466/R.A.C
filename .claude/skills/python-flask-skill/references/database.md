# 資料庫與 SQLAlchemy ORM 規範（SQLite 與 SQL Server 雙軌相容）

對應 AGENTS.md 第 4 節。

- 純 ORM 抽象原則：業務邏輯與 Repository 層一律透過 SQLAlchemy ORM 模型進行操作，嚴禁撰寫任何 Raw SQL（原生語法），確保雙軌切換相容性。唯一例外為下方的資料庫連線初始化事件（基礎設施層）。
- 條件式初始化與方言隔離 (Dialect Check)：
  - 建立 Engine 後，若 engine.dialect.name == "sqlite"，才透過 SQLAlchemy 的連線事件（event.listen(engine, "connect", ...)）於底層連線建立時自動執行 PRAGMA journal_mode=WAL;（預寫日誌模式）、PRAGMA foreign_keys=ON;（SQLite 預設不強制外鍵）與 PRAGMA busy_timeout（皆僅限此事件內），不得在業務程式碼中直接呼叫。
  - 若底層為 SQL Server，則不註冊該事件，改由 SQLAlchemy 的連線池與交易管理接管並發與非同步，並設定連線池參數（如 pool_pre_ping、pool_recycle）以避免閒置斷線。
- JSON 欄位與資料序列化：高動態記憶體與設定（如 characters_json, milestones_json, entities_json 等）一律採用 SQLAlchemy 的 JSON 型態，由 Repository 層負責讀寫時的序列化與反序列化。不得依 JSON 欄位內容做篩選查詢（SQLite 與 SQL Server 的 JSON 查詢語法不同，會破壞雙軌相容）。
- 字串欄位編碼：一般文字欄位一律使用 SQLAlchemy 的 Unicode / UnicodeText 型別（SQL Server 對應 NVARCHAR），確保雙軌環境中文不亂碼。
- 唯一約束與大小寫：SQLite 的字串比對預設區分大小寫、SQL Server 預設不區分。具唯一約束的字串（如 email）一律在 Service 層正規化（email 轉小寫）後再寫入與查詢，使兩種資料庫行為一致。
- 可為空的唯一欄位：SQL Server 的 UNIQUE 約束只允許一個 NULL，SQLite 則允許多個，直接設定 unique=True 會讓雙軌行為分歧。可為空且需要唯一的欄位一律改用「只涵蓋非 NULL 列」的部分唯一索引（sqlite_where 與 mssql_where，以 ORM 表達式建立，不得使用原生 SQL 字串；專案提供 unique_when_not_null()），且該欄位本身不再設 unique=True。
- 對外識別碼：對外暴露的資源識別碼（網址與 API）使用 UUID（如 public_id），內部整數主鍵不對外；但授權不得依賴識別碼的不可猜測性（見 [auth-security.md](auth-security.md)）。

## 資料表變更 (Migration)

- 使用 Alembic（啟用 render_as_batch 以相容 SQLite），正式環境嚴禁使用 create_all() 建表。
- migration 腳本使用 Alembic op API 與 ORM 模型，不得含方言專屬的原生 SQL（含 op.execute 的原生語法）。
- alembic.ini 與 migration 範本 (script.py.mako) 必須是純 ASCII：Alembic 以系統地區編碼讀取它們（繁體中文 Windows 為 cp950），中文字元會造成解析失敗。alembic.ini 不得含連線字串，連線一律取自 DATABASE_URL；migration 腳本 (.py) 不受影響，仍使用 UTF-8 與繁體中文註解。
- 升級採 expand/contract：新增欄位先設為可為空，部署相容新舊結構的程式後再回填與加上約束；任何版本的程式碼都必須能在「目前結構」與「下一版結構」上運作。
- migration 為部署流程中獨立的步驟，於新版程式上線之前執行（執行前先備份），不得在應用程式啟動時自動執行。

## 連線設定

- 連線帳號與密碼僅存於 DATABASE_URL（`.env` 或平台機密），不得寫入程式碼、模板、文件或版本庫（見 [deployment.md](deployment.md)）。
- 本機開發的 SQL Server 連線可設定 TrustServerCertificate=yes；正式環境嚴禁使用，且不得使用 sa 帳號。

相關：RPG 原始劇本 `scripts.milestones_json` 為唯讀（見 [business-encoding.md](business-encoding.md)）。
