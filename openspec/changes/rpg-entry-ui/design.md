# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/`。這份只講怎麼做與為什麼。

現況（已讀過程式）：

- **後端已齊**：`/api/sessions/setup/<id>`、`/api/sessions/protagonist/suggest`、`POST /api/sessions`（開存檔）與 `/api/game/sessions/<id>` 系列都已存在並有測試；`ScriptRepository.list_published(..., preference=…)` 與 `SessionRepository.list_owned` 存在，但沒有玩家端點使用。**沒有任何 `/game` 頁面**，`DEFAULT_AFTER_LOGIN` 仍是 `/cs`。
- **前端慣例**：每頁一個 Jinja 樣板（`base.html` 繼承）加 `app/static/js/<區>/` 的腳本；初始狀態以 JSON 區塊傳入（`Auth.readInitialState`）；`Auth.describeError` 把 axios 錯誤整理成 `{kind, message, fields, code, status}`；Modal、Toast、受限狀態提示是 `UI.*` 的共用元件；可單元測試的邏輯寫成 UMD 純函式模組（如 `script-draft.js`），以 `node --test` 測；掛載到 Vue 的 `setup()` 以 `vm` 沙盒測（如 `account-story-preference.test.js`）。
- **閘門慣例**：`GET /api/scripts` 等「會呼叫 AI 的流程」走 `require_chat_access`；`/scripts` 頁面則把 `chat_gate` 的結果放進初始狀態讓前端顯示提示。尚未完成年齡確認的**管理員**沒有受限狀態，但過不了 `chat_gate`。
- **既有的超時建議**：`session_api_controller` 的說明寫明 `POST /api/sessions` 最壞約 30 秒（劇本尚未審查過時再加一次），前端逾時應為 65 秒並於離開頁面時中止。

## Goals / Non-Goals

**Goals：**

- 玩家登入後能自己點著走完「選劇本 → 序章 → 聯繫 → 創角（含 AI 生成）→ 開始冒險 → 看到存檔頁」，用現有後端、不改任何遊戲規則。
- 清單端點的查詢次數固定，不因筆數增加（避免逐筆查標籤、作者與輪數）。
- 創角內容（個資）不離開頁面記憶體；清單與頁面不洩漏他人的資料或未上架內容。
- RC-3b 與 RC-8 可以直接沿用：存檔頁網址已定、清單端點日後可被完整目錄取代而畫面不必重寫。

**Non-Goals：**

- 遊戲主畫面、串流、第 0 幕、記事簿面板、餘額與簽到（RC-3b）；完整目錄、搜尋、標籤篩選、劇本詳情頁（RC-8）；刪除或封存存檔的介面。
- 任何資料表變更、遷移、新環境變數、新日誌事件。
- 試玩自己的草稿（後端的 `get_startable` 允許，但入口清單只列上架版本；作者要試玩另行處理）。

## Decisions

### D1 網址：`/game` 是入口，`/game/start/<版本>` 是精靈，`/game/<存檔>` 是遊戲頁

AGENTS.md 第 3 節把 `/game` 與 `/cs` 並列為兩套視圖，所以 `/game` 是 RPG 的首頁（選劇本與我的存檔）；開存檔精靈是它底下的 `/game/start/<版本識別碼>`；RC-3b 的遊戲主畫面會是 `/game/<存檔識別碼>`，本 change 先放空殼。兩者的路徑段數不同（`/game/start/<x>` 兩段、`/game/<x>` 一段），不會互相吃掉；但存檔識別碼是 UUID，不會等於 `start`，兩者的 Flask 路由用不同的規則字串，由測試固定。

- 備選：入口放 `/play`、遊戲放 `/game`。拒絕：多一個頂層路徑，且與 AGENTS.md 把 `/game` 當 RPG 視圖的寫法不符。這同時回答了 `docs/RPG.md` 7.17.4 留給 RC-3b 的「`/game` 的網址設計」：**遊戲頁是 `/game/<存檔識別碼>`**，RC-3b 沿用。

### D2 端點放在 `/api/sessions` 底下，新增 `GameEntryService`（唯讀）

`GET /api/sessions/catalog` 與 `GET /api/sessions`（與既有的 `POST /api/sessions` 同路徑不同方法）。`/api/sessions/` 已在 `cache_headers` 的禁止快取前綴內，`setup`、`suggest` 也在這裡，創角前後的端點集中一處。`/api/sessions/<x>` 目前沒有任何 GET 路由，所以 `catalog` 不會與參數路由衝突。

組裝清單檢視的邏輯放進新的 `app/services/game_entry_service.py`（`GameEntryService`），而不是塞進已經很大的 `SessionService`（開存檔流程）。它只讀、不呼叫模型、不動錢包，依賴 `ScriptRepository`、`SessionRepository`、`AccessPolicy`。存檔空殼頁也用它的 `session_summary`（擁有者條件、404、不過閘門）。

