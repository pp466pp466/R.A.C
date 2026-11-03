# Spec Delta

## MODIFIED Requirements

### Requirement: 會員資料模型
系統 SHALL 以 email 為帳號識別，每位會員 SHALL 具有顯示名稱、角色（`user` 或 `admin`）、會員等級（`free`、`plus`、`vip`）與帳號狀態（`active`、`suspended`、`deletion_pending`）。新會員的角色 SHALL 為 `user`、等級 SHALL 為 `free`。顯示名稱 SHALL NOT 要求唯一。對外識別 SHALL 使用 UUID，內部整數編號不得出現在 API 或網址。系統 SHALL NOT 再產生 `pending_verification` 狀態。

#### Scenario: 新帳號的預設值
- **WHEN** 玩家以 Google 首次登入並成功建立一個新帳號
- **THEN** 該帳號的角色為 `user`、會員等級為 `free`、狀態為 `active`，並具有對外的 UUID 識別碼

#### Scenario: 顯示名稱重複
- **WHEN** 兩位不同信箱的使用者的 Google 名稱相同，各自首次登入
- **THEN** 兩者皆建立成功

#### Scenario: 不再產生待驗證狀態
- **WHEN** 任何流程建立或更新帳號
- **THEN** 帳號狀態不會被設為 `pending_verification`

### Requirement: email 正規化與唯一性
email SHALL 先經過 Unicode NFC 正規化、去除頭尾空白並轉為小寫，才用於儲存與比對。對 `gmail.com` 與 `googlemail.com` 的信箱，系統 SHALL 另外去除本機部分的「.」與「+標籤」並統一網域後，產生用於重複判斷的正規形式。原始（小寫）地址 SHALL 保留供寄信使用。email 與其正規形式 SHALL 各自唯一，且行為在 SQLite 與 SQL Server 上一致。

#### Scenario: 只差大小寫
- **WHEN** 已有 `Alice@Example.com` 的帳號，又有人以 `alice@example.com` 的 Google 帳號首次登入
- **THEN** 視為同一個信箱，不建立新帳號

#### Scenario: Gmail 別名
- **WHEN** 已有 `john.doe@gmail.com` 的帳號，又有人以 `johndoe+news@googlemail.com` 的 Google 帳號首次登入
- **THEN** 視為同一個信箱，不建立新帳號

#### Scenario: 其他網域不去點
- **WHEN** 已有 `a.b@example.com` 的帳號，又有人以 `ab@example.com` 的 Google 帳號首次登入
- **THEN** 兩者視為不同信箱，各自建立帳號

#### Scenario: 帳號刪除後重新註冊
- **WHEN** 某信箱對應的帳號已被刪除，之後有人以同一信箱的 Google 帳號首次登入並勾選條款
- **THEN** 可以建立新帳號

## REMOVED Requirements

### Requirement: 註冊輸入驗證
**Reason**: 玩家不再以信箱加密碼註冊；`POST /api/auth/register` 已移除。條款同意由 `/login` 的勾選框與 Google 首次登入負責（見 `auth-pages`、`google-sign-in`），年齡確認在登入後的 `/age` 補填（見 `post-login-age-confirmation`）。
**Migration**: 沒有等價的密碼註冊輸入；顯示名稱取自 Google 名稱（見 `google-sign-in`），管理員由 `create-admin` 指令建立並沿用密碼長度規則（見 `access-control`）。

### Requirement: 註冊回應不洩漏帳號是否存在
**Reason**: 沒有註冊端點，也就沒有「註冊回應」可以洩漏帳號是否存在；Google 登入以穩定識別（sub）與已驗證信箱對應帳號，不回傳帳號是否存在。
**Migration**: 防列舉的要求由管理員密碼驗證的通用訊息承接（見 `session-authentication`、`admin-two-step-login`）。

### Requirement: 註冊結果依信箱驗證設定而定
**Reason**: 信箱驗證開關（`EMAIL_VERIFICATION_REQUIRED`）已移除；Google 首次登入建立的帳號直接為 `active` 且信箱已驗證。
**Migration**: 見 `google-sign-in` 的「首次以 Google 註冊須同意條款」與建立帳號的需求。

### Requirement: 註冊時記錄條款同意
**Reason**: 密碼註冊已移除；條款版本與同意時間改由 Google 首次登入建立帳號時記錄。
**Migration**: 見 `google-sign-in`（建立帳號時記錄條款版本與同意時間）。
