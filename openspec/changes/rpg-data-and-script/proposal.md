# Proposal

## Why

RPG 是這個平台的另一半，但目前資料庫裡完全沒有劇本、存檔與回合的資料表，後續的回合引擎（RC-2）、遊戲介面（RC-3）、玩家創作（RC-7）與公開發佈（RC-8）都沒有地方放資料。2026-11-03 的規範修訂（AGENTS.md 第 5、7、10、11 節，commit `bff4109`）又改變了這一層的前提：玩家可以按固定規格創作並公開分享劇本、已發佈的版本不可變、存檔開局時複製劇本、存檔可以「已完結」，內容分級也從 `sexual_content_level` 0 至 3 改為安全／一般／成人三種模式。這些規範目前只存在於文件，程式與測試仍是舊的 0 至 3 等級。

這個 change 先把**資料、規格驗證與內容模式的基礎**做好，讓後面的 change 有穩固的地基，也讓規範與程式重新一致。

## What Changes

- **新增資料表（遷移 `0012`，只新增、不動既有資料）**：`scripts`（劇本的身分、作者、種子鍵、筆名快照）、`script_versions`（每個版本的內容；已發佈的版本不可變、每個劇本最多一個上架版本與一個進行中的版本）、`script_version_tags`（標籤，要能篩選所以不放進 JSON）、`sessions`（存檔，含劇本內容的複本、世界狀態、記事簿欄位、版本號、內容模式、「已完結」狀態）、`memory_logs`（回合，含前端產生的唯一編號與回合狀態）。作者與使用者外鍵一律 `ON DELETE CASCADE`（失敗時偏向刪除）。
- **固定規格 v1 的驗證器**：把 AGENTS.md 第 7 節與 RPG.md 定下的欄位、長度、數量、列舉值、里程碑觸發（玩家年齡或遊戲內年份，搭配劇本曆法）、結局模式、總量上限、「圖片欄位非空一律拒絕」寫成單一個純函式，輸出標準化後的內容或逐欄位的錯誤。
- **內建劇本種子**：`flask seed-scripts` 以穩定的種子鍵冪等地建立首發的古風仙俠劇本；內容改變時新增一個版本取代舊版，不修改已發佈的版本。
- **內容模式取代等級 0 至 3（併入原先規劃的 content-tier-modes）**：`AccessPolicy` 改為 `safe`／`standard`／`adult` 三種模式，實際生效取玩家設定、劇本宣告與年齡上限三者最保守者；成人模式要同時具備部署開關、書面許可涵蓋的模型、玩家的成年確認（勾選「我確定已滿 18 歲」與安全規範，記錄時間、版本與方式）。新增 `GET／PUT /api/account/content-mode`。
- **BREAKING（設定）**：環境變數 `SEXUAL_CONTENT_LEVEL3_ENABLED`、`SEXUAL_CONTENT_LEVEL3_MODELS` 改名為 `ADULT_MODE_ENABLED`、`ADULT_MODE_MODELS`；舊名稱視為已淘汰（忽略並只警告一次，部署計畫檔含有即被拒絕），因此不會因為改名而意外開啟成人模式。staging 與 production 不再一律禁止開啟，改為與 AGENTS.md 一致：預設關閉、須明確設定才生效、須列出許可涵蓋的模型。
- **BREAKING（內部介面）**：`AccessPolicy.content_ceiling` 的回傳型別與 `adult_ceiling` 建構參數改變；`users` 新增內容模式偏好與成年確認的欄位，既有帳號依確認當時是否成年回填（成年為 `standard`，其餘 `safe`），所以**滿 18 歲不會自動調高**。
- **生命週期銜接**：個人資料匯出納入劇本與存檔（含記事簿與回合）；資料保存期限納入存檔（最後活動後 12 個月）與中斷回合的部分內容（14 天）；抹除與刪除帳號的守門測試擴充到「存檔 → 回合」「劇本 → 版本 → 標籤」這幾層串聯。
- **規範字句微調**：AGENTS.md 第 7 節與技能鏡像的「`scripts.milestones_json` 為唯讀」改為「劇本版本的 `milestones_json`」，與資料表實際的位置一致。
- **不做**（各有後續的 change）：暱稱與初次設定頁（RC-7）、玩家建立與編輯劇本的 API 與表單、硬性檢查（RC-7）、管理員審核佇列、公開目錄與搜尋、檢舉、「匿名保留我已發佈的劇本」的偏好與抹除分支（RC-8）、回合引擎與 SSE 端點、劇情輸出的審查層（RC-2）、遊戲與內容模式的介面（RC-3）、性格原型庫三張資料表（RC-5）、代幣錢包與訂閱（尚未探索）。

