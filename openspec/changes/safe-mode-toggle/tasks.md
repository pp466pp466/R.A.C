# Tasks

> 切批與時程（依 `openspec-batch-workflow`；使用者 2026-11-19 確認）：目標 **11-23 前部署到展示環境，所有項目都要完成，不延後**。對話與 commit 邊界：**A＝第 1、2 組（同一個對話，各自 commit，每個 commit 測試皆為綠）**、**C＝第 3、4 組（同一個對話，條款與隱私權政策已由原第 5 組提前到第 3 組）**、**E＝第 5 組**、**F＝第 6 組**。建議節奏：11/20 A、11/21 C、11/22 E 與 F 的測試、11/23 部署與驗收。每批結束前：相關測試通過、更新 HANDOFF.md、列出簡報衝突（只通知）；使用者說 commit 才提交，commit 後停止，不自行開始下一批。

## 1. 後端核心：兩種模式、生效模式、緊急煞車、同意與 API、遷移

- [x] 1.1 `app/models/user.py` 與 `app/services/content_tier.py`：新增兩值的 `CONTENT_MODES`（`safe`／`adult`），`parse_tier` 把舊值 `standard` 讀作 `adult`，`SAFE_RULES_VERSION` 換新值；**過渡相容（使每個 commit 皆為綠）：三值的 `CONTENT_TIERS` 與 `CONTENT_TIER_STANDARD` 暫時保留，於 2.3 縮減**；改寫 `tests/test_content_tier.py`，驗證兩值順序、未知值為 `safe`、`standard`→`adult`
- [x] 1.2 `app/config.py`：`ContentSettings` 改為只含 `safe_mode_forced`（`SAFE_MODE_FORCED`，預設 False，非布林啟動失敗且訊息不含值）；`ADULT_MODE_ENABLED`／`ADULT_MODE_MODELS` 併入已淘汰變數的警告機制（只列變數名稱、不影響行為）；同步 `.env.example`、`scripts/launch_plan.py`（含舊變數被拒、可含新變數）；改寫 `tests/test_config_rpg_llm.py`、`tests/test_env_example.py`、`tests/test_launch_plan.py` 與 `tests/test_content_tier_modes.py` 的環境段落並全數通過
- [x] 1.3 `AccessPolicy`：新增 `effective_mode(user, script_binding=None)`（偏好缺少目前版本同意視為 safe、取偏好、劇本綁定、緊急煞車最保守者）；建構時接收 `safe_mode_forced`；**過渡相容：舊的 `effective_tier(save_tier=…)`、`content_access`、`is_adult`、`author_content_cap` 暫時保留（由 2.1–2.3 移除）**；以表格驅動測試涵蓋各組合（含管理員不豁免、煞車、舊值）並通過
- [x] 1.4 `ContentModeService` 與 `/api/account/content-mode`：`mode` 只收 `safe`／`adult`（`standard` 回 400）、關閉只需 `confirm_rules`（已同意目前版本則不需要）、緊急煞車時要求 `adult` 回 403 `safe_mode_forced`，回應形狀 `{preference, effective, confirmed, forced}`；移除 `ContentModeRestricted`、`AdultModeUnavailable`、`confirm_adult`；改寫 `tests/test_content_mode_api.py` 並通過（含 CSRF、401、年齡確認前 403、事件日誌不含內容）
- [x] 1.5 遷移 `0020`：`sessions.safe_hint_at`（可為空、不建索引、`recreate="never"` 的 downgrade），ORM 模型同步；`docs/project資料結構文件.md` 同步並於第 14 節追加異動紀錄；驗證 `tests/test_project_db_schema_doc.py`、遷移升降級測試與 `alembic heads` 單一頭通過
- [x] 1.6 本批收尾：相關 pytest 全過（`test_content_tier*`、`test_content_mode_api`、`test_config*`、`test_env_example`、`test_launch_plan`、`test_access_policy*`、`test_project_db_schema_doc`、`tests/architecture`；另跑 rpg 與劇本相關測試確認過渡相容層沒有造成回歸）；更新 `docs/帳號與安全-auth-security.md` 與 `docs/資料模型-data-model.md` 中受影響段落並讓 `tests/test_design_docs.py` 通過；更新 HANDOFF.md；列出簡報衝突

## 2. 目錄、開存檔與審查尺度

