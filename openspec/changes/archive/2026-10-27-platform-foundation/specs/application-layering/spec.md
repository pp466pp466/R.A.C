# Spec Delta

## Purpose

定義後端的關注點分離：頁面只回 HTML、API 只回 JSON、依賴方向單向、錯誤回應統一為繁體中文且不洩漏內部資訊，並讓應用程式可在反向代理後正確運作。

## ADDED Requirements

### Requirement: 應用程式可依設定獨立建立
系統 SHALL 能依指定環境建立彼此隔離的應用程式實例，使測試可同時建立多個互不影響的實例。

#### Scenario: 建立兩個實例
- **WHEN** 以 `testing` 設定連續建立兩個應用程式實例
- **THEN** 兩者各自擁有獨立的設定與資料庫連線，彼此的資料不互相影響

### Requirement: 頁面端點只回傳 HTML
頁面端點 SHALL 只負責渲染頁面外殼與初始狀態，回應內容類型為 HTML，不處理資料操作。

#### Scenario: 開啟頁面
- **WHEN** 瀏覽器請求一個頁面路徑
- **THEN** 回應為 HTML，且包含 UTF-8 字元集宣告與 `zh-Hant-TW` 語言標註

### Requirement: API 端點一律回傳 JSON
API 端點 SHALL 一律回傳 JSON，包含錯誤情況；僅 SSE 串流端點（RPG 適用，不在本 change 範圍）可例外。

#### Scenario: API 成功回應
- **WHEN** 呼叫任一 API 端點並成功
- **THEN** 回應內容類型為 JSON

#### Scenario: API 找不到資源
- **WHEN** 請求 `/api/` 底下不存在的路徑
- **THEN** 回應狀態為 404，內容為 JSON，而不是 HTML 錯誤頁

### Requirement: 層級依賴方向單向
依賴方向 SHALL 為：頁面與 API 端點 → Service → Repository → 資料模型。端點 SHALL NOT 直接存取資料庫工作階段或 Repository；Repository SHALL NOT 依賴 Service 或端點；Service SHALL NOT 依賴 HTTP 請求物件。

#### Scenario: 端點直接使用 Repository
- **WHEN** 對端點程式碼執行層級依賴檢查，而端點直接匯入 Repository
- **THEN** 檢查失敗並指出違規的檔案

#### Scenario: 依賴方向正確
- **WHEN** 所有層級只依照規定方向依賴
- **THEN** 檢查通過

### Requirement: 錯誤回應統一且不洩漏內部資訊
未處理的錯誤與找不到資源 SHALL 以繁體中文回應，API 回應含機器可讀的錯誤代碼與訊息，頁面回應為簡潔的錯誤頁。回應 SHALL NOT 包含堆疊追蹤、檔案路徑、SQL 或機密。

#### Scenario: API 發生未預期錯誤
- **WHEN** API 處理中發生未預期例外
- **THEN** 回應狀態為 500，JSON 含錯誤代碼與繁體中文訊息，且不含堆疊追蹤

#### Scenario: 頁面找不到
- **WHEN** 請求不存在的頁面路徑
- **THEN** 回應 404 與繁體中文的錯誤頁

### Requirement: 反向代理標頭只在預備與正式環境受信任
在 `staging` 與 `production`，系統 SHALL 信任前端入口層轉送的協定與主機標頭，使 HTTPS 與 Secure Cookie 判斷正確。在 `development` 與 `testing`，系統 SHALL NOT 信任這些標頭。

#### Scenario: 正式環境在 HTTPS 入口後
- **WHEN** 入口層以 HTTPS 終止連線並轉送 `X-Forwarded-Proto: https`
- **THEN** 應用程式判定請求為 HTTPS，產生的絕對網址使用 `https`

#### Scenario: 開發環境收到偽造標頭
- **WHEN** `development` 收到 `X-Forwarded-Proto: https` 標頭
- **THEN** 應用程式忽略該標頭

### Requirement: 基礎頁面樣板
所有頁面 SHALL 繼承同一個基礎樣板，內含 UTF-8 字元集、`zh-Hant-TW` 語言標註、深色模式屬性 `data-bs-theme="dark"`，以及離線資產的引入。

#### Scenario: 繼承基礎樣板的頁面
- **WHEN** 渲染任一頁面
- **THEN** 輸出的 HTML 含 `<meta charset="UTF-8">`、`lang="zh-Hant-TW"` 與 `data-bs-theme="dark"`
