# Design

## Context

動機與範圍見 [proposal.md](proposal.md)。現況（已封存的 `age-gate`，觀察自程式）：

- `users` 有 `age_confirmed_at`、`age_policy_version` 兩個可為空的欄位；`AuthService.register(…, confirm_adult)`、`AuthService.confirm_adult`、`GoogleSignInService.begin(…, confirm_adult)`（交易 cookie 旗標 `a`）、`UserRepository.confirm_adult_if_unset`、`POST /api/auth/confirm-adult` 都以「勾選」為單位。
- `CurrentUser.age_confirmed` 是布林；`AccessPolicy.can_use_chat`／`require_chat_access` 只看這個布林，**不看年齡**；沒有內容上限的概念。
- `flask create-admin` 是目前唯一的 CLI（`app/cli.py`）；沒有任何排程指令，也沒有 `deletion_tombstones`。
- 年齡數字（18）散在多處文字與測試中，沒有單一來源。
- AGENTS.md 第 7、10、11 節已修訂（2026-10-28），是本設計的規範依據，本 change 不修改它。

## Goals / Non-Goals

**Goals:**
- 規範與程式一致：生日加年齡區間、未滿 13 歲拒絕、13 至 17 歲安全模式、12 個月閒置清除。
- 年齡規則只有一個來源，日後（例如換 LLM 供應商）改規則是改一處、提高版本號，不改資料結構、不改頁面。
- 生日是個資：不外洩（API、日誌、模型、管理員）、有明確的保存期限。

**Non-Goals:**
- 不實作 RPG 的安全模式套用與「提醒切換分級」（RPG change）；本 change 只提供 `content_ceiling` 這個判斷。
- 不實作 `/admin` 頁面（C-4）、對話等尚不存在的資料的刪除（C-4）、`LLMProvider`（C-3）。
- 不核實年齡（證件、第三方驗證）。

## Decisions

### D1　年齡規則集中成 `AgePolicy`（程式內的設定物件，不是環境變數）

新增 `app/services/age_policy.py`：不可變的 `AgePolicy`（最低註冊年齡 13、最低使用年齡 13、成年門檻 18、年齡區間選項、政策版本），預設實例 `DEFAULT_AGE_POLICY`，以及純函式 `age_on(birth_date, today)`、`validate_age_input(raw_birth_date, raw_range, today, policy)`。`create_app` 接受 `age_policy` 參數（比照 `identity_provider`），放進 `app.extensions`，`get_age_policy()` 供 Service 與頁面取用。**只有 `AccessPolicy` 與年齡驗證讀它**；頁面的區間選項、條款裡的「13 歲」都由它在伺服器端渲染。

- 為什麼不是環境變數：規則改動必須伴隨政策版本與條款修改，是需要審查的程式變更，不是部署時的開關；且不是機密。也不放資料庫：要能在測試與 migration 之外一眼看到。
- 為什麼分「最低註冊年齡」與「最低使用年齡」：兩者目前都是 13，但改成 18 時語意不同——註冊被擋是對新人，使用被擋是對既有的 13 至 17 歲帳號（`age_restricted`），不需要刪帳號。
- 替代方案：把 13、18 寫在各處（目前的狀態，否決）；做成 DB 設定表（過度設計，否決）。

### D2　資料：沿用兩欄、新增三欄；「完成」只看生日

`users` 新增 `birth_date`（`Date`）、`age_range`（`Unicode(20)`，存列舉代碼如 `13_17`）、`is_adult_at_confirmation`（`Boolean`，僅供紀錄），皆可為空；沿用 `age_confirmed_at`（改為「生日與區間通過對照的時間」）與 `age_policy_version`。**「已完成年齡確認」= `birth_date` 與 `age_range` 都存在**，舊的 `age_confirmed_at`（勾選版）不算。是否成年一律 `age_on(birth_date, 今天) >= 18`，儲存的布林不參與任何判斷。

- 為什麼只看生日：滿 18 歲要自動解除、政策調整要能即時重算，儲存的結果會過期。使用者要求「都儲存」，所以留一欄當紀錄，但刻意不讓程式依賴它（測試會守住這一點：把它改成與生日矛盾的值，行為不變）。
- 不加索引、不依這幾欄查詢（統計用 `GROUP BY age_range`，是 ORM 表達式、與方言無關）。
- 替代方案：另建 `age_verifications` 資料表保留歷史。否決：目前只需要最新狀態，且歷史會讓保存期限更複雜。

### D3　驗證順序與錯誤

單一函式 `validate_age_input` 依序：①缺少或格式不合法（`birth_date`、`age_range` 各自的欄位錯誤，400 `validation_error`）→ ②未滿 13 歲，或區間為 `under_13`（400 `age_not_eligible`，欄位 `birth_date`）→ ③生日算出的年齡不在所選區間（400 `age_mismatch`，**無欄位**，訊息固定為「資料有誤，請重新註冊」）。通過才回傳驗證過的 `date` 與區間代碼。

