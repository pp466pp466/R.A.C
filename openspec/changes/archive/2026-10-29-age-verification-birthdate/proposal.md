# Proposal

## Why

使用者於 2026-10-28 修訂了 AGENTS.md 第 7、10、11 節：年齡確認從「勾選我已年滿 18 歲」改為**生日加年齡區間（自行聲明、互相對照）**，並決定未滿 13 歲不得註冊、13 至 17 歲可使用但固定為安全模式（L0）、帳號連續 12 個月未登入即刪除。已封存的 `age-gate` 只實作了勾選版，因此程式與規範之間有已知落差（見 [RPG.md](../../../docs/RPG.md) T-10、R-15.4）。這個 change 補上落差，並且讓日後改年齡規則（例如換成條款更嚴格或更寬鬆的 LLM 供應商）只需要改一處設定。

目前的設定（13 歲以上可用）**與 Gemini API 條款不符**（條款只有 18 歲門檻，已於 2026-10-28 重讀原文確認）。使用者決定僅在個人測試期間（只有本人、已成年）使用這個設定，對外開放前以實際供應商的條款重新確認；本 change 的設計因此把年齡規則集中成單一設定。

## What Changes

- **註冊改填生日與年齡區間，移除「我已年滿 18 歲」勾選**：信箱註冊與首次以 Google 註冊都要填生日、從下拉選單選年齡區間。伺服器依生日計算年齡並做對照：
  - 未滿 13 歲：不得註冊，不建立帳號，**也不保存其填寫的生日**。
  - 生日與年齡區間不一致：一律回「資料有誤，請重新註冊」，不建立帳號。
  - **BREAKING**：`POST /api/auth/register` 與 `POST /api/auth/google/start` 的 `confirm_adult` 欄位改為 `birth_date`（`YYYY-MM-DD`）與 `age_range`（列舉代碼）；`POST /api/auth/confirm-adult` 改為 `POST /api/auth/verify-age`（補填）。專案尚無真實使用者，不需要相容舊欄位。
- **儲存**：`users` 新增 `birth_date`、`age_range`、`is_adult_at_confirmation`（僅供紀錄）三個可為空的欄位；沿用 `age_confirmed_at` 與 `age_policy_version`（政策版本提高，區分勾選版的舊紀錄）。**是否已滿 18 歲一律由 `birth_date` 於當下計算**，不以儲存的結果判斷，使用者滿 18 歲後限制自動解除。
- **年齡規則集中成單一設定**（最低註冊年齡 13、成年門檻 18、年齡區間選項、政策版本），只由權限政策與年齡驗證讀取。日後把最低年齡改成 18，就等於「未成年不得使用」，不需要改動多處程式。
- **權限政策**：`can_use_chat`／`require_chat_access` 改為「帳號可用、已完成年齡確認、且年齡不低於最低使用年齡」；新增 `content_ceiling(user)`：未滿 18 歲固定安全模式（`sexual_content_level` 0），成年者上限為 2（3 仍鎖定）。尚未完成年齡確認的帳號（管理員指令建立的帳號、勾選版時代的帳號）沿用 `age_confirmation_required`，改在帳號設定頁補填生日與區間。
- **前端**：註冊頁以生日與年齡區間取代年齡勾選；登入頁的 Google 入口同樣要填；帳號設定頁補填，並對未成年者顯示安全模式提示；`/api/auth/me` 回 `age_confirmed` 與 `age_group`（`minor` 或 `adult`），**不回生日與區間**。
- **閒置帳號清除**：新增排程指令，連續 12 個月未登入的帳號直接刪除（不經 7 天寬限期），並寫入 `deletion_tombstones`（僅存內部 ID 與刪除日期，供備份還原後重新執行刪除）。
- **了解用戶群**：新增只輸出各年齡區間人數的指令（不含任何個別生日）；管理員後台的呈現屬 C-4。
- **條款、隱私權政策與匯出**：條款改為「13 歲以上；13 至 17 歲僅限安全模式」；隱私權政策揭露蒐集生日與年齡區間、使用目的（年齡確認、內容分級、以去識別的統計方式了解用戶群）、保存期限（連續 12 個月未登入即刪除）；日誌絕不記錄生日與年齡區間。
- **文件與驗收**：README、`docs/spec_report`、`docs/RPG.md` 的 T-10 狀態、Edge 無頭瀏覽器驗收、容器驗收、migration `0004` 與開發資料庫升級（先備份、需使用者同意）。

### 假設（已記錄，可在 review 時推翻）

