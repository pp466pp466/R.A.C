# Proposal

## Why

玩家創作的劇本現在只走到一半：作者可以建立、編輯、送審（規則層與模型層審查都已實作），送審後版本停在 `pending_review`，但**沒有任何管理員介面能核可或退回**——`ScriptRepository.approve_version` 與 `reject_version` 存在，卻沒有 Service、端點與畫面呼叫它們。所以「先審後發」的公開分享在實際上還不能運作，`published` 的玩家劇本只能由種子（內建劇本）產生。

另外三塊缺口也屬於同一條路：①上架之後沒有**下架**（AGENTS.md 第 7 節已定：作者自己可下架並自行恢復，管理員可下架並只有管理員能恢復）；②入口 `/game` 的目錄是 RC-3a 的最小版（只有卡片與 `offset` 分頁），沒有搜尋、標籤與讀者頻道篩選、詳情頁，劇本一多就找不到；③作者在 `/scripts` 看不到審核結果（退回的類別）與下架狀態。

AGENTS.md 已於 `67ef67b` 修訂這一批的規範（「劇本審核與下架（RPG）」「公開目錄與檢舉（RPG）」）。檢舉與「匿名保留我已發佈的劇本」另立 `rpg-script-reports`（8b），本 change 只做 8a，但資料與端點設計要讓 8b 能直接接上。

## What Changes

- **管理員審核佇列**（`/admin` 新分頁「劇本審核」）：列出待審版本，有疑慮（模型層 `flag`）的排最前、其餘由舊到新；可讀全文（限於送審過的版本，每次讀取寫稽核 `script.read`）；**核可**（`pending_review` → `published`，取代舊上架版本、第一次核可鎖筆名）與**退回**（`pending_review` → `rejected`，附固定類別與只有管理員看得到的審核備註）。核可與退回**不要求重新輸入密碼**，但寫稽核（`script.approve`、`script.reject`）。人工退回的類別沿用現有列舉並新增人工專用的 `other_policy`。
- **下架與恢復**：新增版本狀態 `removed`（只新增）。`published` → `removed`：作者可自行下架（不需重審，自己下架的可自行恢復）、管理員可下架（須重新輸入密碼、附類別與審核備註、寫稽核 `script.remove`，只有管理員能恢復，恢復同樣須密碼並寫稽核 `script.restore`）。下架的版本不能再開新存檔，已開局的存檔不受影響（開局時已複製內容）。恢復時若同劇本已另有上架版本則拒絕；核可新版本時，同劇本的 `removed` 舊版改為 `superseded`（不可再恢復）。內建劇本（沒有作者）v1 不開放下架。
- **作者看得到結果**：`/scripts` 的版本列表與詳細檢視顯示「已退回」（固定類別與時間）、「已下架」（作者自己或管理員，管理員下架時顯示類別）；自己下架的版本有「恢復上架」按鈕；`published` 版本有「下架」按鈕（自製 Modal 確認）。審核備註**絕不**出現在作者看得到的任何回應。不寄信。
- **完整公開目錄**（`GET /api/sessions/catalog`）：新增關鍵字 `q`（只比對標題、摘要、世界名稱、標籤；Service 層 NFC 與不分大小寫正規化）、標籤 `tag`、讀者頻道 `audience` 篩選、**游標分頁取代 `offset`**（`cursor` 與 `next_cursor`），仍保留故事偏好排序；新增 `GET /api/sessions/catalog/tags`（常用標籤與數量）與 `GET /api/sessions/catalog/<版本識別碼>`（詳情，欄位白名單，**不含**隱藏特質、里程碑、結局文字與規則）。
- **詳情頁與入口畫面**：新增 MVC 頁面 `/game/script/<版本識別碼>`（外殼只傳初始狀態）；`/game` 的「選劇本」加搜尋框、標籤晶片、讀者頻道選單與游標式「載入更多」，卡片可點進詳情。「點選未綁定安全模式的劇本時詢問是否修改安全模式」的 Modal 抽成共用，詳情頁的「開始冒險」沿用。
- **資料**：遷移 `0021`（expand-only）為 `script_versions` 新增可為空欄位 `submitted_at`、`removed_by`、`decision_category`、`decision_note`、`decided_at`，並加索引 `(status, published_at)`；沒有回填、沒有新資料表。
- **稽核與日誌**：`AUDIT_ACTIONS` 新增 `script.read`、`script.approve`、`script.reject`、`script.remove`、`script.restore`（稽核**不含**審核備註，備註只隨版本保存與刪除）；`script_events` 新增核可、退回、下架、恢復事件（只有內部編號與固定列舉）。
- **匯出與刪除**：個人資料匯出的劇本版本納入狀態 `removed`、`decision_category`、`decided_at`、`removed_by`，**不含**審核備註（那是管理員的文字，不是作者的資料）；帳號刪除沿用既有串聯。
- **架構守門**：`tests/architecture/test_script_state_writes.py` 擴充，新欄位只能由 `ScriptRepository` 的轉移方法寫入。
- **文件**：`docs/project資料結構文件.md`（含第 14 節異動紀錄）、`docs/資料模型-data-model.md`、`docs/系統架構-architecture.md`、`docs/前端結構-frontend.md`、`docs/RPG.md`、`docs/路線圖-roadmap.md`；簡報衝突依 `deck-conflict-watch` 只記錄。
- **BREAKING**：目錄端點的 `offset` 參數不再被讀取（前端與後端同批改；`has_more` 保留，另加 `next_cursor`）。其餘無。

