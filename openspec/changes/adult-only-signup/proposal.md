# Proposal

## Why

平台的內容方向改為全站只限滿 18 歲者使用（AGENTS.md 第 7、10、11 節已於 2026-11-17 修訂並提交為 `4960372`）：服務含成年角色之間的露骨內容，而且 Gemini API 條款本來就要求使用者年滿 18 歲，所以「13 至 17 歲可使用、只能用安全模式」的設計不再成立。程式與規格目前仍是 13 歲門檻，登入頁也只有條款勾選，沒有年滿 18 歲的聲明。

這個 change 只處理「誰可以進來」：最低年齡改為 18、登入頁多一個年滿 18 歲的勾選、未滿 18 歲者立即抹除、條款與隱私權政策的年齡說明。內容分級改成兩種模式（安全模式開／關）是另一個 change（`safe-mode-toggle`），不在這裡。

2026-11-24、11-25 要實際展示全部功能，所以這個 change 要先於 11-23 部署到展示環境。

## What Changes

- **最低年齡改為 18**：`AgePolicy` 的最低註冊年齡、最低使用年齡都是 18，政策版本提高。年齡區間選項照舊（保留「未滿 13 歲」與「13–17」），選到或由生日算出未滿 18 歲，一律回 `age_not_eligible` 並立即抹除該帳號與全部個資（走原本未滿 13 歲的出口），不保存其填寫的生日與暱稱，只回通用訊息。
- **登入頁多一個勾選**：新使用者必須同時勾選「同意服務條款與隱私權政策」與「我確認已滿 18 歲」。任一未勾選就不建立帳號，導回登入頁提示。回訪的使用者仍然一鍵登入。年滿 18 歲聲明的時間與版本記入帳號（新增兩個可為空的欄位，遷移 `0019`，只擴張不收縮）。
- **年齡確認頁照舊**：仍要填生日、年齡區間與暱稱，用來實際擋掉未滿 18 歲；頁面加上「本服務僅限滿 18 歲者使用」的提示。
- **預設內容模式一律為安全**：年齡確認成功時，所有新帳號的內容模式偏好都是 `safe`（原本成年者是 `standard`），對應 AGENTS.md「預設為安全模式開」。玩家仍可用現有的個人頁開關調整；三種模式改兩種留給 `safe-mode-toggle`。
- **移除未成年的特例**：帳號設定頁不再有「13 至 17 歲目前為安全模式」的提示；不再記錄「確認當時是否已滿 18 歲」（`is_adult_at_confirmation` 欄位保留不寫入，日後收縮）；規格中「13 至 17 歲可使用」的情境改為被拒絕。
- **條款與隱私權政策**：`/terms` 載明僅限滿 18 歲、聲明年滿 18 歲者須如實聲明並自行承擔責任、未滿 18 歲者不得使用且帳號與資料會立即刪除；`/privacy` 的生日用途拿掉「內容分級」、未滿 13 歲改為未滿 18 歲。條款版本提高。露骨內容的告知與供應商留存的告知，等 `safe-mode-toggle` 讓關閉安全模式真的成為功能時再加（避免在功能出現前就對使用者作不實的陳述）。
- **測試玩家與文件**：`scripts/mint_test_sessions.py` 簽出的測試玩家要帶年滿 18 歲聲明；個人資料匯出納入聲明的時間與版本；`project資料結構文件.md`、`資料模型`、`帳號與安全`等設計文件同步。
- **BREAKING**：未滿 18 歲者不能再註冊或使用（既有帳號只有管理員，管理員不受影響，沒有資料要遷移）。Google 登入發起請求多一個 `declare_adult` 欄位，登入頁部署前已經開啟的授權流程（交易 cookie 沒有聲明旗標）會被拒絕並要求重新勾選。

## Capabilities

### New Capabilities

（無。全部是既有能力的需求修改。）

### Modified Capabilities

