# Proposal

## Why

RPG 劇情對話要改成「消耗遊戲幣」：2026-11-05 的探索（`docs/RPG.md` 第 7 章、`docs/report.md` U-31）與 AGENTS.md 第 7、10、11 節的修訂（`51d4330`）已經決定一輪來回 30 幣、首次註冊登入送 200 免費幣、每日簽到 200、免費幣上限 1000、幣不過期、先扣免費再扣付費，客服則維持次數額度、不扣幣。目前專案沒有任何錢包、帳本或簽到，RC-2 的回合引擎因此沒有地方預扣與結算，也沒有地方放「失敗不重複扣」的冪等保證。

這個 change 先把錢包做完整、做成可獨立驗收的後端：餘額與帳本、贈幣與簽到、預扣／結算／退回，以及管理員調整與稽核。RC-2 只需要呼叫它。儲值與訂閱（W2）、RPG 的回合引擎（RC-2）與玩家看得到的畫面（`player-hub`）都不在這裡。

## What Changes

- **錢包與帳本**：每位玩家一個錢包，**免費餘額與付費餘額兩個整數**（不做批次制：幣不過期，批次只剩 W2 的「按購買退款」一個用途，到時再用 expand／contract 加）。餘額異動一律伴隨一筆只增不改的帳本列（種類、免費與付費各自的增減量、異動後的兩個餘額、冪等鍵、關聯的存檔對外識別碼、時間）。錢包在第一次需要時才建立；**使用者的任何異動都以版本號比對（CAS）加重試**，不使用原生 SQL 或資料庫專屬的鎖。
- **贈幣**：玩家**完成年齡確認時**發放首次註冊的 200 免費幣（帳號 24 小時內沒完成年齡確認會被刪，不會送出又收不回）；冪等鍵「`signup:<使用者>`」，已發過不再發。**既有且已完成年齡確認的玩家**在第一次存取錢包時補發（同一個冪等鍵，不另寫資料遷移）。管理員帳號不發。
- **每日簽到**：以台北時間 0 點為日界，每個台灣日一次，每次 200 免費幣。**兩步驟**：`GET /api/wallet` 回報目前是否可簽到、會領多少、會作廢多少；領取會超過免費幣上限 1000 時，`POST /api/wallet/checkin` 沒有帶 `confirm_overflow` 就回 409 `checkin_overflow` 並帶預覽，玩家選「取消」不消耗當日簽到、選「領取」則超出的部分作廢。伺服器以最終餘額重算，冪等鍵為「`checkin:<使用者>:<台北日期>`」。免費幣上限**只限制新發放**（簽到、贈幣）：退款與管理員調整不受限，所以餘額可能暫時超過 1000。
- **預扣、結算與退回（供 RC-2 呼叫的 Service，沒有 HTTP 端點）**：`hold` 以呼叫端給的冪等鍵原子預扣（**先扣免費、再扣付費**，帳本列記錄各拆多少）；`settle` 與 `refund` 各自冪等且**互斥**（一個預扣只能結算或退回其中一次，退回依原本的拆分原路退回）；餘額不足拋出 `InsufficientCoins`（409 `insufficient_coins`，帶餘額與所需數量）；不得出現負餘額。**預扣超過逾時秒數仍未結算者，在該使用者下一次存取錢包時被退回**（不設排程指令）。每輪的幣價以「模型鍵」取得，現在只有 `standard` = 30。
- **數值放資料庫設定表**：`runtime_limits` 新增五個鍵：`coin.turn_cost.standard`（30）、`coin.signup_grant`（200）、`coin.checkin_grant`（200）、`coin.free_cap`（1000）、`coin.hold_timeout_seconds`（180）；遷移寫入初始值，程式每次使用時現讀、缺值或不合法用保底預設。新增人工類指令 `flask set-wallet-setting`、`flask show-wallet-settings`（驗證後寫入並留下 `limit.set` 稽核）；既有的 `set-script-limit`、`show-script-limits` 仍只管劇本與主角生成的十個鍵。
- **玩家 API**：`GET /api/wallet`（兩個餘額與合計、上限、簽到狀態與預覽、每輪幣價）、`POST /api/wallet/checkin`、`GET /api/wallet/entries`（記錄頁用，依時間新到舊、游標分頁、每筆附繁體中文說明，不含內部編號）。登入的玩家才能用；受限登入狀態（鎖定、待確認年齡、等待刪除）由既有的預設拒絕處理。回應禁止快取。
- **管理員調整**：`POST /api/admin/users/<public_id>/wallet/adjust`，本文為 `bucket`（`free`／`paid`）、`delta`（非零整數，正負皆可）、`reason` 與管理員密碼；高風險操作，重新輸入密碼並寫入新的稽核動作 `wallet.adjust`（含桶、增減量與調整後餘額，不含理由以外的文字）。調整不受免費幣上限限制，不能使餘額低於 0。管理員的使用者檢視多顯示兩個餘額；`/admin` 的使用者詳情新增「調整遊戲幣」表單。
- **生命週期**：錢包與帳本以 CASCADE 隨帳號刪除，納入個人資料匯出（JSON 與 CSV）；日誌只記內部編號與整數。
- **不做**：儲值與訂閱（W2）；RPG 回合引擎、預扣的實際呼叫與餘額不足時的 SSE 行為（RC-2）；玩家看得到的畫面（錢包頁、記錄頁、個人頁、簽到按鈕，`player-hub`）；模型目錄與每模型定價（W3，這裡只留「模型鍵」）；批次制與到期；排程的逾時預扣清理；贈幣的農號防堵（已列為監控項目，不設關卡）；`/privacy` 與 `/terms` 的措辭（版本進位另做）。