- [x] 2.1 `script_repository`：目錄與依標籤查詢移除 `max_tier` 篩選；`startable.py`、`session_service.py`、創角資料與主角生成移除劇本模式比較與 `ContentTierRestricted`，新增 `SafeModeForced` 錯誤碼對應；`game_entry_service` 卡片的 `content_tier` 經 `parse_tier`、`content_mode` 改為 `{effective, forced}`、我的存檔的模式現算（存檔複本的劇本綁定＋玩家設定＋煞車）；改寫 `rpg-entry-api`、`rpg-sessions`、`script-store` 對應的測試（安全模式玩家看得到並開得了未綁定劇本、綁定劇本的存檔恆為 safe、設定改變後下一回合生效）並通過
- [x] 2.2 `game_service`：三處（回合、重生／編輯、第 0 幕）改用 `effective_mode(current, script_binding=<存檔複本的 content_tier>)`，不再讀 `save.content_tier`；開存檔仍寫入當下現算值但不被讀取；測試涵蓋「玩家中途關閉安全模式後下一回合使用 adult 界線」與「綁定劇本不受影響」並通過
- [x] 2.3 `rpg_prompt.TIER_BOUNDARIES` 刪 `standard` 段；`script_rules.check_text_rules`／`check_rules` 刪 `STANDARD` 分支（`safe` 才產生 `tier_mismatch`，未綁定不因性內容產生）；`rpg_output_guard`、`script_review_prompt` 對應兩值；`script_spec.validate_script` 移除 `max_tier`、接受 `safe`／`adult`／`standard`（標準化為 `adult`，只對新儲存內容標準化）；`script_authoring_service`／`script_submission_service` 移除作者上限傳遞；改寫 `rpg-turn-context`、`rpg-output-review`、`script-fixed-spec`、`script-review`、`script-authoring` 相關測試並通過
- [x] 2.4 `app/data/scripts/xianxia-qingyun.json` 的 `content_tier` 改為 `adult`；更新 `tests/` 內種子相關案例（新增第 2 版取代第 1 版、舊存檔不受影響、15 歲玩家案例改為安全模式玩家）；驗證 `flask seed-scripts` 預覽與實跑的結果
- [x] 2.5 補一支全碼庫守門測試：`app/` 內不得有比較 `== "standard"`／`CONTENT_TIER_STANDARD` 的判斷（舊值只允許出現在 `content_tier.py` 的讀取正規化），移除 1.1、1.3 的過渡相容層（`CONTENT_TIERS` 縮為兩值、`effective_tier`、`content_access`、`is_adult`、`author_content_cap`）；`tests/architecture` 與守門測試通過
- [x] 2.6 本批收尾：相關 pytest 全過（rpg 回合、審查、劇本、主角、種子、`tests/architecture`）；更新 `docs/RPG.md`（內容分級與審查段）、`docs/系統架構-architecture.md`、`docs/資料模型-data-model.md` 並讓 `tests/test_design_docs.py` 通過；更新 HANDOFF.md；列出簡報衝突

## 3. 個人頁的安全模式開關與同意 Modal、條款與隱私權政策

- [ ] 3.1 新增 `app/static/js/auth/account-safe-mode.js`（純函式與狀態機：載入、切換、同意 Modal 開關、緊急煞車停用、請求失敗還原、返回位置驗證 `safeReturnPath`）與 Node 測試（`tests/js/`，含沙盒載入）；驗證：開→關需同意、已同意直接送出、關→開直接送出、煞車禁用、請求期間禁用並於 `finally` 恢復、無效返回位置被丟棄
- [ ] 3.2 `auth/account.html` 與 `account.js`：新增「安全模式」區塊（開關、兩種狀態說明、自製 Modal、無 `data-bs-*`、無 `v-html`、無年齡勾選）；`auth_page_controller` 初始狀態只帶開／關、`confirmed`、`forced` 與已驗證的返回位置；改寫 `tests/test_account_page.py` 並通過（初始狀態不含生日與年齡區間、管理員未完成年齡確認不顯示、載入順序）
- [ ] 3.3 瀏覽器冒煙（本機 Chrome，自己的埠）：360／1280 px 無水平溢出、無純白文字、無主控台錯誤；開關在安全規範 Modal 的取消／Esc／確認路徑；帶 `?return=/game/start/…` 時完成後導回、帶惡意返回位置時被忽略；結果記入 README 驗收紀錄
- [ ] 3.4 `/terms`、`/privacy`：補上露骨內容、安全模式兩種狀態與絕對禁止項、年滿 18 歲與謊報責任、敏感內容可能被模型供應商留存或審閱、關閉安全模式時內容可能被 RPG 模型供應商留存或審閱；更正 `/terms` 第 5 節「預設使用一般模式」一句；提高條款版本；改寫 `tests/test_legal_pages.py` 與年齡確認相關測試並通過（頁面仍標示草稿、沒有「一般模式」字樣）
- [ ] 3.5 本批收尾：Node 全套與相關 pytest 通過；更新 `docs/前端結構-frontend.md`（新檔與 `/account` 載入順序）、`docs/帳號與安全-auth-security.md`（含條款與隱私權政策的揭露）；更新 HANDOFF.md；列出簡報衝突

## 4. 目錄通知、卡片標示與創作頁綁定

