# Spec Delta

## ADDED Requirements

### Requirement: 首次以 Google 登入即建立待確認年齡的帳號
沒有帳號的 Google 身分登入時，若發起時已勾選同意條款，系統 SHALL 建立帳號（信箱已驗證、沒有密碼、記錄條款版本與同意時間、等級 `free`、角色 `user`），**不要求**生日與年齡區間，並記錄 24 小時的年齡確認期限，登入後導向年齡確認頁（見 `post-login-age-confirmation`）。發起（`/api/auth/google/start`）SHALL 只接受條款勾選旗標，SHALL NOT 要求或驗證生日與年齡區間；若請求仍帶有這兩個欄位，SHALL 忽略且不保存。沒有帳號而未勾選條款時 SHALL 不建立帳號並導向登入頁提示。

#### Scenario: 新使用者勾選條款
- **WHEN** 沒有帳號的使用者勾選條款後以 Google 登入
- **THEN** 建立待確認年齡的帳號並導向年齡確認頁，資料庫沒有生日

#### Scenario: 新使用者未勾選條款
- **WHEN** 沒有帳號的使用者沒有勾選條款就以 Google 登入
- **THEN** 不建立帳號，導向登入頁並提示須先同意條款

#### Scenario: 請求帶著生日
- **WHEN** 發起請求仍帶有 `birth_date` 與 `age_range`
- **THEN** 被忽略，交易 cookie 與資料庫都沒有這些值

### Requirement: 回訪者一鍵登入
已有帳號（已綁定 Google 身分，或以信箱合併）的使用者 SHALL 不需要勾選條款或填寫任何欄位即可登入：發起請求可以是空的，回呼成功後建立登入狀態並導向登入前要去的位置（不合法時為預設頁面）。登入頁的 Google 按鈕 SHALL 在條款未勾選時仍可按下，由伺服器依帳號是否存在決定是否接受。

#### Scenario: 回訪者不勾選
- **WHEN** 已有帳號的使用者沒有勾選條款就按 Google 登入並通過
- **THEN** 直接登入，不被要求補勾

#### Scenario: 空的發起請求
- **WHEN** 發起請求沒有任何欄位
- **THEN** 回應授權網址，狀態與 PKCE 的防護照常運作

## REMOVED Requirements

### Requirement: 首次以 Google 註冊須通過年齡驗證
**Reason**: 年齡確認改在 Google 首次登入之後才填（AGENTS.md 第 10 節），發起時不再要求或驗證生日與年齡區間。
**Migration**: 新規則見本 change 的 `首次以 Google 登入即建立待確認年齡的帳號` 與 `post-login-age-confirmation`；含生日的交易 cookie 不再存在，既有的過渡期交易（舊版發起、尚未回呼）若帶有生日，回呼時 SHALL 被忽略。