- `age-confirmation`：最低年齡 18；未滿 18 歲不得註冊（取代「未滿 13 歲不得註冊」）；13 至 17 歲不再可使用；預設內容模式一律為 `safe`；不再記錄確認當時是否成年；條款與隱私權政策的揭露；年齡規則集中設定的情境；生日區間對照的情境。
- `post-login-age-confirmation`：補填時未滿 18 歲立即抹除（取代未滿 13 歲）；首次登入建立帳號的情境改為兩個勾選；首次贈幣的「未滿 13 歲」情境改為 18。
- `google-sign-in`：首次註冊須同時同意條款並聲明年滿 18 歲，記錄聲明的時間與版本；發起請求的欄位與交易 cookie。
- `auth-pages`：登入頁有兩個勾選框與對應的錯誤提示；年齡確認頁的提示與被抹除說明改為未滿 18 歲；帳號設定頁不再有 13 至 17 歲的提示。
- `access-control`：政策的「13 至 17 歲可使用客服」情境改為被拒絕（`age_restricted`）。
- `content-tier-modes`：移除「帳號頁對未成年者顯示安全模式的提示」。
- `player-nickname`：未滿 13 歲被抹除的情境改為 18。
- `service-observability`：年齡確認事件改為未滿 18 歲，新增「未勾選年滿 18 歲聲明而被拒絕建立帳號」事件。
- `personal-data-export`：匯出納入年滿 18 歲聲明的時間與版本。
- `cs-nlp-contract`：客服提示詞的平台資料改為如實描述兩個勾選框、生日只用於年齡確認與去識別的統計，並刪除「未成年者可使用但只能使用安全模式」的區間（第 3 批之後由使用者決定併入，見 tasks 6.4）。

## Impact

- **程式**：`app/services/age_policy.py`（最低年齡與版本）、`app/services/google_sign_in_service.py`、`app/controllers/auth_api_controller.py` 與頁面控制器（聲明旗標、錯誤碼）、`app/services/auth_service.py` 與 `app/repositories/user_repository.py`（預設偏好與停寫成年紀錄）、`app/models/user.py`、`app/services/legal.py`（條款版本）、`app/services/personal_export_service.py`、`app/services/cs_prompt.py`（客服提示詞的平台資料）、`app/observability/` 事件、`scripts/mint_test_sessions.py`。
- **前端與樣板**：`app/templates/auth/login.html`、`age.html`、`account.html`、`legal/terms.html`、`legal/privacy.html`；`app/static/js/auth/auth-flows.js`、`account.js`。
- **資料庫**：遷移 `0019` 新增 `users.adult_declared_at`、`users.adult_declaration_version`（可為空；遷移使用 ORM 與 Alembic op，沒有原生 SQL）。展示環境的遷移要依 `aws_demo.py` 的流程（先快照）。後續的 `rpg-ops-loadtest` 若有遷移，編號順延為 `0020`。
- **文件**：`docs/project資料結構文件.md`（逐欄字典與第 14 節異動紀錄）、`docs/資料模型-data-model.md`、`docs/帳號與安全-auth-security.md`、`docs/前端結構-frontend.md`、`docs/路線圖-roadmap.md`、`README.md`。
- **不在範圍（交給 `safe-mode-toggle`）**：內容分級由三種改兩種、關閉安全模式的流程與緊急煞車開關、劇本「綁定安全模式」與目錄通知、遊戲內的提示、`AccessPolicy` 依年齡的內容上限與遊戲相關規格中「13 至 17 歲玩家」的情境（`rpg-entry-api`、`rpg-entry-pages`、`rpg-game-screen`、`rpg-sessions`；這些玩家被閘門擋下，情境不會發生但不會錯）、露骨內容與供應商留存的政策告知。
- **相依與時程**：沒有新相依。2026-11-23 前部署到展示環境；順序在 `safe-mode-toggle` 與 `rpg-ops-loadtest` 之前。
