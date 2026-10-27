# offline-frontend-assets Specification

## Purpose
定義前端資產與字型必須完全本地化：頁面載入時不得連線任何外部網站，並以可驗證的方式禁止 CDN、外部字型與不允許的前端技術，使系統可在離線環境運作。

## Requirements

### Requirement: 第三方前端庫由本機提供
Vue 3、Bootstrap 5.3 與 Axios SHALL 以檔案形式存放在本機靜態資源目錄並由應用程式自行提供，頁面 SHALL NOT 從任何外部網址載入腳本或樣式。

#### Scenario: 載入頁面
- **WHEN** 瀏覽器載入任一頁面
- **THEN** 所有腳本與樣式的請求都指向本站，沒有任何請求送往外部主機

#### Scenario: 資產檔案存在
- **WHEN** 請求本機提供的 Vue、Bootstrap 或 Axios 檔案
- **THEN** 回應為 200 且內容為對應的函式庫

### Requirement: 字型由本機提供
系統 SHALL 以 Noto Sans TC 作為預設字型，字型檔為依字元範圍切片的 woff2，存放於本機字型目錄；後備字型 SHALL 為 Microsoft JhengHei 與 PingFang TC。系統 SHALL NOT 使用任何外部字型服務。

#### Scenario: 顯示繁體中文
- **WHEN** 頁面顯示繁體中文
- **THEN** 字型來自本站的 woff2 檔案，且字型檔只在需要的字元範圍被請求

#### Scenario: 字型檔載入失敗
- **WHEN** 本機字型檔無法載入
- **THEN** 文字改用後備字型顯示，頁面版面仍可使用

### Requirement: 頁面資源一律以靜態路徑函式引用
樣板中對靜態資源的引用 SHALL 一律透過框架提供的靜態網址產生方式，SHALL NOT 寫死絕對網址或指向外部主機的網址。

#### Scenario: 樣板檢查
- **WHEN** 掃描所有樣板的資源引用
- **THEN** 沒有 `http://` 或 `https://` 開頭的資源網址，且靜態資源皆使用靜態網址產生方式

### Requirement: 禁用的前端技術
專案 SHALL NOT 包含 Vue 單檔元件（`.vue`）、前端打包工具設定、CDN 引用、外部字型服務、外部彈窗套件，也 SHALL NOT 載入 Bootstrap 的 JavaScript。樣板 SHALL NOT 使用 `data-bs-*` 屬性，唯一例外為 `data-bs-theme="dark"`。

#### Scenario: 專案檢查
- **WHEN** 對專案執行禁用技術檢查
- **THEN** 找不到 `.vue` 檔案、打包工具設定、CDN 網址、外部字型網址與外部彈窗套件

#### Scenario: Bootstrap JavaScript
- **WHEN** 檢查頁面載入的腳本
- **THEN** 不含 Bootstrap 的 JavaScript，僅含其樣式

#### Scenario: data-bs 屬性
- **WHEN** 掃描所有樣板的 `data-bs-*` 屬性
- **THEN** 只有 `data-bs-theme="dark"`

### Requirement: 資產清單可驗證
系統 SHALL 維護一份資產清單，記錄每個本機資產的名稱、版本、來源網址、授權與校驗碼；清單中的校驗碼 SHALL 與實際檔案一致。

#### Scenario: 校驗資產
- **WHEN** 重新計算每個資產檔案的校驗碼
- **THEN** 與資產清單記錄的值一致

#### Scenario: 檔案被改動
- **WHEN** 某個資產檔案內容被修改
- **THEN** 校驗失敗並指出該檔案