- 年齡區間選項為：未滿 13 歲、13–17、18–24、25–34、35–44、45–54、55 以上。包含「未滿 13 歲」是為了讓誠實的兒童得到明確的拒絕訊息，而不是「資料有誤」。區間可日後調整，調整時提高政策版本。
- 生日以原生日期輸入欄位填寫；年齡一律以 Asia/Taipei 的日曆日期計算。
- **管理員帳號不納入閒置清除**：否則唯一的管理員閒置一年就會被刪除，且沒有人能再建立（`flask create-admin` 需要部署者手動執行）。AGENTS.md 第 11 節沒有明寫例外，這是我加的假設，可推翻。
- 勾選版時代（`age-gate`，`age_policy_version` 為舊版）的確認紀錄**不視為已完成**：完成與否只看 `birth_date` 是否存在。專案目前沒有真實帳號，不做回填。
- Google 首次註冊時，生日與區間在 `/api/auth/google/start` 就驗證（未滿 13 歲與不一致都在離開本站前被擋下），驗證後的值放在已簽章的 10 分鐘交易 cookie，回呼成功建立帳號時才寫入資料庫、回呼結束一律刪除。
- 「資料有誤」常是輸入太快所致（使用者決定）：**不封鎖、不鎖定、不標記該信箱，也不累計次數**，提醒後回到註冊頁，保留電子郵件與顯示名稱，清空生日、區間與密碼，使用者可立即以同一個信箱重新註冊。因此重試沒有額外限制；自行聲明本來就無法防止未成年者改填成年資料，這是已知的固有限制，入口層限流屬 C-5。

## Capabilities

### New Capabilities
- `account-retention`: 閒置帳號的保存期限與清除：連續 12 個月未登入即刪除、管理員不納入、刪除紀錄（tombstone）、以排程指令執行且不依賴行程內計時器。

### Modified Capabilities
- `age-confirmation`: 由「勾選確認、不蒐集年齡」改為「生日加年齡區間對照」：紀錄內容、未滿 13 歲與不一致的處理、補填、未成年的安全模式、年齡規則集中設定、隱私揭露、只提供彙總統計。
- `member-registration`: 「註冊輸入驗證」把年齡勾選換成生日與年齡區間。
- `google-sign-in`: 「首次以 Google 註冊須確認年滿 18 歲」改為須通過生日與年齡區間的驗證。
- `access-control`: 「權限判斷集中在政策物件」的使用條件與新增的內容上限。
- `auth-pages`: 「登入與註冊表單」與「帳號設定頁」改為生日與年齡區間，並顯示未成年的安全模式提示。
- `session-authentication`: 「查詢目前登入者」回應改為 `age_confirmed` 與 `age_group`。
- `service-observability`: 「年齡確認事件的記錄」新增被拒絕的事件，並明定不得記錄生日與年齡區間。

## Impact

- **資料庫**：migration `0004`：`users` 新增三個可為空的欄位、新增 `deletion_tombstones` 資料表（expand；舊程式忽略新欄位）。升級流程：備份、先 migration、再部署。生日用 `Date` 型別，兩種資料庫行為一致，不依 JSON 欄位查詢。
- **程式**：`app/models/user.py`（與新的 tombstone 模型）、`app/repositories/user_repository.py`、`app/services/{age_policy（新）,auth_service,google_sign_in_service,access_policy,current_user,legal,errors}.py`、`app/observability/security_events.py`、`app/controllers/{auth_api_controller,auth_page_controller}.py`、`app/cli.py`（兩個新指令）、`app/templates/auth/{register,login,account}.html`、`app/templates/legal/{terms,privacy}.html`、`app/static/js/auth/{register,login,account}.js`、`app/static/css/auth.css`。
- **API**：`/api/auth/register`、`/api/auth/google/start` 的必填欄位改變（BREAKING）；`/api/auth/confirm-adult` 移除、新增 `/api/auth/verify-age`；`/api/auth/me` 新增 `age_group`；新的錯誤代碼 `age_not_eligible`、`age_mismatch`、`age_restricted`。
- **測試**：既有使用 `confirm_adult` 的註冊呼叫（約 10 個測試檔）、年齡閘門測試、log-scan 完整流程、Edge 與容器驗收腳本都要同步。
- **文件與規範**：AGENTS.md 第 7、10、11 節與 skill 鏡像已於 2026-10-28 修訂完成，本 change 不再修改；`docs/RPG.md` T-10、README、`docs/spec_report_v2.2.0.md`。
- **不在範圍內**：RPG 端的「玩家輸入曖昧內容時提醒切換分級」與實際套用安全模式（隨 RPG change）；管理員後台與 `/api/admin/*`（C-4，本 change 只提供彙總統計的指令）；對話、`llm_call_logs` 等尚未存在的資料的刪除（C-4）；`LLMProvider` 介面與 Gemini 呼叫（C-3）；證件或第三方年齡驗證；因為供應商條款而必須把最低年齡改為 18 的決定（只提供「改設定即可」的能力）。