- 呼叫點：`AuthService.register`（在 `_validate_new_account` 之後、查詢信箱之前，維持「已存在與不存在得到相同回應」）、`GoogleSignInService.begin`、`AuthService.verify_age`。三處共用同一個函式。
- 「未滿 13 歲不保存」是結構性的：驗證發生在任何寫入之前，且失敗只記錄事件名稱（`auth.age_rejected.underage`、`auth.age_rejected.mismatch`），輸入值不進入日誌、例外訊息或交易 cookie。
- 日曆日期以 Asia/Taipei 計（`clock()` 轉換），避免 UTC 與台北日期差一天造成生日當天判錯。
- 不一致時不指出是哪個欄位：這是使用者的決定（「資料有誤，重新註冊」）。代價是使用者不知道錯在哪，可接受。
- 不一致**不留下任何後果**（使用者決定：常是輸入太快）：驗證在任何寫入之前，失敗只記事件名稱；不新增失敗計數、不鎖信箱、不寫任何旗標，所以同一個信箱與密碼可立刻再註冊。測試以「失敗一次後立即以同一信箱成功」守住。

### D4　權限政策：年齡門檻、內容上限、檢查順序

`AccessPolicy` 注入 `AgePolicy` 與時鐘。`require_chat_access` 順序：帳號狀態與信箱驗證 → 尚未完成年齡確認（`AgeConfirmationRequired`，沿用 `age_confirmation_required`）→ 年齡低於最低使用年齡（新的 `AgeRestricted(Forbidden)`，`age_restricted`）。兩種年齡拒絕都記 `auth.age_gate_denied`。新增 `content_ceiling(user) -> ContentCeiling(sexual_content_level, safe_mode)`：未滿 18 歲 `(0, True)`；成年 `(2, False)`（3 維持鎖定，常數與 AGENTS.md 的鎖一致）；尚未完成者保守地回 `(0, True)`。`CurrentUser` 新增內部欄位 `birth_date`（不進 `public_dict`）；`age_confirmed` 由它推得；`public_dict` 另接受 `age_group`（由 `AuthService.profile` 以時鐘算出 `minor`／`adult`／空值）。

- 為什麼 `CurrentUser` 帶 `birth_date` 而不是 `is_adult`：快照每個請求都由資料庫重建，帶原始資料讓判斷永遠是「當下」。

### D5　Google 流程：驗證在發起時，資料放進已簽章的交易 cookie

`begin(accept_terms, birth_date, age_range)` 先呼叫 `validate_age_input`（失敗直接 400，不離開本站），通過後把 ISO 生日與區間代碼放進交易（鍵 `b`、`r`，取代 `a`）。`complete` 建立新帳號前**再驗證一次**（不信任 cookie 內容，也處理跨日邊界），失敗走 `age_required`。既有帳號登入不讀這些鍵、不寫年齡欄位。交易 cookie 本來就是簽章、HttpOnly、10 分鐘、回呼結束刪除。

- 替代方案：把生日只存在伺服器端 session。否決：專案刻意不用伺服器 session（多 worker、無共用狀態），交易 cookie 是既有機制。
- 風險：生日在簽章但未加密的 cookie 中（使用者自己的瀏覽器看得到）。接受：內容是使用者自己填的，且 10 分鐘內即刪除。

### D6　補填取代補確認

`POST /api/auth/verify-age`（取代 `/confirm-adult`）：需要登入、驗證同註冊、`UserRepository.complete_age_verification_if_unset(user, …)` 以條件式 UPDATE（`WHERE birth_date IS NULL`）一次寫入全部欄位，回傳是否寫入，並發只有一個成功；已完成者不改動（回成功）。事件 `auth.age_confirmed.later`。

### D7　閒置帳號清除

`flask purge-inactive-accounts [--dry-run]`（`app/cli.py`）→ `AccountRetentionService`：以時鐘算 `cutoff = now − 365 天`；Repository 取出 `role <> admin` 且 `coalesce(last_login_at, created_at) < cutoff` 的內部編號（ORM 的 `func.coalesce`，不寫原生 SQL）；每個帳號以**條件式 DELETE**（同樣的條件再寫一次，rowcount 為 1 才算數）刪除，成功才寫入 `deletion_tombstones`（`user_id`、`deleted_at`、`reason='inactive_12_months'`），同一個交易提交；`user_identities`、`auth_tokens` 靠既有的 `ON DELETE CASCADE`。預覽模式只計數。輸出只有筆數；每個被刪帳號記一筆只含內部編號的事件。

- 為什麼條件式 DELETE：避免「判斷後、刪除前剛好登入」的競態（多 worker、排程與使用者並行），與專案其他原子操作（`_run_update`）同一種做法。
- **管理員排除**：見 proposal 假設；理由是否則唯一的管理員閒置一年就消失。
- **刪除範圍會隨 C-3、C-4 增加**（對話、訊息等都有指向 `users.id` 的外鍵）。為了不讓日後新增的資料表悄悄擋住或漏掉清除，新增一個架構測試：所有指向 `users.id` 的外鍵都必須是 `ON DELETE CASCADE` 或 `SET NULL`（`llm_call_logs` 的匿名化用 `SET NULL`），否則測試失敗並說明要補哪裡。
- 替代方案：先進 `deletion_pending` 再刪。否決：使用者明確要求「直接刪除」；一年沒登入的帳號沒有需要撤銷的對象。