## Capabilities

### New Capabilities

- `script-fixed-spec`: 劇本固定規格 v1 的欄位、上限、標準化與逐欄位錯誤，是玩家創作與內建種子共用的資料合約。
- `script-store`: 劇本與版本的資料行為：身分與擁有者、版本狀態與不可變、每個劇本至多一個上架版本與一個進行中的版本、標籤、讀取範圍（私人內容必須帶擁有者、公開只讀已上架的版本）、刪帳號的串聯。
- `rpg-sessions`: 存檔與回合的資料行為：開局時複製劇本內容、擁有者隔離、世界狀態與記事簿欄位、版本號、回合的唯一性與冪等、中斷回合不計輪、「已完結」的存檔不得再新增回合。
- `content-tier-modes`: 安全／一般／成人三種內容模式、玩家偏好、成人模式的可用條件與成年確認紀錄、滿 18 歲不自動調高、部署層設定與已淘汰變數、內容模式 API。
- `builtin-script-seed`: 以種子鍵冪等建立首發劇本與內容變動時的版本更替。

### Modified Capabilities

- `age-confirmation`: 移除「未成年者的安全模式與內容上限」「等級 3 預設關閉且只能以環境設定開啟」「開啟等級 3 時須記錄許可涵蓋的模型」三項（由 `content-tier-modes` 取代）；新增「年齡確認時決定預設內容模式」。
- `launch-gates`: 上線前關卡中的「成年驗證方式（若要開放分級 3）」改為成人模式的成年確認方式。
- `account-deletion`: 抹除的範圍納入劇本、版本、標籤、存檔與回合。
- `personal-data-export`: 匯出的範圍與 CSV 檔案清單納入劇本與存檔。
- `data-retention`: 新增存檔的 12 個月保存期限與中斷回合部分內容的 14 天保存期限。

## Impact

- **資料庫**：遷移 `0012_rpg_data_and_script`（5 張新表、`users` 新增 4 欄、一次小型的資料回填）；SQLite 與 SQL Server 雙軌都要通過；`recreate="never"` 的降級說明沿用 `0010` 的寫法。
- **程式**：`app/models/`（新模型、`users` 新欄位）、`app/repositories/`（劇本、存檔、保存期限、抹除銜接）、`app/services/`（規格驗證器、`AccessPolicy`、內容模式服務、存檔服務、種子服務、匯出）、`app/controllers/account_api_controller.py`（內容模式端點）、`app/config.py` 與 `registry.py`（`ADULT_MODE_*`）、`app/cli.py`（`seed-scripts`）、`app/data/scripts/`（種子內容）。
- **測試**：`tests/test_access_policy.py`、`tests/test_content_ceiling_switch.py`、`tests/test_age_gate_api.py` 與其測試輔助、`tests/test_config_rpg_llm.py` 改為新語意；`tests/architecture/` 的抹除與外鍵守門測試擴充；新增劇本、存檔、規格驗證、種子與內容模式的測試。
- **文件與規範**：`.env.example`、README（環境變數表與端點表）、`docs/`（資料模型、系統架構、帳號與安全、營運與部署、模型呼叫層、路線圖、RPG.md）、AGENTS.md 與技能鏡像的一處字句。
- **不影響**：客服（用量額度、限流與提示詞）、登入、管理員後台、既有的對話資料與匯出的既有部分。
