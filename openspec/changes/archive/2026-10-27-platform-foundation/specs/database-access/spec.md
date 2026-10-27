# Spec Delta

## Purpose

定義資料庫存取的雙軌（SQLite 與 SQL Server）行為：依方言初始化連線、全面使用 ORM 而不寫原生 SQL、資料表只能透過版本化 migration 變更，並確保中文與跨資料庫的唯一性行為一致。

## ADDED Requirements

### Requirement: 同時支援 SQLite 與 SQL Server
系統 SHALL 依資料庫連線字串自動選擇 SQLite 或 SQL Server，兩者使用相同的資料模型與資料存取程式碼，業務行為不得因資料庫種類而不同。

#### Scenario: 連線 SQL Server
- **WHEN** 連線字串指向 SQL Server 且資料庫可連線
- **THEN** 系統可建立連線並完成讀寫

#### Scenario: 連線記憶體 SQLite
- **WHEN** 連線字串指向記憶體 SQLite
- **THEN** 系統可建立連線並完成讀寫，行為與 SQL Server 一致

### Requirement: 連線初始化依方言隔離
僅在資料庫為 SQLite 時，系統 SHALL 於每條新連線建立時自動啟用預寫日誌模式、外鍵強制檢查與忙碌等待逾時。資料庫為 SQL Server 時，系統 SHALL NOT 執行上述任何 SQLite 專屬設定。業務程式碼不得自行執行這些設定。

#### Scenario: SQLite 新連線
- **WHEN** SQLite 建立一條新連線
- **THEN** 該連線的日誌模式為預寫日誌、外鍵檢查為開啟、忙碌等待逾時大於零

#### Scenario: SQLite 外鍵被違反
- **WHEN** 寫入的資料違反外鍵關聯
- **THEN** 寫入被拒絕並回報完整性錯誤，而不是靜默成功

#### Scenario: SQL Server 連線
- **WHEN** 資料庫為 SQL Server 並建立新連線
- **THEN** 不執行任何 SQLite 專屬設定，且不產生相關錯誤

### Requirement: SQL Server 連線池必須耐受閒置斷線
針對 SQL Server，系統 SHALL 在取用連線前確認其可用，並定期回收閒置過久的連線，使資料庫端關閉閒置連線後下一個請求仍可成功。

#### Scenario: 資料庫端斷開閒置連線
- **WHEN** 連線池中的某條連線已被資料庫端關閉，之後來了新請求
- **THEN** 系統丟棄失效連線並改用新連線，請求成功完成

### Requirement: 資料存取全面使用 ORM
業務邏輯與 Repository 層 SHALL 只透過 ORM 模型與查詢建構操作資料，SHALL NOT 出現任何原生 SQL 字串。唯一例外是連線初始化的 SQLite 專屬設定。

#### Scenario: 靜態檢查
- **WHEN** 對應用程式原始碼執行原生 SQL 檢查
- **THEN** 除連線初始化的例外位置外，找不到任何原生 SQL

### Requirement: 資料存取的交易生命週期
每個需要資料庫的請求 SHALL 使用單一工作階段：處理成功時提交，處理失敗時回復，且無論結果如何，結束時一定釋放連線。

#### Scenario: 處理成功
- **WHEN** 請求順利完成
- **THEN** 變更被提交，連線被歸還連線池

#### Scenario: 處理中發生例外
- **WHEN** 請求處理中拋出例外
- **THEN** 該請求所有未提交的變更被回復，連線被歸還連線池

### Requirement: 中文與結構化資料可無損往返
文字欄位 SHALL 使用可儲存 Unicode 的型別，結構化資料 SHALL 使用 JSON 欄位；兩種資料庫上寫入後讀出的內容 SHALL 與原值完全相同。系統 SHALL NOT 依 JSON 欄位內容做篩選查詢。

#### Scenario: 繁體中文往返
- **WHEN** 寫入包含繁體中文與表情符號的文字再讀出
- **THEN** 內容與原值逐字相同，在 SQLite 與 SQL Server 上皆然

#### Scenario: JSON 往返
- **WHEN** 寫入含中文鍵值的巢狀結構再讀出
- **THEN** 結構與內容與原值相同

### Requirement: 唯一字串的比對在兩種資料庫行為一致
具唯一約束的文字（例如 email）SHALL 經過同一套正規化後才寫入與比對，使「只差大小寫」的兩個值在 SQLite 與 SQL Server 上都被視為重複。

#### Scenario: 只差大小寫的重複值
- **WHEN** 先寫入 `Alice@Example.com`，再寫入 `alice@example.com`
- **THEN** 第二次寫入因重複被拒絕，在兩種資料庫上結果相同

#### Scenario: 可為空的唯一欄位
- **WHEN** 多筆資料的該欄位皆為空值，之後再寫入兩筆相同的非空值
- **THEN** 多筆空值都被允許，第二筆相同的非空值被拒絕，在兩種資料庫上結果相同

### Requirement: 資料表只能透過版本化 migration 變更
資料表結構 SHALL 只透過版本化 migration 建立與變更，且 SHALL 提供基線 migration。應用程式啟動時 SHALL NOT 自動建立資料表，也 SHALL NOT 自動執行 migration。Migration SHALL NOT 包含方言專屬的原生 SQL。

#### Scenario: 對空資料庫套用基線
- **WHEN** 對全新的空資料庫執行升級到最新版本
- **THEN** 升級成功，且在 SQLite 與 SQL Server 上皆然

#### Scenario: 應用程式啟動不改動結構
- **WHEN** 啟動應用程式且資料庫尚未升級
- **THEN** 應用程式不建立任何資料表，也不執行 migration

#### Scenario: Migration 含原生 SQL
- **WHEN** 檢查 migration 腳本
- **THEN** 不存在以原生 SQL 字串執行的語句

### Requirement: 基礎模型提供一致的欄位慣例
系統 SHALL 提供基礎模型慣例：對外識別使用 UUID 欄位、時間以 UTC 儲存、建立與更新時間自動維護，使後續各模組的資料表行為一致。

#### Scenario: 新增資料列
- **WHEN** 新增一筆採用基礎慣例的資料
- **THEN** 對外識別碼自動產生且唯一，建立時間為 UTC 且自動填入

#### Scenario: 更新資料列
- **WHEN** 更新該筆資料
- **THEN** 更新時間自動改為更新當下的 UTC 時間，建立時間不變
