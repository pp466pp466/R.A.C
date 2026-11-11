# Proposal

## Why

AGENTS.md 第 7 節已規定「故事偏好」（男頻、女頻、全讀者，只影響劇本目錄排序）與劇本的「讀者頻道」（`audience`，單選，既有劇本視為全讀者），但程式裡兩者都還不存在：使用者表沒有偏好欄位、劇本規格沒有 `audience`、作者工作坊沒有單選、劇本清單查詢也沒有排序依據。兩邊缺一不可：只有玩家欄位則排序沒有東西可比，只有劇本欄位則沒有人可以選。

RC-3a（選劇本頁）要顯示可玩劇本清單，排序規則要在那之前就位，否則清單做出來之後還得回頭改排序與規格。RC-3 探索（`docs/RPG.md` 7.17）已決定把它獨立成這個 change，排在 RC-3a 之前，且不需要修訂 AGENTS.md。

## What Changes

- **玩家的故事偏好**：使用者表新增 `story_preference`（`all`／`male`／`female`，預設 `all`）；新增 `GET`／`PUT /api/account/story-preference`；`/account` 新增「故事偏好」區塊（三選一單選，儲存成功以 Toast 回報）。偏好**只影響劇本清單的排序**，不影響內容分級、敘事與任何權限。
- **劇本規格新增 `audience`**：固定規格的選填欄位，`all`（全讀者，預設）、`male`（男頻）、`female`（女頻）；缺值視為 `all`。**規格版本維持 1**（選填且向下相容）。標準化時 `all` 與缺值一律不寫進內容，所以既有內容的 `content_hash` 不變：既有草稿、待審的審查結果不失效，內建劇本重跑種子仍是「未變更」，不會憑空多出一個新版本。
- **版本內容與不可變**：`script_versions` 新增可為空的 `audience` 欄位；它屬版本內容，已發佈的版本不可變，修改一律產生新版本並重新送審（沿用既有機制，不新增流程）。**不回填舊版本**（NULL 視同全讀者）。
- **作者工作坊**：表單最下方新增「讀者頻道」單選（男頻、女頻、全讀者），預設全讀者。
- **清單排序的依據**：新增純函式的排序規則（符合偏好者最前，其次全讀者，其餘最後；偏好為全讀者則不重排）；`ScriptRepository.list_published` 與 `list_published_by_tag` 新增選填的 `preference` 參數，用 ORM 表達式排序，同層仍依發佈時間由新到舊。目前沒有玩家端點呼叫它們，由 RC-3a 的可玩劇本清單使用。
- **個人資料匯出**：帳號資料含故事偏好，劇本版本含讀者頻道。
- **遷移 `0017`**（expand）：兩個新欄位，皆為舊版程式不認識也照常運作的形狀（偏好有資料庫預設值、`audience` 可為空）。

## Capabilities

### New Capabilities

- `story-preference`: 玩家的故事偏好（值域、預設、讀取與更新的 API、帳號頁的設定區塊）與劇本清單的排序規則；偏好不影響內容分級、敘事與權限。

### Modified Capabilities

- `script-fixed-spec`: 新增選填的讀者頻道欄位 `audience`（值域、缺值視為全讀者、預設值不進標準化內容）。
- `script-store`: 版本記錄讀者頻道（可為空、屬版本內容而不可變、不回填）；公開清單的讀取可依偏好排序。
- `script-authoring`: 創作頁面的編輯器新增讀者頻道單選。
- `personal-data-export`: 匯出納入故事偏好與劇本版本的讀者頻道。
- `builtin-script-seed`: 加入 `audience` 之後，既有內建劇本重跑種子仍是未變更。

## Impact

- **程式**：`app/models/user.py`、`app/models/script.py`；`app/repositories/user_repository.py`、`script_repository.py`；`app/services/script_spec.py`、`current_user.py`、`personal_export_service.py`、`registry.py`，新增 `story_preference` 的 Service 與純函式模組；`app/controllers/account_api_controller.py`；前端 `app/templates/auth/account.html`、`app/static/js/auth/account.js`、`app/templates/scripts/workshop.html`、`app/static/js/scripts/script-draft.js`、`script-workshop.js`。
- **資料庫**：遷移 `0017`（`users.story_preference` NOT NULL 預設 `'all'`、`script_versions.audience` 可為空）。**部署順序**：備份 → 遷移 → 新版程式；Azure 與本機 `project` 目前在 0016，新程式上線前必須先升級。
- **文件**：`docs/project資料結構文件.md`（第 5 節、第 6 節固定值、第 14 節異動紀錄）、`docs/資料模型-data-model.md`、`docs/帳號與安全-auth-security.md`、`docs/系統架構-architecture.md`、`docs/RPG.md` 7.17、`docs/路線圖-roadmap.md`，依 `docs/README.md` 對照表。
- **不涉及**：AGENTS.md（規則已寫在第 7 節）、LLM 呼叫與提示詞（偏好與頻道不進模型）、遊戲幣、日誌白名單欄位（只用既有的事件名稱與使用者編號）。
- **不做**：競品畫面 5 的「喜歡的類型」（圖片類型多選）、依頻道篩選或隱藏劇本、RC-3a 的清單端點與畫面、`/age` 精靈中的偏好步驟（屬 `onboarding-polish`）。
