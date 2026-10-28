# Design

## Context

動機與範圍見 [proposal.md](proposal.md)；行為要求見各份差異規格。這裡只說明現況中影響做法的部分：

- `users` 已有 `terms_version`、`terms_accepted_at`（可為空，建立帳號時由 Service 寫入；管理員指令建立的帳號為空）。年齡確認沿用同一套慣例。
- 「能不能用 AI 功能」目前只由 `AccessPolicy.can_use_chat(user)` 判斷（狀態為 `active`，信箱驗證開啟時須已驗證）。**目前沒有任何端點呼叫它**（客服端點是 C-3 才建立），只有測試在用。端點不得自行寫權限判斷（access-control 規格）。
- Service 與端點之間的資料契約是 `CurrentUser` 快照；`public_dict()` 是唯一對外的帳號資料。`ServiceError` 的 `code` 會原樣進入 JSON 錯誤回應，所以新增 403 的機器可讀代碼不需要改控制器。
- Google 登入的流程資料存在簽章的 `oauth_tx` cookie（10 分鐘），內容是一個沒有結構限制的字典，現有的 `t` 旗標代表「發起時已同意條款」。回呼頁的錯誤只用一組固定代碼，各有固定的繁體中文訊息。
- 日誌事件名稱是固定列舉（`app/observability/security_events.py`），原因或來源寫在名稱最後一段，`log_event` 只接受已登記的名稱，因此不可能夾帶自由文字。
- 資料庫雙軌（SQLite、SQL Server），migration 自成一體、不匯入應用程式、採 expand/contract。
- AGENTS.md 目前沒有年齡確認的條文；它是唯一權威來源，修訂須經使用者批准。

## Goals / Non-Goals

**Goals:**
- 讓「已確認年滿 18 歲」成為所有 AI 功能的前提，且判斷集中在一處，之後的客服與 RPG 端點不必重寫。
- 記錄剛好足夠的資訊（是否確認、何時、依據哪一版政策），不多收個資。
- 日後改用更嚴格的確認方式時，不需要改資料結構。

**Non-Goals:**
- 證件、信用卡或第三方的年齡驗證。
- 政策版本改變後的強制重新確認（只預留資料，不實作行為）。
- 內容分級、成年角色閘門（RPG 的 RC-1 至 RC-3）。
- 建立任何 AI 端點（C-3 之後）。
- Google 登入時，替「已存在但未確認」的帳號隱含補確認（見風險）。

## Decisions

### D1　兩個可為空的欄位，而不是布林

`users.age_confirmed_at`（UTC 時間）與 `users.age_policy_version`（`Unicode(40)`），皆可為空。「已確認」定義為 `age_confirmed_at` 不為空。政策版本是 `app/services/legal.py` 的常數（暫定 `2026-10-28-self-declared`），由伺服器決定。

- 為什麼：完全比照 `terms_*` 的慣例；`NULL` 自然代表「升級前的帳號與管理員指令建立的帳號」，不需要回填；版本字串讓日後改用更嚴格的方式時（例如版本改為 `…-verified`）能區分舊的自行聲明紀錄。
- 替代方案：(a) 布林 `is_adult`：丟失時間與依據，日後無法區分，否決。(b) 儲存生日或年齡：蒐集了不需要的個資，違背最小蒐集，也讓個資法的特種個資與保存期限問題變得更複雜，否決。(c) 另建 `age_confirmations` 表保留歷史：目前只需要最新狀態，過度設計，日後有需要再 expand。
- 閘門條件只看 `age_confirmed_at` 是否為空，**不比對版本**；比對版本屬於日後「強制重新確認」的 change。

### D2　閘門放在 `AccessPolicy`，並新增會拋錯的版本

`can_use_chat` 加入「已確認年滿 18 歲」；另外新增 `require_chat_access(user)`：條件不足時拋出錯誤。年齡不足時拋出 `Forbidden` 的子類別 `AgeConfirmationRequired`（`code = "age_confirmation_required"`，繁體中文訊息說明須先到帳號設定確認），其他條件不足維持原本的 `Forbidden`。

- 為什麼：access-control 規格要求權限判斷集中、端點不自行寫。只回布林的方法逼每個端點自己決定回什麼訊息；一個會拋錯的方法讓錯誤代碼在全站一致，前端可以依代碼引導使用者。
- 檢查順序：帳號狀態、信箱驗證先於年齡，因為前兩者不允許時，補確認年齡也沒有幫助。
- 因為目前沒有端點呼叫它，這個行為以**測試專用的端點**驗證（比照既有的 IDOR 測試框架做法），並把「AI 端點必須先呼叫 `require_chat_access`」寫進 README 的「撰寫新端點」一節，提醒 C-3。
- 替代方案：用裝飾器放在端點層。否決：那會把權限判斷散在端點，違反既有規格。

