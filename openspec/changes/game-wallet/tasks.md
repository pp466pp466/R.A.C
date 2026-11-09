# Tasks

## 1. 設定機制、資料模型與遷移

- [x] 1.1 `app/services/runtime_limits.py`：`LimitSpec` 新增 `group`（`script`／`wallet`）；`LIMIT_SPECS` 新增五個 `wallet` 鍵（`coin.turn_cost.standard` 1 至 10000 預設 30、`coin.signup_grant` 0 至 100000 預設 200、`coin.checkin_grant` 0 至 100000 預設 200、`coin.free_cap` 0 至 1000000 預設 1000、`coin.hold_timeout_seconds` 60 至 1800 預設 180）；新增 `coin_turn_cost(model_key)`（模型鍵須在 `WALLET_MODEL_KEYS`，否則拋出 `LimitError`）、`coin_signup_grant()`、`coin_checkin_grant()`、`coin_free_cap()`、`coin_hold_timeout()`；`set(key, value, *, group)` 與 `rows(group)` 加分組，鍵屬於另一組時以繁體中文錯誤指出該用哪個指令。驗證：`tests/test_runtime_limits.py` 擴充：十五個鍵與範圍、五個新鍵的邊界（幣價 0 被拒、上限 1000001 被拒）、每次現讀、缺值與損壞用保底預設且事件只含鍵名、`set(..., group='script')` 拒絕遊戲幣鍵（反之亦然）、既有十個鍵的行為不變。
- [x] 1.2 新增 `app/models/wallet.py`（`Wallet`、`WalletEntry`，欄位與約束依 design D1：`user_id` 外鍵 `ON DELETE CASCADE`、`wallets.user_id` 唯一、`CHECK` 兩個餘額不小於 0、`(user_id, seq)` 唯一、`idempotency_key` 唯一、字串欄位用 `Unicode`；常數：帳本種類五個、`resolution` 兩個值）並登記於 `app/models/__init__.py`。驗證：`tests/test_wallet_models.py`：建立與預設值、負餘額被 `CHECK` 擋下、冪等鍵與 `(user_id, seq)` 唯一、刪除使用者串聯刪除錢包與帳本、`tests/architecture/test_user_foreign_keys.py` 通過。
- [x] 1.3 撰寫遷移 `0015_game_wallet`：新增兩張表（`op.create_table`，含 `CheckConstraint` 與索引）；`runtime_limits` 以 `op.bulk_insert` 只插入缺少的五個鍵（先查再插，冪等）；降級刪表與五個鍵；只用 op API 與 ORM，不含方言專屬語法。驗證：`tests/test_migrations.py` 擴充（SQLite 與真實 SQL Server `project_test`）：從 `0014` 升級後兩張表存在、`CHECK` 與唯一約束生效、五個鍵存在且唯一、既有十個限制值（含被調整過的）不變、重複升級不重複、降級後表與五個鍵消失；`alembic.ini` 與範本仍純 ASCII；`alembic check` 無差異。
- [x] 1.4 更新抹除涵蓋與架構測試：`test_account_erasure_coverage.py` 的 `CASCADED_FROM_USERS` 登記 `wallets.user_id`、`wallet_entries.user_id`；新增 `tests/architecture/test_wallet_writes.py`（AST 掃描：只有 `WalletRepository` 寫入 `wallets`／`wallet_entries`，且 Controller 不直接匯入錢包 Repository；附反向驗證）。驗證：兩個測試通過，且故意在 Service 內直接改 `Wallet.free_balance` 時架構測試失敗。

## 2. 錢包的 Repository、事件與 Service

