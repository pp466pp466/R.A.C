# Proposal

## Why

後端的 RPG 已經能開存檔、玩回合、記事簿與扣遊戲幣（RC-1、RC-9、RC-2a、RC-2b、W1 都已封存），但**沒有任何畫面能讓人親手玩到它**：沒有可玩劇本的清單端點（`GET /api/scripts` 只回自己創作的劇本）、沒有我的存檔清單端點、創角與開存檔只有 API 沒有畫面，登入後的預設頁面還是客服 `/cs`。`story-preference` 做好的排序（`list_published(..., preference=…)`）目前也沒有玩家端點使用。

RC-3 探索（`docs/RPG.md` 7.17）已決定先做入口（RC-3a）、再做遊戲畫面（RC-3b）。入口採最小版：兩個唯讀端點加卡片列表，終點是「存檔建立成功」，不等到 RC-8 的完整目錄。

## What Changes

- **兩個唯讀端點**：`GET /api/sessions/catalog`（可玩的已發佈劇本清單：內容模式不高於玩家生效模式，依玩家的故事偏好排序，分頁）與 `GET /api/sessions`（我的存檔清單：由新到舊，含狀態與已完成輪數，分頁）。兩者只回對外識別碼與卡片需要的欄位，不含劇本全文、審查結果、內部編號與他人的個資。
- **三個頁面**（MVC Page Controller，只渲染外殼與初始狀態）：
  - `/game`：入口頁，「選劇本」與「我的存檔」兩個分頁；顯示目前內容模式，未滿 18 歲時說明原因；頂部導覽連到創作、客服與帳號。
  - `/game/start/<版本識別碼>`：開存檔精靈——序章（背景故事，唯讀）→ 聯繫（對象與一句關係，可略過）→ 創角（角色卡、選填的自我介紹、「請 AI 生成」、示範氣泡、來歷）→「開始冒險」。AI 生成只是預覽，可重新生成、修改後接受；生成失敗、被擋或不想用時改為自創。
  - `/game/<存檔識別碼>`：**空殼**，確認存檔屬於本人並顯示劇本標題、主角與「劇情畫面建置中」，由 RC-3b 取代。
- **登入後的預設頁面改成 `/game`**（`DEFAULT_AFTER_LOGIN` 單一常數），滿足「設定暱稱後自動導向選擇劇本」。客服 `/cs` 仍可由導覽列進入。
- 全程只用既有的後端（`/api/sessions/setup/…`、`/api/sessions/protagonist/suggest`、`POST /api/sessions`）；Vue 3 全域變數模式、離線資源、自製 Modal 與 Toast、純文字渲染（嚴禁 `v-html`）。創角內容（含自我介紹）**不寫入任何瀏覽器儲存**。
- 不改任何遊戲規則、審查、額度或資料表；**沒有遷移**。

## Capabilities

### New Capabilities

- `rpg-entry-api`：可玩劇本清單與我的存檔清單兩個唯讀端點（可見範圍、內容模式篩選、故事偏好排序、分頁、欄位白名單、受限狀態與非擁有者隔離）。
- `rpg-entry-pages`：`/game` 入口頁、`/game/<存檔識別碼>` 空殼頁與各頁導覽（頁面外殼與初始狀態、閘門提示、內容模式提示、錯誤與空狀態、純文字渲染、離線資源）。
- `rpg-creation-wizard`：`/game/start/<版本識別碼>` 的開存檔精靈（步驟與資料流、AI 生成預覽與自創、示範氣泡、錯誤對照、逾時與中止、不寫入瀏覽器儲存、成功後導向）。

### Modified Capabilities

- `auth-pages`：「登入後的預設頁面」由 `/cs` 改為 `/game`（已登入者開啟 `/` 或 `/login`、登入與年齡確認完成後都導向它）。

## Impact

- **程式**：`app/controllers/session_api_controller.py`（兩個 GET）、新增 `app/controllers/game_page_controller.py`（三個頁面）、`app/security/redirects.py`（常數）、`app/__init__.py`（註冊藍圖）；新增 Service 方法（清單檢視組裝，放在 `SessionService` 或新的 `GameEntryService`，由 design 決定）；`app/repositories/script_repository.py`（一次取回清單所需的劇本與標籤，避免逐筆查詢）、`app/repositories/session_repository.py`（各存檔的已完成輪數一次取回）；前端新增 `app/templates/game/*.html`、`app/static/js/game/*.js`、`app/static/css/game.css`，並在 `/scripts`、`/account`、`/cs` 的導覽加「冒險」連結。
- **測試**：既有以 `/cs` 為預設頁面的測試（約十二個檔案）改為 `/game`；新增端點、頁面、精靈與前端純函式的測試。
- **資料庫**：無。**部署**：無順序限制；沒有新環境變數。
- **文件**：`docs/系統架構-architecture.md`、`docs/前端結構-frontend.md`、`docs/帳號與安全-auth-security.md`（預設頁面與端點表）、`docs/RPG.md` 7.17、`docs/路線圖-roadmap.md`，依 `docs/README.md` 對照表；README 端點表與路由數。
- **不涉及**：AGENTS.md（規則已寫明）、LLM 提示詞、遊戲幣與額度、日誌白名單（不新增事件）。
- **不做**：遊戲主畫面、第 0 幕、串流、記事簿面板、餘額與簽到入口（RC-3b）；完整目錄、搜尋、標籤篩選、劇本詳情頁（RC-8）；刪除或封存存檔的介面；試玩自己的草稿；頭像與性別欄位（v1 不做）；對話人設的預填（`player-personas`，只預留位置）；`/age` 的分步精靈與使用守則 Modal（`onboarding-polish`）。