### D3　`CurrentUser` 加 `age_confirmed`，對外只回布林

`CurrentUser.from_model` 計算 `age_confirmed`；`public_dict()`（也就是 `/api/auth/me`）回傳 `age_confirmed`，**不回時間與版本**。

- 為什麼：前端只需要知道要不要顯示確認區塊；時間與版本是內部稽核資料，沒有理由外露（規格同樣要求）。

### D4　註冊：驗證在「帳號是否已存在」之前，與同意條款同一個位置

`AuthService.register` 增加 `confirm_adult` 參數，驗證與 `accept_terms` 並列：`confirm_adult is not True` 時，欄位錯誤 `confirm_adult`（「請確認您已年滿 18 歲。」）。兩者各自獨立回報。成功建立帳號時寫入 `age_confirmed_at` 與 `age_policy_version`。

- 為什麼：驗證發生在查詢帳號是否存在之前，因此不影響「註冊回應不洩漏帳號是否存在」；信箱已存在的路徑完全不變（不會因為這次勾選而替既有帳號補確認）。
- 管理員指令 `flask create-admin`：**不確認**。確認必須是使用者本人的行為，替別人建立帳號的人無法代為聲明；管理員第一次登入後在帳號設定頁補確認，與其他人一樣。（README 的建立首位管理員一節要補這一點。）

### D5　Google 流程：多帶一個旗標，新帳號兩個都要

`GoogleSignInService.begin` 多接 `confirm_adult`，交易字典多一個 `a` 旗標（與 `t` 並列）；`complete` 把兩個旗標傳進 `_resolve`。需要建立新帳號時：沒同意條款 → 沿用 `terms_required`；已同意但沒確認年齡 → 新的固定代碼 `age_required`（登入頁固定訊息「請先確認您已年滿 18 歲。」）。兩者都缺時以 `terms_required` 優先，因為前端會在離開本站前擋下，這裡只是伺服器端的最後防線。

- 日誌：新增 `auth.google_rejected.age`，與既有的 `auth.google_rejected.terms` 並列。
- 已存在帳號（身分已綁定、以信箱合併、合併未驗證帳號）的登入路徑**完全不變**，不因旗標而隱含補確認。
- 進行中的舊 cookie（沒有 `a`）視為未確認；這只會讓「新帳號」被擋回登入頁重新勾選，是安全的預設，且 cookie 只有 10 分鐘壽命。

### D6　補確認 API：條件式更新，第一次寫入為準

`POST /api/auth/confirm-adult`，需要登入；與所有非 GET 請求一樣通過 CSRF 檢查，不需要例外。請求 `confirm_adult` 必須是 `true`，否則 400 與欄位說明。Repository 以一個**條件式更新**（`WHERE id = ? AND age_confirmed_at IS NULL`，用 ORM 表達式，非原生 SQL）寫入時間與版本，並回傳受影響列數；Service 只在列數為 1 時記錄日誌事件，然後回傳最新的帳號資料。

- 為什麼：與專案已有的原子操作（權杖使用、失敗次數）同一個作法；同時送出兩次確認時不會互相覆蓋，也保證「已確認的帳號再次確認不改變原時間」。
- 不換發登入狀態：確認不改變 `token_version`，也沒有安全意義上的狀態轉換。

### D7　日誌事件

`security_events.py` 新增：`auth.age_confirmed.register`、`auth.age_confirmed.google`、`auth.age_confirmed.later`、`auth.age_gate_denied`、`auth.google_rejected.age`。來源寫在名稱最後一段，沿用既有慣例，所以日誌不會夾帶自由文字。`auth.age_gate_denied` 不含 `refused`、`rejected` 等字樣，因此是資訊層級，不會被當成警告：使用者還沒確認是正常狀態，不是異常。

### D8　前端：沿用既有元件與模式

- 註冊頁在同意條款下方新增獨立的確認勾選（文字：「我已年滿 18 歲」），錯誤顯示在自己旁邊；登入頁的 Google 入口同樣新增勾選。兩頁的 Google 按鈕在離開本站前檢查兩個勾選。
- 帳號設定頁在清單加一列「年齡確認」，尚未確認時顯示說明區塊（確認前無法使用 AI 功能）、勾選與按鈕；成功後以既有的 Toast 回報並更新狀態；沒勾選就按時不送出請求。
- 全部沿用 Vue 3 全域變數模式與既有的 `Auth.useForm`，不使用 `v-html`，初始狀態仍由伺服器以 JSON 區塊提供（`user.age_confirmed` 來自 `public_dict`）。