- [x] 2.1 新增 `app/repositories/wallet_repository.py`（所有方法帶 `user_id`，只用 ORM）：`get(user_id)`、`create_if_missing(user_id, now)`（SAVEPOINT 內處理並行建立的唯一衝突）、`cas_update(wallet_id, expected_version, free, paid, now)`（條件式 UPDATE，回傳是否成功）、`add_entry(...)`、`get_entry(user_id, key)`、`list_entries(user_id, before_seq, limit)`（排除已退回的預扣與 `turn_refund`）、`stale_open_holds(user_id, cutoff, limit)`、`resolve_hold(user_id, key, to)`（條件式 UPDATE，只有 `resolution IS NULL` 成立，回傳是否成功）、`export_wallet(user_id)`（沒有錢包回傳 `None`，不建立）、`grant_totals(start, end)`（每日發放量與帳戶數的唯讀彙總，供日後監控）。驗證：`tests/test_wallet_repository.py`：每個方法的擁有者隔離（他人的鍵與列看不到）、CAS 版本不符回傳失敗且不改資料、`resolve_hold` 只能成功一次、列表排除退回、`export_wallet` 不建立錢包、並行建立錢包只有一列。
- [x] 2.2 新增 `app/observability/wallet_events.py`（函式只接收 `user_id`、整數與列舉，不接收任何文字參數）：`signup_granted`、`checkin`、`checkin_overflow`、`hold`、`settle`、`refund`、`stale_released`、`insufficient`、`adjust`、`grant_deferred`。驗證：`tests/test_wallet_events.py`：每個事件的欄位白名單、函式簽名沒有文字參數（反射檢查，附反向驗證）；比照 `tests/test_script_events.py` 與 `tests/test_protagonist_events.py` 的白名單測試寫法。
- [x] 2.3 新增 `app/services/wallet_service.py` 的核心：`_prepare(current)`（建立錢包、補發贈幣、退回逾時預扣，各自冪等且有界）、D2 的 CAS 迴圈（重試 5 次後 `WalletBusy`）、`view(current)`（兩個桶、合計、上限、幣價、簽到預覽，同一個純函式 `checkin_amounts(free, grant, cap)` 供預覽與實際入帳共用）、`ensure_signup_grant(current)`（玩家、生日已存在、冪等鍵 `signup:<使用者>`、受上限限制、管理員不發）。驗證：`tests/test_wallet_service_core.py` 逐一對應 `game-wallet` 的「錢包有兩個餘額」「帳本只增不改」「冪等」「首次註冊贈幣」情境：第一次存取才建立、管理員不發、未確認年齡不發、發一次不重發、既有玩家補發、並行兩個請求只入帳一次、`seq` 連續無洞、失敗的異動不留帳本。
- [x] 2.4 `WalletService.checkin(current, confirm_overflow)`：日期取台北日界，鍵 `checkin:<使用者>:<YYYY-MM-DD>`；入帳量在 CAS 迴圈內重算；入帳量小於簽到數且未確認 → `CheckinOverflow`（寫入任何東西之前）；已簽到 → `AlreadyCheckedIn`。驗證：`tests/test_wallet_checkin.py` 逐一對應「每日簽到」「簽到超過上限時須先確認」「免費幣上限只限制新發放」：一般入帳、同日第二次、台北 23:59 與 00:01 的日界（假時鐘）、餘額 900 與 1000 的作廢、取消不留帳本且當日仍可簽到、確認時餘額已改變以當下重算、`confirm_overflow` 不是布林的呼叫端錯誤、退款超過上限不被截斷（含 970→1000→1030 的情境）。
- [x] 2.5 `WalletService.hold`／`settle`／`refund` 與逾時退回：先免費後付費的拆分、`InsufficientCoins`（帶合計餘額與所需）、模型鍵白名單、`settle` 與 `refund` 互斥的條件式標記、退回依帳本記錄的拆分原路退回且不受上限限制、他人的預扣視為不存在、逾時預扣在下一次存取時退回（秒數取設定）。驗證：`tests/test_wallet_spend.py` 逐一對應「預扣先扣免費幣再扣付費幣」「預扣可被結算或退回且兩者互斥」「逾時未結算的預扣自動退回」：只扣免費、跨桶、合計不足、並行兩個 30 幣預扣對餘額 40 恰一個成功、同鍵重送、結算後不可退回、重複退回只退一次、失敗後以新鍵再預扣淨扣一次、60 秒不退回與 90 秒（逾時調為 60）退回、不認得的模型鍵不扣。**並行測試同時在 SQLite（檔案資料庫）與 SQL Server 上以多執行緒執行。**
- [x] 2.6 `WalletService.adjust(target_user_id, bucket, delta)`、`entries(current, before_seq, limit)` 與新增 `app/services/wallet_messages.py`（帳本種類與結算結果的繁體中文說明）。驗證：`tests/test_wallet_adjust_entries.py`：增減兩個桶、扣到負數被拒且不留帳本、免費幣超過上限仍成功；記錄由新到舊、游標分頁（45 筆分三頁）、處理中的預扣顯示「處理中」、已退回的預扣與退回列不出現、回應沒有冪等鍵與內部編號、`tests/test_wallet_messages.py` 每個種類都有繁體中文說明且不含英文代碼。

## 3. 錯誤、組裝與年齡確認的贈幣

