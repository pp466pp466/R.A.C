# Proposal

## Why

RPG 已經可以從選劇本一路玩到結局（RC-2a 回合引擎、RC-2b 記事簿、RC-3b 遊戲畫面都已封存），但玩家對**剛收到的那一輪**完全沒有補救手段：AI 偏離設定、劇情重複、打錯行動，只能往下玩或開新存檔。規範（AGENTS.md 第 7、10、11 節的 RA-17，`docs/RPG.md` 7.8）早已定下「只能動最後一輪的重新生成與編輯輸入、理由必填、AI 輸出不可直接修改」，但後端沒有任何端點，畫面也沒有按鈕。

現在做是因為 RC-3 已完成，玩家能在畫面上親手玩，重新生成與編輯的缺口最明顯；`docs/RPG.md` 7.15.2、7.15.7、7.17.1 把幾個待決事項（結局那一輪、被擋與中斷的回合、斷線的免費次數、預扣鍵要不要加 `revision`、管理員檢視要不要拆出）留到這個 change，本次探索（2026-11-12）已與使用者逐項定案，一併寫進來。

## What Changes

- **兩個新串流端點**（事件協定沿用回合串流）：`POST /api/game/sessions/<id>/turns/<client_msg_id>/regenerate/stream`（分類＋理由＋`expected_revision`）與 `…/edit/stream`（新行動＋`expected_revision`）。只能動「最後一輪」；成功才**原子替換**，失敗、中斷、被擋時保留舊內容、退回預扣、不消耗免費次數。
- **費用**：每個回合前 2 次重新生成免費（次數放資料庫設定表、每次現讀），第 3 次起每次一輪的幣價（30）；編輯輸入一律一輪的幣價、原本那輪已扣的幣不退，並使該回合的免費次數歸零。預扣沿用 `attempt` 當冪等鍵（每次認領加 1），解掉 7.15.2 留下的「退回過的鍵被撿回」問題。
- **回滾**：回合開始時把回合前的世界狀態存成快照（只留最後一輪，下一輪完成時清除）；重新生成與編輯以快照為基礎重算上下文與狀態，成功時世界狀態以「還原再套用」的結果寫回，版本照常加 1。記事簿不需回滾（視窗永遠不含最後一輪）。
- **並行與認領**：回合列新增 `revision`、`regen_count`、`revising_started_at`、`state_before_json`；以條件式 UPDATE 認領（`revision = expected_revision`）；修訂進行中不得開始新回合，反之亦然；認領逾時（同預扣逾時）可被接手。
- **回饋表** `turn_regenerations`：每次嘗試一列（種類、分類、理由、是否免費、結果 `applied`／`failed`／`blocked`），失敗與被擋的理由也保留；隨存檔與帳號刪除、納入個人資料匯出；日誌只記內部編號與整數長度。理由**不進模型、不進 system_instruction**。
- **前端**：最後一輪的氣泡下方出現「重新生成」「編輯」；重新生成跳自製 Modal（分類＋5 至 50 字理由）；編輯跳確認 Modal（提醒再次消耗遊戲幣、顯示餘額、不足時停用）；顯示剩餘免費次數；快取改用「從最後一個完成回合的前一個序號重抓並覆寫」，修正其他裝置與 IndexedDB 過期；在途紀錄加上種類與 `expected_revision`（不存理由）。
- **文件與政策頁**：`/privacy`、`/terms` 補理由回饋的告知（並補上遊戲幣與帳本，目前政策頁尚未提及）；`docs/RPG.md` 7.8、7.15.2、7.15.7、7.17 的 2c 待決事項改為已決；路線圖、資料結構文件與各設計文件同步。
- **不改 AGENTS.md**：規則（RA-17）已寫明，本次只是實作。
- **BREAKING**：無。遷移全部是 expand（新增可為空的欄位與新表），舊版程式在新結構上照常運作；既有回合沒有快照，視為不可重新生成或編輯（只影響遷移前建立的回合）。

## Capabilities

### New Capabilities

- `rpg-turn-revision`：玩家重新生成與編輯最後一輪的後端行為——哪一輪可動、端點與請求驗證、費用與免費次數、預扣與退回、回滾與原子替換、並行與認領、失敗處理、回饋表與隱私。
- `rpg-revision-ui`：遊戲畫面的重新生成與編輯——按鈕與可用性、理由 Modal、編輯確認 Modal、剩餘免費次數、串流中的氣泡與失敗還原、結果通知、快取覆寫與在途紀錄。

### Modified Capabilities

- `rpg-turn-engine`：回合開始時保存回合前快照、下一輪完成時清除；修訂進行中不得開始新回合。
- `rpg-sessions`：回合列的修訂欄位、回合視圖帶 `revision`、存檔快照帶最後一輪的修訂資訊。
- `rpg-turn-stream`：事件的 `meta`／`done` 帶修訂資訊（`kind`、`revision`、`free_left`）；重送事件同步。
- `rpg-game-sync`：在途紀錄帶種類與 `expected_revision`；最後一輪以覆寫重抓。
- `runtime-limits`：新增設定鍵「每個回合的免費重新生成次數」。
- `personal-data-export`：匯出納入重新生成回饋。
- `auth-pages`：隱私權政策與服務條款補理由回饋與遊戲幣帳本的告知。

## Impact

- **程式**：`app/models/rpg_session.py`（欄位與新模型）、`migrations/versions/…_0018_…`（遷移）、`app/repositories/session_repository.py`（認領、替換、快照、釋放；新增 `TurnRegenerationRepository` 或同檔方法）、`app/services/`（`game_service.py` 的 `begin_revision`、新增 `RevisionRunner`、把狀態階段抽成 `TurnRunner` 與 `RevisionRunner` 共用）、`app/services/runtime_limits.py` 與遷移的設定值、`app/controllers/game_api_controller.py`（兩個端點）、`app/observability/rpg_events.py`（新事件）、`app/services/personal_export_service.py`、`app/templates/legal/*.html`、`app/services/legal.py`（條款版本）。
- **前端**：新增 `app/static/js/game/game-revision.js`；修改 `game-api.js`、`game-sync.js`、`game-store.js`、`game-state.js`、`game-session.js`、`game-labels.js`、`game.js`、`session.html`、`game.css`、`tests/js/support/game-contract.js`（名稱約定三處同步）。
- **資料庫**：`memory_logs` 加 4 個可為空或有預設的欄位、新表 `turn_regenerations`、設定表新增 1 列；開發 SQL Server `project` 目前在 0017，部署順序為備份 → 遷移 → 新版程式。`docs/project資料結構文件.md` 須同步並在第 14 節追加異動紀錄。
- **測試**：Python（儲存庫、服務、端點、端到端、日誌掃描、匯出、遷移）、Node（`game-revision`、同步、狀態純函式、`vm` 掛載）、驗收腳本與瀏覽器實測、正式破壞驗證。
- **文件**：`docs/RPG.md`、`docs/路線圖-roadmap.md`、`docs/資料模型-data-model.md`、`docs/系統架構-architecture.md`、`docs/前端結構-frontend.md`、`docs/project資料結構文件.md`、`README.md`，依 `docs/README.md` 的對照表。
- **不做**：管理員的「重新生成回饋」檢視頁與統計（拆成後續小 change，2c 只負責寫入、匯出、刪除連動）；第 0 幕的重新生成；對更早的回合動手（要回到更早的分歧，開新存檔）；把普通回合的斷線納入免費次數（先觀察中斷比例，見 `docs/RPG.md` 7.16.4）；「AI 寫入記事簿的條目不帶『第 N 輪』字樣」（另開的小 change）。