- 備選：沿用 `GET /api/scripts` 加參數。拒絕：該端點是作者自己的劇本，語意與權限都不同。
- 備選：`/api/catalog/scripts` 新藍圖。拒絕：RC-8 才需要獨立的目錄，現在多一個藍圖與快取前綴只是增加表面。

### D3 清單端點不過 `require_chat_access`

`catalog` 與 `GET /api/sessions` 只要 `require_login()`（受限登入狀態照舊被擋）。理由：`/game` 是**預設登入頁**，尚未完成年齡確認的管理員登入後也會落在這裡；若清單端點拒絕他們，預設頁面就是一片錯誤。清單不呼叫模型、內容模式對尚未確認年齡者本來就是安全，所以放行是安全的；真正會花成本的「開始」由 `POST /api/sessions` 的既有閘門擋，頁面也依初始狀態的 `gate` 禁用按鈕並提示。

- 備選：照 `list_mine` 的做法（全過閘門）。拒絕：會讓管理員的預設頁面壞掉，也與 `AccessPolicy.chat_gate` 說明中「列表端點不過閘門」的原意相反。

### D4 Repository：固定查詢次數

- `ScriptRepository` 新增 `list_published_cards(max_tier, *, limit, offset, preference)`：以 `select(ScriptVersion, Script).join(Script, …)` 一次取回版本與劇本（筆名、短代號），條件與排序抽成與 `list_published` 共用的私有方法，所以兩者的篩選與排序不會分歧；另新增 `tags_of_versions(version_ids)` 一次用 `IN` 取回多個版本的標籤，Service 再依版本分組。標籤依既有 `tags_of` 的順序規則。`list_published` 原樣保留。
- `SessionRepository` 新增 `complete_turn_counts(user_id, session_ids)`：以 ORM 的 `GROUP BY` 計各存檔 `turn_status == complete` 的回合數，並以 `sessions.user_id` 為條件（沿用「Repository 方法簽名必接 `user_id`」）；沒有回合的存檔以 0 補。`list_owned` 加 `include_archived=False` 呼叫。
- 存檔的標題取自 `script_snapshot_json["title"]`：JSON 欄位不能做篩選，但**讀出後取值**是允許的；代價是每列會載入整份劇本複本（有總量上限）。分頁預設 20、至多 50，可接受；見「風險」。

**分頁**：`limit + 1` 筆取回，多出的一筆只用來決定 `has_more`（與 `turn_views` 相同）。參數驗證走 `ValidationError`（400，帶欄位），上限常數放在 Service，由測試固定。

### D5 前端結構：三個頁面、少量純函式模組

```
app/templates/game/{index,start,session}.html
app/static/js/game/
  game-api.js        axios 包裝（沿用 script-api.js 的形狀：固定路徑、encodeURIComponent、describeError）
  game-labels.js     內容模式、讀者頻道、來歷、審查類別的繁體中文對照（純資料）
  entry.js           /game 的 Vue 掛載（分頁、載入更多、重試）
  wizard-state.js    UMD 純函式：空白草稿、欄位字數、前進條件、示範氣泡、建立開存檔 payload、API 錯誤→畫面訊息
  wizard.js          /game/start 的 Vue 掛載
app/static/css/game.css
```

- `wizard-state.js` 與 `game-labels.js` 是純函式／純資料，用 `node --test` 全面測；`entry.js` 與 `wizard.js` 用 `vm` 沙盒測 `setup()`（失敗保留、`finally` 恢復、重複點擊只送一次、離開時中止）。沒有瀏覽器可以掛載，這是與前面 change 相同的證明方式；瀏覽器實測另列任務，由使用者親自看。
- 顯示用的繁體中文對照（「安全模式」「男頻」「穿越者」、審查類別名稱）集中在 `game-labels.js`，後端只回列舉值，沿用 `script-draft.js` 的做法。
- 共用元件沿用 `UI.Modal`／`UI.toast`／`ui-restriction-notice`，不新增元件。

### D6 精靈是單頁、狀態只在記憶體

三個步驟在同一頁以 `step` 響應式變數切換（`v-if`），不是三個伺服器頁面：創角內容不能放網址，也不寫瀏覽器儲存（個資，見 AGENTS.md 第 11 節），所以重新整理就回到序章是**刻意**的行為（spec 已寫明）。步驟的前進條件只做「必要欄位已填、未超過長度」這類即時回饋；**合規與審查全由伺服器判斷**，前端不重複實作規則（避免兩邊分歧，也不洩漏規則）。

資料流：

```
setup(id)  ──► 序章（背景）──► 聯繫（anchor：對象＋一句／略過）──► 創角（card＋origin＋自我介紹）
                                                                    │ 請 AI 生成 → suggest → 填入 card＋anchor（預覽）
                                                                    └─ 開始冒險 → POST /api/sessions {version_id, protagonist, anchor|skip_anchor}
                                                                                        └─ 201 → location = /game/<id>
```