- [x] 3.1 `app/services/errors.py` 新增 `InsufficientCoins`、`CheckinOverflow`、`AlreadyCheckedIn`、`HoldAlreadySettled`、`HoldAlreadyRefunded`、`WalletBusy`（繁體中文訊息、HTTP 狀態與代碼依規格），並登記於 `controllers/errors.py` 的轉換。驗證：`tests/test_service_errors.py` 與 `tests/test_errors.py` 擴充：每個錯誤的狀態碼、代碼與 JSON 形狀（`checkin_overflow` 帶預覽、`insufficient_coins` 帶餘額與所需）。
- [x] 3.2 `registry.py` 新增 `build_wallet_service`／`get_wallet_service`（依賴 `WalletRepository`、`RuntimeLimits`、時鐘）；`AuthService` 新增可注入的 `on_age_verified` 回呼，`verify_age` 在 `complete_age_verification_if_unset` 成功後呼叫，包在 SAVEPOINT 與 try/except 內（失敗記 `grant_deferred` 事件、不影響確認）；registry 把 `ensure_signup_grant` 接上，管理員補填不發放。驗證：`tests/test_wallet_age_grant.py` 逐一對應 `post-login-age-confirmation` 的四個情境：補填成功得 200、重複補填不再發、回呼拋出錯誤時年齡確認仍完成且第一次查詢錢包補發、不一致與未滿 13 歲沒有贈幣也沒有錢包；既有 `tests/test_age_step_service.py` 與 `tests/test_age_gate_api.py` 不改而通過。

## 4. 玩家 API

- [x] 4.1 新增 `app/controllers/wallet_api_controller.py`（`/api/wallet`）：`GET ""`（`view`）、`POST /checkin`（讀 `confirm_overflow`，非布林回 400）、`GET /entries`（`before_seq`、`limit` 預設 20 至多 100）；註冊 Blueprint；`cache_headers.py` 的禁止快取前綴加入 `/api/wallet/`。驗證：`tests/test_wallet_api.py` 逐一對應 `wallet-api` 的情境：新玩家 200／0／200、今日已簽到、接近上限的預覽、簽到 409 `checkin_overflow` 與確認後成功、409 `already_checked_in`、記錄分頁與退回不顯示、回應沒有內部編號與冪等鍵、未登入 401、未確認年齡 403 `age_confirmation_required` 且沒有發放、被鎖定 403 `account_locked`、缺少 CSRF 被拒、回應標頭禁止快取。
- [x] 4.2 路由盤點測試：列出所有已註冊的路由，斷言玩家端只有 `POST /api/wallet/checkin` 會改變餘額，且沒有任何路由寫入 `runtime_limits`。驗證：`tests/test_wallet_routes.py` 通過，並以暫時新增一條 `POST /api/wallet/spend` 驗證測試會失敗。

## 5. 管理員調整

- [x] 5.1 `app/models/admin_audit_log.py` 新增 `ACTION_WALLET_ADJUST = "wallet.adjust"` 並加入動作列舉；`AdminService.adjust_wallet(admin, public_id, *, bucket, delta, reason, password)`（依 design D8：驗證、`_manageable_target`、`_confirm`、`WalletService.adjust`、`_record`，與調整同一交易）；`_user_view` 多帶兩個餘額（`AdminRepository` 的唯讀方法，沒有錢包為 0 且不建立）。驗證：`tests/test_admin_wallet.py` 與擴充 `tests/test_admin_audit_repository.py`、`test_admin_service_users.py`：增加付費幣與稽核內容（只有桶、增減量與調整後餘額，理由在理由欄）、扣到負數 409 且沒有稽核、`bucket`／`delta`／理由不合法 400、超過上限仍成功、不能調整管理員、密碼錯誤 400 並寫 `reauth.failed`、稽核與調整同交易（模擬稽核寫入失敗時餘額不變）、檢視餘額不建立錢包；列舉以外的動作仍被拒。
- [x] 5.2 `admin_api_controller.py` 新增 `POST /users/<public_id>/wallet/adjust`；`/admin` 使用者詳情新增「調整遊戲幣」：`admin.js` 新增 `wallet` 確認種類（餘額種類下拉、增減量、理由、密碼，沿用既有對話框與錯誤顯示），`admin-format.js` 新增幣量與帳本種類的格式化函式。驗證：`tests/test_admin_api.py` 擴充（狀態碼、錯誤欄位、CSRF）；`tests/js/admin-format.test.js` 新增格式化測試；`tests/architecture/test_frontend_rules.py`（沒有 CDN、沒有 `v-html`、沒有 Bootstrap 互動屬性）通過。

## 6. 匯出、抹除與指令