- [ ] 4.1 `entry.js` 與 `game/index.html`：點選未綁定劇本（`content_tier` 為 `adult`）且玩家 `effective === 'safe'`、`forced` 為假時，以自製 Modal 詢問（「前往設定」導向 `/account?return=/game/start/<id>`、「維持安全模式繼續」、Esc／遮罩視同取消）；綁定劇本、已關閉安全模式或煞車時直接前往；`game-labels.js` 改寫 `TIER_LABELS`（綁定標籤）、`contentModeNotice`（安全開／關／煞車三種說明，移除未成年與「部分劇本不顯示」）、移除 `GATE_FAILURES.content_mode_restricted`；Node 測試涵蓋全部分支並通過
- [ ] 4.2 精靈頁（`wizard*.js`）與遊戲頁（`game-session.js`、`game-state.js`）：移除 `content_tier_restricted` 與 `MINOR_NOTE` 分支，遊戲頁顯示現算模式（含綁定與煞車說明）；改寫對應 Node 測試與 `tests/test_game_pages.py`、`test_game_template` 並通過
- [ ] 4.3 創作頁：`script-draft.js`、`script-workshop.js`、`workshop.html` 以「綁定安全模式」勾選框取代三選一下拉與年齡上限提示（預設未勾、舊 `standard` 草稿顯示未勾並於儲存時為 `adult`、唯讀版本禁用、說明限制文字）；改寫 Node 與頁面測試並通過
- [ ] 4.4 瀏覽器冒煙（360／390／768／1280 px）：目錄詢問流程（前往設定→開關→返回精靈頁、維持安全模式、取消）、卡片綁定標籤、創作頁勾選；無水平溢出與純白文字；記入 README 驗收紀錄
- [ ] 4.5 本批收尾：Node 全套與相關 pytest 通過；更新 `docs/前端結構-frontend.md`、`docs/RPG.md`；更新 HANDOFF.md；列出簡報衝突

## 5. 遊戲內提示

- [ ] 5.1 後端提示判定與記錄：`game_service`／回合完成路徑在「玩家偏好為安全開且存檔劇本未綁定、煞車未啟用」時，以 `mentions_sexual`（輸入）或審查層因性內容擋下／降為安全尺度（輸出）判定，成功完成的回合以 `sessions.safe_hint_at is null` 的條件式更新搶先記錄，更新 1 列才在 `state` 事件的 `notices` 加入 `safe_mode_hint`；`rpg_events` 新增只含編號與原因列舉的事件；測試涵蓋：每存檔一次、換裝置不重複、綁定／已關閉／煞車不提示、被擋或中斷回合不記錄、重生不重複、事件順序仍為 `meta…state、done`、日誌不含文字，並通過
- [ ] 5.2 前端提示：`game-state.js` 的 `NOTICE_KINDS` 與 `noticeText` 擴充 `safe_mode_hint`（固定繁體中文、不保證結果的措辭、帶前往 `/account` 的返回連結與關閉鈕、純文字渲染），`game-session.js` 顯示；Node 測試涵蓋措辭禁字（無「保證」「一定」）、連結、關閉不改設定並通過
- [ ] 5.3 本批收尾：相關 Node 與 pytest 通過；更新 `docs/RPG.md` 的遊戲內提示段落；更新 HANDOFF.md；列出簡報衝突

## 6. 全套驗證、部署與驗收

- [ ] 6.1 全套 pytest 與 Node 全套通過（背景執行、輸出存檔；記錄數字）；`openspec validate safe-mode-toggle --strict` 通過；變異測試（至少：生效模式的三項取最保守、緊急煞車、同意版本失效、提示每存檔一次、返回位置驗證）逐一破壞並確認測試會失敗（變異腳本設逾時，結束後比對還原）
- [ ] 6.2 展示環境部署（先備份與資料庫快照，使用者授權後才執行）：`aws_demo.py deploy`（`--tag` 明確帶）→ 遷移 0020 → `flask seed-scripts` → 重啟 → 驗證 `/healthz`、`/readyz`、日誌無 ERROR；主機環境檔確認沒有 `SAFE_MODE_FORCED`、也沒有 `ADULT_MODE_*`；寫下並演練「啟用／解除緊急煞車」的操作（營運文件）
- [ ] 6.3 使用者驗收（真實瀏覽器，含手機）：新玩家預設安全開、目錄看得到全部劇本、點內建劇本出現詢問、前往設定關閉安全模式並同意後返回、遊玩出現的尺度與提示、綁定劇本恆為安全、緊急煞車啟用後全站回安全且 `/account` 開關禁用；結果記入 README 驗收紀錄（只記實際回報的項目，未驗證處照實標註）
- [ ] 6.4 更新 `docs/` 對應的設計文件（對照表見 `docs/README.md`：`系統架構-architecture.md`、`資料模型-data-model.md`、`前端結構-frontend.md`、`帳號與安全-auth-security.md`、`營運與部署-operations.md`〔緊急煞車與 `launch-gates` 關卡改列〕、`RPG.md`、`路線圖-roadmap.md`），並讓 `tests/test_design_docs.py`、`tests/test_readme.py`、`tests/test_agents_skill_sync.py` 通過

## Workflow follow-up

- 簡報衝突（`docs/2.0簡報.md`、六張圖解與 `build_diagrams*.py` 寫「規範兩種、程式仍三種」）依 deck-conflict-watch 只通知使用者，由使用者決定，不自行修改。
- 使用者驗收通過後，以 `/openspec-archive-change safe-mode-toggle` 封存；封存後更新 `docs/路線圖-roadmap.md` 並複查各設計文件標頭的「對應 change」。
- 之後可另立 contract change：移除 `sessions.content_tier`、`users.is_adult_at_confirmation` 等不再被讀取的欄位。
