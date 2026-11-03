# member-registration Specification

## Purpose
定義會員資料的基本模型與帳號唯一性：以 email 為帳號識別、一組信箱（含 Gmail 的別名寫法）只能對應一個帳號、對外只用 UUID，帳號狀態只有啟用、停權與待刪除，以及服務條款與隱私權政策頁面。帳號只經由 Google 登入建立（見 `google-sign-in`），沒有密碼註冊與信箱驗證。

## Requirements

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

### Requirement: 服務條款與隱私權政策頁面
系統 SHALL 提供不需登入即可瀏覽的 `/terms` 與 `/privacy` 繁體中文頁面。隱私權政策 SHALL 告知對話內容會傳送至 Google 的 Gemini 服務、資料保存期限與當事人權利。在法務覆核完成前，兩個頁面 SHALL 清楚標示為草稿。

#### Scenario: 開啟條款頁
- **WHEN** 未登入的訪客開啟 `/terms` 與 `/privacy`
- **THEN** 回應 200 的繁體中文頁面，且頁面標示內容為草稿

#### Scenario: 隱私權政策的必要告知
- **WHEN** 檢視 `/privacy`
- **THEN** 內容包含對話傳送至 Gemini 服務的說明與各類資料的保存期限