- [x] 6.1 `personal_export_service.py`：JSON 新增 `wallet` 區塊（兩個餘額加全部帳本，含已退回的預扣與退回列，不含冪等鍵與 `ref_key`），CSV 新增 `wallet_entries.csv`（UTF-8-sig、公式字元中和）；沒有錢包時輸出 0 與空帳本且**不建立錢包**。驗證：`tests/test_personal_export_wallet.py` 逐一對應 `personal-data-export` 的情境：JSON 與 CSV 都含帳本、含已退回、不含內部資料、沒有錢包的使用者匯出後仍沒有錢包。
- [x] 6.2 抹除涵蓋：有錢包、帳本與未結算預扣的帳號被抹除（自助、閒置、未確認年齡、未滿 13 歲與備份還原後重播）後，兩張表都沒有該使用者的列。驗證：`tests/test_account_erasure_wallet.py` 涵蓋四種抹除原因與 `replay-deletions`；既有 `tests/test_account_erasure_*.py` 不改而通過。
- [x] 6.3 `app/cli.py` 新增 `set-wallet-setting KEY VALUE` 與 `show-wallet-settings`（沿用 `set-script-limit` 的輸出格式與稽核），登記到 `MANUAL_COMMANDS`；`set-script-limit` 遇到遊戲幣鍵時指出該用 `set-wallet-setting`。驗證：`tests/test_cli_wallet_settings.py`：修改並稽核、範圍外被拒、用錯指令被拒並指出正確指令、`show-wallet-settings` 輸出五個鍵與是否為保底預設、輸出沒有任何個資；`tests/test_scheduled_commands.py` 通過（兩個指令歸入人工類）；`show-script-limits` 仍輸出十個鍵。

## 7. 整合驗證

- [x] 7.1 端對端整合測試 `tests/test_wallet_flow.py`（假時鐘與假回合，不打真實 API）：新玩家年齡確認得 200 → 簽到 → 連續三個回合各預扣再結算 → 一個回合失敗被退回 → 餘額與帳本與記錄頁一致 → 管理員調整 → 匯出 → 抹除；另跑「接近上限」與「餘額不足」兩條路徑，並掃描完整流程的日誌輸出，找不到冪等鍵、存檔識別碼、理由與任何玩家文字。驗證：測試通過，且以故意破壞（跳過 CAS、讓結算與退回可同時成立、日誌輸出冪等鍵）驗證各自對應的測試會失敗。
- [x] 7.2 更新 README：端點表（三支玩家端點與一支管理員端點及其錯誤代碼）、兩個新指令、「遊戲幣」小節（規則、數值與設定方式）、驗收紀錄。驗證：`tests/test_readme.py` 與 `tests/test_launch_docs.py` 通過。
- [x] 7.3 完整驗證：`pytest` 全套（含真實 SQL Server `project_test`，同時只能有一個測試行程）、`npm test` 的 Node 測試、`openspec validate game-wallet --strict`；README 的「驗收紀錄」記錄筆數。驗證：全數通過。
- [x] 7.4 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`資料模型-data-model.md`（兩張表、約束與設計理由）、`系統架構-architecture.md`（`/api/wallet`、`WalletService` 與依賴方向、`AuthService` 回呼）、`帳號與安全-auth-security.md`（贈幣時機、管理員調整與稽核）、`營運與部署-operations.md`（兩個指令、新事件、五個設定）、`前端結構-frontend.md`（`/admin` 的調整表單）、`路線圖-roadmap.md`（W1 狀態與日期）、`RPG.md`（7.7 節標示已實作），並讓 `tests/test_design_docs.py` 通過。驗證：`pytest tests/test_design_docs.py` 通過。

## Workflow follow-up

- 實作完成並由使用者驗收後再 `/openspec-archive-change game-wallet`；封存時新增 2 個主規格（`game-wallet`、`wallet-api`）並同步 7 個既有規格的修改（`runtime-limits`、`admin-user-management`、`admin-audit-log`、`post-login-age-confirmation`、`personal-data-export`、`account-deletion`、`service-observability`）。
- 升級開發用 SQL Server `project` 到 `0015` 之前由使用者先備份；升級後執行 `alembic current`、`alembic check`、`flask show-wallet-settings`，並依專案慣例同步 `docs/project資料結構文件.md`（兩張新表的字典、外鍵、第 12 節 migration 歷程，標頭的表／欄位數與「最後更新」）並在第 14 節最下方追加異動紀錄。
- AGENTS.md 第 11 節「遊戲幣與帳本」那一條的「關聯的存檔內部編號」已改為「關聯的存檔識別碼」（使用者核可，2026-11-09），技能鏡像與 `tests/test_agents_skill_sync.py` 已同步。
- `/privacy`、`/terms` 補遊戲幣帳本與理由回饋的說明屬後續（版本進位，仍標為草稿待法務覆核）；玩家畫面屬 `player-hub`；預扣的實際呼叫屬 RC-2。