### D8　彙總統計用指令，不做 API

`flask age-stats` 輸出各 `age_range` 人數與尚未完成人數（`UserRepository.count_by_age_range()`，`GROUP BY`，只回計數）。後台頁面屬 C-4，屆時直接呼叫同一個方法；這裡刻意不新增任何會回傳生日的 API，並以測試掃描所有路由的回應欄位（對 `/api/auth/*` 與頁面初始狀態）確認不含生日。

### D9　前端

- 註冊頁：生日用原生日期欄位（`max` 為伺服器給的今天、`min` 1900-01-01），年齡區間為 `<select>`，選項與標籤來自頁面初始狀態 `ageRules`；移除年齡勾選。登入頁的 Google 入口與帳號設定頁補填表單使用同樣的兩個欄位。
- `Auth.describeError` 已提供 `code`：收到 `age_mismatch` 時顯示提醒並讓註冊頁回到初始狀態——清空生日、區間與密碼，**保留電子郵件與顯示名稱**，使用者可立即重新送出；收到 `age_not_eligible` 時清空生日並顯示說明，不提示可以改填。Google 入口在發起時遇到不一致，同樣只顯示提醒與清空兩個欄位，不離開本站。
- 帳號設定頁：13 至 17 歲顯示安全模式提示；不顯示生日、區間。`/api/auth/me` 帶 `age_group`。
- 條款與隱私權政策是伺服器渲染的 Jinja 範本，年齡數字全部來自 `AgePolicy`，範本內不得出現寫死的「13」「18」。

### D10　測試支援與既有測試的遷移

新增 `tests/support/age.py`：以時鐘的 Taipei 日期產生「N 歲」的生日與對應區間的輔助函式，並提供預設通過的 `age_fields()`。既有約 10 個測試檔、以及 `scripts/verify_container.py` 的 `confirm_adult` 一律改用它。`add_user` 的測試輔助新增 `adult=True`（寫入通過的年齡欄位）以保持多數與年齡無關的測試簡短。

## Risks / Trade-offs

- **與 Gemini 條款不符**（條款只有 18 歲門檻）→ 只在個人測試期間（僅有成年的本人）使用；D1 讓改回 18 歲只需要修改 `AgePolicy` 與提高版本；`age_restricted` 讓既有的 13 至 17 歲帳號立即被擋；對外開放前須以實際供應商條款重新確認（RPG.md T-10）。
- **生日是個資**，存放在資料庫與（短暫地）交易 cookie → 不進 API、日誌、模型、管理員畫面；12 個月閒置即刪；隱私權政策揭露；匯出含生日（當事人權利）；未滿 13 歲者的輸入不保存。
- **自行聲明的固有限制**：未成年者可以改填成年資料重試（「資料有誤」沒有重試限制）→ 已記為已知限制，入口層限流屬 C-5；不嘗試核實。
- **SQLite 的 rowid 可能被重用**：`deletion_tombstones` 只存內部編號，若在 SQLite 上還原備份後重新執行刪除，可能誤刪重用同一編號的新帳號 → SQL Server（正式環境）的 IDENTITY 不重用；SQLite 只用於測試與本機展示，記錄於此，不為它加 `AUTOINCREMENT`。
- **生日當天的時區**：以 Asia/Taipei 的日曆日期計算；使用者在其他時區註冊的生日當天可能差一天 → 接受，政策版本可記錄計算方式。
- **勾選版時代的帳號變成「未完成」** → 目前沒有真實帳號（開發資料庫 `users` 為 0 筆）；若日後有，補填表單即是升級路徑。
- **管理員永不被閒置清除**可能違反「12 個月內刪除個資」的字面意思 → 管理員帳號的個資是營運必要資料；已記為假設，可推翻。
- **本 change 會讓程式再次與 `age-gate` 的規格不同**（歸檔後的主規格寫的是勾選版）→ 這個 change 的差異規格就是用來修正它，歸檔時一併同步。

## Migration Plan

1. 備份開發資料庫（使用者已同意此流程；需再次同意才升級 `project`）。
2. `alembic upgrade head` → `0004`：`users` 新增三個可為空的欄位；新增 `deletion_tombstones`（expand；舊程式忽略它們，所以順序是先 migration、再部署）。不回填：沒有真實帳號，勾選版的紀錄不算完成。
3. 部署新程式；排程器註冊 `flask purge-inactive-accounts`（C-5 的部署工作，這裡只提供指令）。
4. 回復：`alembic downgrade 0003`（`recreate="never"`，只刪新欄位與新資料表；已有真實生日之後不可使用，改用備份還原）。
5. 之後的 contract：沒有；舊的 `age_confirmed_at` 仍在使用。