- AI 預覽回傳的角色卡不含來歷（後端刻意如此）：玩家選的來歷保持不變；回傳的錨定事實**覆蓋**聯繫步驟的內容，所以已有手動內容時先以 Modal 確認。預覽只是填表，沒有「接受」的伺服器動作；玩家修改後按「開始冒險」才寫入。
- `anchor` 在前端有三態：未設定、已設定（kind、ref、text）、略過。「未設定」時按「開始冒險」會提示回聯繫補填或略過（伺服器本來就要求二選一，前端提早提示即可）。
- 逾時：`suggest` 用 `config.rpg_llm.llm.frontend_timeout_seconds`（後端總預算加 5 秒，與 `/scripts` 的送審相同的算法）；開存檔最壞是「劇本審查（必要時）＋主角模型審查」兩次呼叫，用 `2 × 後端單次請求總預算 + 5 秒` 算出（預設 65 秒，與 `session_api_controller` 說明一致），由頁面初始狀態提供，前端不寫死。兩者都用 `AbortController`，離開頁面（`pagehide`）時中止。
- 失敗對照（`wizard-state.js` 的 `describeStartFailure`）：400 欄位錯誤 → 依 `protagonist.*`／`anchor.*` 的欄位前綴決定跳回哪一步；422 → 只顯示類別對照文字；503 → 稍後再試；403 → 依代碼的說明；網路／逾時 → 通用文案。**任何失敗都保留已填內容**。

### D7 存檔空殼頁

`/game/<存檔識別碼>` 的 Page Controller 呼叫 `GameEntryService.session_summary(current, id)`（以 `get_owned` 查詢、非擁有者 `NotFound`），把標題、主角姓名、內容模式與是否已完結放進初始狀態；Controller 不自行寫權限判斷。頁面除了這些與「劇情畫面建置中」、回 `/game` 的連結外什麼都不做、不發任何 API 請求。`NotFound` 由既有的錯誤處理轉成 404 頁面。

### D8 預設頁面與導覽

`DEFAULT_AFTER_LOGIN = "/game"`（單一常數，`redirects.py` 內的註解一併更新）。`/cs` 不受影響，仍可直接開啟。導覽連結：`/scripts` 與 `/account` 的頂列已有「客服」，新增「冒險」；`/cs` 的頁首新增「冒險」；`/game` 頂列放「客服」「創作」「帳號」。以測試檢查各頁含連結，不涉及任何 Bootstrap 的互動屬性。

既有以 `/cs` 為預設頁面的測試（約十二個檔案）只需更新期望值：這些測試正是用來守住「單一常數決定預設頁面」，改常數後它們失敗是預期內的訊號，逐一確認失敗原因都是預設頁面，而不是別的。

### D9 日誌與個資

不新增事件。兩個唯讀端點與三個頁面只留既有的請求日誌（方法、路徑、狀態碼、延遲）。清單與存檔頁的內容（標題、筆名、主角姓名）不進日誌；精靈送出的內容由既有的 `protagonist_events` 管轄，前端不 `console.log` 任何創角內容。劇本卡片只含公開欄位（作者的筆名與短代號本來就公開顯示），不含作者的使用者編號。

## Risks / Trade-offs

- **[清單的存檔載入整份劇本複本以取標題]** → 分頁上限 50、預設 20；劇本有總量上限；若日後存檔數量增加造成明顯延遲，再用 expand/contract 新增 `title` 欄位（本 change 不動資料表）。
- **[單頁精靈沒有瀏覽器自動化測試]** → 純函式全面測、`setup()` 在 `vm` 沙盒測；另列瀏覽器實測任務由使用者親自操作（沿用 `story-preference` 的做法），做不到的項目明講「未驗證」。
- **[AI 預覽覆蓋聯繫內容]** → 已有內容時以 Modal 確認；預覽只是填表，不寫入。
- **[預設頁面改成 `/game` 對現有玩家的衝擊]** → 目前沒有對外開放的會員；`/cs` 仍可直接開啟且各頁有導覽連結。
- **[RC-3b 之前存檔只是空殼]** → 頁面明講「劇情畫面建置中」，並且沒有任何會花遊戲幣的動作，玩家不會因為點進去而扣款。
- **[管理員未確認年齡也能進入入口頁]** → 清單只含安全模式的劇本，開始按鈕禁用並說明；後端閘門不變。

## Migration Plan

沒有遷移、沒有新設定。部署即上線；回滾就是還原程式（`DEFAULT_AFTER_LOGIN` 回到 `/cs`）。不影響已存在的存檔與 API 契約（兩個新端點只增不改）。

## Open Questions

- 「我的存檔」是否需要顯示最後一幕的摘要或封面：不影響本 change 的契約，留到 RC-3b 有遊戲畫面後再看。
- 劇本卡片是否顯示發佈日期：預設不顯示（排序已依發佈時間），之後可視回饋加上。