### D9　條款與隱私權政策（草稿）

`terms.html` 載明「僅限年滿 18 歲者使用」；`privacy.html` 說明只記錄確認時間與政策版本、不蒐集生日、年齡或證件，並更新資料保存表。**條款內容改變，因此 `CURRENT_TERMS_VERSION` 也要更新**（沿用既有的「內容修改時更新字串」慣例），這不影響已同意舊版本的帳號。

### D10　Migration 0003

`20261028_0003_users_age_confirmation.py`：以 `batch_alter_table`（SQLite 相容）新增兩個可為空的欄位；日期時間型別用與 0002 相同的 `DATETIME2(6)` 變體；不匯入應用程式程式碼、不含原生 SQL、不建索引（從不依值查詢）。`downgrade` 刪除這兩個欄位。測試在 SQLite 與 SQL Server（`project_test`）都跑，涵蓋升級、降級與欄位為空的屬性。

## Risks / Trade-offs

- **[自行聲明可能不足以符合 Gemini 條款]** 條款說的是「不得用於可能被未滿 18 歲者使用的應用」，勾選並不能阻止說謊的未成年人。→ 這是法務第 4 題與給 Google 的詢問信第 4 題要回答的問題；設計上預留版本欄位作為升級路徑；本 change 不宣稱它「足夠」，只宣稱它「已做到合理的第一步並留下紀錄」。
- **[BREAKING 的欄位]** `register` 與 `google/start` 新增必填欄位。→ 唯一的消費者是本站前端；35 處測試呼叫與容器驗收腳本要同步；README 的端點表要更新（既有的路由表一致性測試會強制）。
- **[`can_use_chat` 目前沒有正式呼叫者]** 閘門只有在 C-3 的端點真的呼叫它時才生效。→ 測試專用端點驗證行為；README 與 C-3 的任務清單明寫；`require_chat_access` 的存在讓呼叫端只需一行。
- **[Google 登入的既有未確認帳號不會隱含補確認]** 即使他們在登入頁勾了年齡確認，仍須到帳號設定再確認一次。→ 專案尚未對外開放，只影響開發階段帳號；換來的是流程簡單、沒有隱含的狀態變更。
- **[管理員帳號也要補確認]** 管理員第一次登入後在 AI 功能被擋之前要先確認。→ README 說明；這是刻意的，管理員不豁免。
- **[版本欄位沒有被使用]** 目前閘門不比對版本，欄位看起來像多餘。→ 這是為日後升級預留的最小成本；設計文件與規格都說明了用途。

## Migration Plan

1. （使用者批准後）修訂 AGENTS.md 第 10 節並同步 `references/`，建議條文見下。
2. 備份資料庫。
3. 執行 `alembic upgrade head`（`0002` → `0003`）。這是部署流程中獨立的步驟，**不在應用程式啟動時自動執行**。本機開發資料庫 `project` 與測試資料庫 `project_test` 都要執行；`project` 的變更需使用者同意。
4. 部署新版程式。舊程式在新結構上可運作（新欄位可為空、舊程式不讀取）；新程式在舊結構上不可運作，因此 migration 必須先於部署。
5. 回復：程式可直接回到舊版。`alembic downgrade` 會刪除兩個欄位與其中的資料，只適用於尚無真實確認紀錄時；之後請改用備份還原。

**建議的 AGENTS.md 第 10 節新增條文**（apply 開始時請使用者核可或修改）：

> 年齡確認：註冊（含 Google 首次註冊）須以與同意條款分開的勾選確認年滿 18 歲，記錄確認時間與政策版本（`age_confirmed_at`、`age_policy_version`），不蒐集生日、年齡或證件。尚未確認的帳號可登入與管理帳號，但不得使用任何會呼叫 Gemini 的功能，由 AccessPolicy 判斷並回 403 與代碼 `age_confirmation_required`；管理員與各會員等級不豁免。既有帳號於帳號設定頁補確認。原因：Gemini API 條款要求使用者年滿 18 歲，且不得用於可能被未滿 18 歲者使用的應用。

## Open Questions

- 勾選文字、`/terms` 與 `/privacy` 的措辭，最終由法務定稿（目前為草稿，標示清楚）。
- 政策版本改變時，是否強制重新確認、以及重新確認的時機：待法務第 4 題與 Google 的回覆之後，另案處理。