## Capabilities

### New Capabilities

- `game-wallet`: 錢包與帳本的資料不變量、首次註冊贈幣、每日簽到與免費幣上限、預扣／結算／退回的語意與冪等、逾時預扣的退回、免費幣先扣，以及這些數值的來源。
- `wallet-api`: 玩家端 `GET /api/wallet`、`POST /api/wallet/checkin`、`GET /api/wallet/entries` 的請求與回應、兩步驟的簽到確認、記錄的顯示、快取與受限狀態的行為。

### Modified Capabilities

- `runtime-limits`: 鍵的集合增加五個遊戲幣設定與其範圍、初始值由遷移寫入、增加 `set-wallet-setting`／`show-wallet-settings` 並限定各指令管轄的鍵。
- `admin-user-management`: 新增「調整遊戲幣餘額」；「危險操作須重新輸入密碼」納入這個操作。
- `admin-audit-log`: 動作的固定列舉新增 `wallet.adjust`。
- `post-login-age-confirmation`: 補填年齡確認成功時（玩家）發放首次註冊贈幣。
- `personal-data-export`: 匯出納入錢包餘額與帳本。
- `account-deletion`: 抹除的範圍納入錢包與帳本。
- `service-observability`: 白名單事件新增遊戲幣事件，只含內部編號、列舉與整數。

## Impact

- **資料庫**：遷移 `0015_game_wallet`（只新增，expand）：`wallets`、`wallet_entries` 兩張新表（`user_id` 外鍵 `ON DELETE CASCADE`；`idempotency_key` 唯一；`wallets.user_id` 唯一）；`runtime_limits` 以 ORM 補五列。SQLite 與 SQL Server 雙軌都要通過。**同步 `docs/project資料結構文件.md`（字典、外鍵、第 12 節 migration 歷程）並在第 14 節追加異動紀錄**（使用者升級本機 `project` 資料庫後）。
- **程式**：新 `app/models/wallet.py`、`app/repositories/wallet_repository.py`（所有方法帶 `user_id`）、`app/services/wallet_service.py`（贈幣、簽到、預扣、結算、退回、調整、記錄）、`app/services/wallet_messages.py`（帳本種類的繁體中文說明）、`app/services/runtime_limits.py`（鍵與分組）、`app/services/auth_service.py`（年齡確認後的贈幣）、`app/services/admin_service.py`、`app/models/admin_audit_log.py`、`app/controllers/wallet_api_controller.py`、`app/controllers/admin_api_controller.py`、`app/services/errors.py`（`InsufficientCoins`、`CheckinOverflow`）、`app/services/registry.py`、`app/cli.py`（兩個指令並登記為人工類）、`app/observability/wallet_events.py`、`personal_export_service.py`、`app/static/js/admin/`（調整表單）。
- **測試**：兩個桶的扣款拆分（含剛好用完免費、跨桶、不足）、並行扣款不超扣（CAS 重試）、冪等（同一冪等鍵的預扣、結算、退回各自重送）、結算與退回互斥、逾時預扣的退回、簽到的台北日界與兩步驟（上限邊界、取消不消耗、作廢）、贈幣只發一次與既有玩家補發、管理員調整與稽核與密碼、設定的現讀與保底、抹除與匯出涵蓋、日誌掃描、遷移雙軌與降級、架構測試（`wallet_*` 只能由 Service 寫入）。以故意破壞程式的方式驗證每組新測試會失敗。
- **文件**：`docs/資料模型-data-model.md`、`docs/project資料結構文件.md`、`docs/系統架構-architecture.md`、`docs/帳號與安全-auth-security.md`、`docs/營運與部署-operations.md`、`docs/前端結構-frontend.md`、`docs/路線圖-roadmap.md`、`docs/RPG.md`（第 7.7 節標示已實作）、README（端點表與指令）。AGENTS.md 已於 `51d4330` 修訂，這個 change 不再修改它。
- **不影響**：客服的次數額度與對話、登入流程、既有存檔與劇本、全站每日 token 上限。