**不在本 change 內**（屬 8b `rpg-script-reports`）：檢舉、檢舉的管理員檢視與優先度、帳號偏好「匿名保留我已發佈的劇本」、隱私權政策與服務條款對檢舉與匿名保留的補充。

## Capabilities

### New Capabilities

- `admin-script-review`：管理員審核佇列：清單與排序、全文讀取與稽核、核可與退回（類別、審核備註、不需密碼）、稽核動作、不寄信、備註不外洩，以及 `/admin` 的「劇本審核」分頁。
- `script-takedown`：下架與恢復：作者與管理員各自的權限與可恢復範圍、管理員下架與恢復須密碼與稽核、恢復的前置條件、下架對新存檔與既有存檔的影響、對作者的顯示。
- `rpg-script-catalog`：完整公開目錄：關鍵字、標籤與讀者頻道篩選、游標分頁、常用標籤、詳情端點與詳情頁、詳情欄位白名單、`/game` 的搜尋與篩選畫面。

### Modified Capabilities

- `script-store`：版本狀態新增 `removed` 與轉移（`published` ↔ `removed`、`removed` → `superseded`）、核可時取代 `removed` 舊版、決定欄位（類別、備註、時間、下架者）與 `submitted_at`、由 `removed` 版本建立新草稿、讀取範圍（`removed` 只有擁有者與管理員專用路徑可讀）、擁有者不得刪除 `removed`。
- `rpg-sessions`：開存檔的可見性——`removed` 版本不能開新存檔（含作者自己）；創角資料與 AI 生成預覽的可見性「與開存檔相同」，因此隨之生效。
- `rpg-entry-api`：目錄端點由 `offset` 改游標、新增查詢參數與兩個新端點。
- `rpg-entry-pages`：選劇本分頁的搜尋與篩選、安全模式詢問 Modal 共用、詳情頁導覽。
- `script-authoring`：`/scripts` 的版本摘要與頁面顯示退回與下架資訊、新增「下架」「恢復上架」操作。
- `admin-audit-log`：動作列舉新增五項。
- `admin-console`：新增「劇本審核」分頁（外殼規則不變）。
- `personal-data-export`：劇本版本的匯出欄位。
- `service-observability`：劇本審核與下架事件只含白名單欄位。

## Impact

- **程式（後端）**：`app/models/script.py`（狀態、欄位、類別常數）、`app/models/admin_audit_log.py`、新遷移 `migrations/versions/…_0021_script_publishing.py`；`app/repositories/script_repository.py`（核可、退回、下架、恢復、佇列與目錄查詢、游標）；新增 `app/services/admin_script_review_service.py`（或併入 `AdminService`，見 design）、`script_takedown_service.py`、`catalog_service.py`（或併入 `GameEntryService`）；修改 `script_authoring_service.py`、`startable.py`、`personal_export_service.py`、`script_events.py`；控制器：`admin_api_controller.py`、`script_api_controller.py`、`session_api_controller.py`、`game_page_controller.py`；`registry.py` 組裝。
- **程式（前端）**：`admin/console.html`、`admin/admin.js`（新分頁）、`scripts/workshop.html`、`script-workshop.js`、`game/index.html`、`entry.js`、新詳情頁樣板與模組、安全模式詢問 Modal 的共用片段、`game-labels.js`（類別與狀態文案）、`css`。
- **API**：新增 `/api/admin/scripts*`（列表、讀取、核可、退回、下架、恢復）、`/api/scripts/versions/<id>/remove`、`/restore`、`/api/sessions/catalog/tags`、`/api/sessions/catalog/<id>`；修改 `/api/sessions/catalog`、`/api/scripts`（摘要欄位）。
- **資料庫**：遷移 `0021`（expand-only，可為空欄位與一個索引）；須同步 `docs/project資料結構文件.md` 並在第 14 節追加異動紀錄；SQLite 與 SQL Server 雙軌測試。
- **文件**：見上；最後一項任務依 `docs/README.md` 的對照表更新設計文件並讓 `tests/test_design_docs.py` 通過。
- **風險**：①目錄的 `LIKE '%…%'` 搜尋在劇本很多時不走索引（v1 規模可接受，詞數與長度有上限；若成問題另立 change）；②游標的穩定性依賴 `(偏好層, published_at, id)` 三鍵，恢復上架不改 `published_at`；③「核可」不再讀一次模型，依賴送審時的審查結果（內容在 `pending_review` 已凍結）；④稽核與日誌絕不能帶審核備註（以守門測試與事件函式簽名保證）；⑤`removed` 狀態是新的列舉值，所有「依狀態分支」的程式都要檢查（`latest_closed_version`、`get_startable`、匯出、`list_owned` 的畫面）。
