# Proposal

## Why

Gemini API 附加條款（2026-03-23 生效）規定使用者須年滿 18 歲，且不得把服務用於「directed towards or is likely to be accessed by individuals under the age of 18」的網站或應用程式。目前的註冊流程沒有任何年齡確認，一旦客服或 RPG 對外開放，整個專案的 Gemini 帳號（濫用監控頁指出嚴重違規可能波及其他 Google 服務）就處於違規狀態。這與後續是否提供成熟內容無關，是**所有 AI 功能的共同前提**，必須在 C-3 讓客服開始呼叫 Gemini 之前、也就是任何對外開放之前完成。依據：[RPG.md](../../../docs/RPG.md) 的 R-15.4、RA-10 與 6.3 節 T-1，以及 [content-policy-research.md](../../../docs/content-policy-research.md) 第 2 章。

## What Changes

- **註冊必須明確確認年滿 18 歲**：信箱註冊與首次以 Google 註冊都要求一個與「同意條款」分開的確認，未確認就不建立帳號。**BREAKING**：`POST /api/auth/register` 與 `POST /api/auth/google/start` 新增必填欄位 `confirm_adult`，沒帶或不是 `true` 會得到 400。
- **記錄確認，但不蒐集年齡**：`users` 新增「確認時間」與「確認當時的政策版本」兩個可為空的欄位；不收生日、年齡或證件。政策版本由伺服器決定，日後改用更嚴格的確認方式時，可藉由提高版本區分舊紀錄並要求重新確認。
- **既有帳號補確認**：升級前已存在的帳號、管理員指令建立的帳號都視為尚未確認。它們仍可登入與管理自己的帳號，但在確認前不能使用 AI 功能；新增登入後的補確認 API（`POST /api/auth/confirm-adult`）與帳號設定頁的確認區塊。
- **未確認不得使用 AI 功能**：把「已確認年滿 18 歲」納入集中的權限政策（`can_use_chat`），不允許時回 403 與代碼 `age_confirmation_required`，讓前端能引導使用者補確認。管理員與各會員等級都不豁免。C-3、RPG 之後的 AI 端點只要沿用這個政策即可。
- **前端**：註冊頁與登入頁的 Google 入口新增獨立的勾選；帳號設定頁顯示確認狀態與補確認；`/api/auth/me` 回傳是否已確認（只回布林值）。
- **條款與隱私權政策（草稿）**補上：僅限年滿 18 歲者使用；只記錄確認時間與政策版本，不蒐集生日、年齡或證件。
- **日誌**新增兩個列舉事件（確認完成、因未確認被拒），不含 email 與其他個資。
- **密碼長度下限由 10 降為 8**（2026-10-28，使用者手動修訂 AGENTS.md 第 10 節）：這是與年齡確認無關的規則修訂，因為本 change 的 `member-registration` 差異規格本來就會改寫「註冊輸入驗證」這條需求（歸檔時會整條覆蓋主規格），為避免歸檔後主規格退回舊的 10 至 128，一併在這裡改為 8 至 128。只放寬註冊、改密碼與重設密碼的輸入規則，既有的雜湊不受影響，登入不檢查下限。
- **文件**：README、`docs/spec_report_v2.2.0.md` 的 users 表、[RPG.md](../../../docs/RPG.md) 的 T-1 狀態；並須在 apply 開始時請使用者批准修訂 AGENTS.md 第 10 節（RA-10）與同步 `references/`（AGENTS.md 是唯一權威來源，**不自行修改**）。

### 假設（已記錄，可在 review 時推翻）

- 確認方式先採**自行聲明**（勾選），不做證件或第三方驗證。法務問題第 4 題（自行聲明是否足夠）尚未有答案，因此設計上預留升級路徑（政策版本），但本 change 不實作更嚴格的驗證。
- 專案尚未對外開放，沒有需要保留的真實使用者，因此既有帳號一律走「補確認」，不做資料回填。
- 年齡確認與同意條款是兩個**各自獨立**的勾選，不合併，以便日後各自變更版本。
- **2026-10-28 的後續修訂**：使用者手動修訂 AGENTS.md 第 10 節，年齡確認改為「蒐集生日做對照、年齡（給下拉式選項給玩家選取範圍）、不給證件」，也就是上面「不收生日、年齡」的假設被推翻。本 change **維持已實作的自行聲明（勾選）**，生日與年齡區間的蒐集、對照、保存、隱私權政策與補確認流程，由後續另開的 change 處理（見 tasks.md 的 Workflow follow-up）；在那之前，程式與 AGENTS.md 第 10 節的年齡條文有已知的落差。

## Capabilities

### New Capabilities
- `age-confirmation`: 使用者對「已年滿 18 歲」的確認：如何記錄（不蒐集年齡）、既有帳號如何補確認、未確認時不得使用 AI 功能（含錯誤代碼），以及條款與隱私權政策中的揭露。

### Modified Capabilities
- `member-registration`: 「註冊輸入驗證」改為同時要求同意條款與確認年滿 18 歲，兩者各自獨立。
- `google-sign-in`: 新增「首次以 Google 註冊須確認年滿 18 歲」；既有帳號的 Google 登入不需要在此確認，但補確認前同樣不能使用 AI 功能。
- `access-control`: 「權限判斷集中在政策物件」的「可使用客服」條件加入「已確認年滿 18 歲」。
- `auth-pages`: 「登入與註冊表單」加入年齡確認勾選並與 Google 入口連動；「帳號設定頁」顯示確認狀態並提供補確認。
- `session-authentication`: 「查詢目前登入者」的回應加入「是否已確認年滿 18 歲」（只回布林值）。
- `service-observability`: 新增年齡確認事件的記錄。

## Impact

- **資料庫**：新增 migration `0003`（只新增兩個可為空的欄位，符合 expand；不回填）。升級流程仍是「備份、先執行 migration、再部署新程式」，且舊程式在新結構上可運作（忽略新欄位）。
- **程式**：`app/models/user.py`、`app/services/{auth_service,google_sign_in_service,access_policy,current_user,legal,errors}.py`、`app/observability/security_events.py`、`app/controllers/{auth_api_controller,auth_page_controller,errors}.py`、`app/security/oauth_cookie.py` 所承載的流程資料（Google 流程需多帶一個旗標）、`app/templates/auth/{register,login,account}.html`、`app/templates/legal/{terms,privacy}.html`、`app/static/js/auth/{register,login,account}.js`。
- **API**：`/api/auth/register`、`/api/auth/google/start` 新增必填欄位（BREAKING）；新增 `/api/auth/confirm-adult`；`/api/auth/me` 的回應新增 `age_confirmed`；Google 回呼新增一個固定的錯誤代碼（`age_required`）。
- **測試**：既有 8 個測試檔、共 35 處使用 `accept_terms` 的註冊呼叫需要同步補上確認；`scripts/verify_container.py` 的會員流程驗收同樣需要；README 的路由表一致性測試會要求同步新端點。
- **文件與規範**：AGENTS.md 第 10 節需新增年齡確認條文（待使用者批准）；`references/auth-security.md` 與 `references/business-encoding.md` 同步。
- **不在範圍內**：證件或第三方的年齡驗證、政策版本變更後的強制重新確認、內容分級與成年角色閘門（屬 RPG 的 RC-1 至 RC-3）、客服與 RPG 的 AI 端點本身（C-3 之後）、台灣以外地區的年齡規定。
