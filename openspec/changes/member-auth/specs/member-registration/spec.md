# Spec Delta

## Purpose

定義會員資料的基本模型與註冊行為：以信箱加密碼建立帳號、一組信箱只能對應一個帳號、註冊時必須同意條款並留下紀錄，以及信箱驗證開關在不同環境下的註冊結果。

## ADDED Requirements

### Requirement: 會員資料模型
系統 SHALL 以 email 為帳號識別，每位會員 SHALL 具有顯示名稱、角色（`user` 或 `admin`）、會員等級（`free`、`plus`、`vip`）與帳號狀態（`pending_verification`、`active`、`suspended`、`deletion_pending`）。新會員的角色 SHALL 為 `user`、等級 SHALL 為 `free`。顯示名稱 SHALL NOT 要求唯一。對外識別 SHALL 使用 UUID，內部整數編號不得出現在 API 或網址。

#### Scenario: 新帳號的預設值
- **WHEN** 成功註冊一個新帳號
- **THEN** 該帳號的角色為 `user`、會員等級為 `free`，並具有對外的 UUID 識別碼

#### Scenario: 顯示名稱重複
- **WHEN** 兩位不同信箱的使用者使用相同的顯示名稱註冊
- **THEN** 兩者皆註冊成功

### Requirement: 註冊輸入驗證
註冊 SHALL 要求 email、密碼、顯示名稱與同意條款。email SHALL 為合法的英數字格式且不超過 254 字元；密碼長度 SHALL 為 10 至 128 字且不強制字元組合；顯示名稱 SHALL 為 1 至 50 字（去除頭尾空白與控制字元後）。輸入不合法時，系統 SHALL 回應 400 與繁體中文的欄位錯誤說明，且不建立帳號。

#### Scenario: 密碼太短
- **WHEN** 以 9 個字元的密碼註冊
- **THEN** 回應 400，說明密碼長度須為 10 至 128 字，且不建立帳號

#### Scenario: 密碼不要求字元組合
- **WHEN** 以 10 個全為小寫英文字母的密碼註冊
- **THEN** 密碼被接受

#### Scenario: 未同意條款
- **WHEN** 註冊時未勾選同意服務條款與隱私權政策
- **THEN** 回應 400，不建立帳號

### Requirement: email 正規化與唯一性
email SHALL 先經過 Unicode NFC 正規化、去除頭尾空白並轉為小寫，才用於儲存與比對。對 `gmail.com` 與 `googlemail.com` 的信箱，系統 SHALL 另外去除本機部分的「.」與「+標籤」並統一網域後，產生用於重複判斷的正規形式。原始（小寫）地址 SHALL 保留供寄信使用。email 與其正規形式 SHALL 各自唯一，且行為在 SQLite 與 SQL Server 上一致。

#### Scenario: 只差大小寫
- **WHEN** 已有 `Alice@Example.com` 的帳號，又有人以 `alice@example.com` 註冊
- **THEN** 視為重複，不建立新帳號

#### Scenario: Gmail 別名
- **WHEN** 已有 `john.doe@gmail.com` 的帳號，又有人以 `johndoe+news@googlemail.com` 註冊
- **THEN** 視為重複，不建立新帳號

#### Scenario: 其他網域不去點
- **WHEN** 已有 `a.b@example.com` 的帳號，又有人以 `ab@example.com` 註冊
- **THEN** 兩者視為不同信箱，皆可註冊

#### Scenario: 帳號刪除後重新註冊
- **WHEN** 某信箱對應的帳號已被刪除，之後有人以同一信箱註冊
- **THEN** 可以註冊成功

### Requirement: 註冊回應不洩漏帳號是否存在
無論信箱是否已有帳號，註冊的回應狀態與訊息 SHALL 相同；兩種情況 SHALL 執行相同的密碼雜湊運算，且寄信 SHALL 在回應送出之後才進行，使回應時間不因此出現可辨識的差異。信箱已存在時，系統 SHALL NOT 建立新帳號，也 SHALL NOT 變更既有帳號的密碼或其他資料。

#### Scenario: 信箱已有帳號
- **WHEN** 以已被使用的信箱註冊
- **THEN** 回應與成功註冊完全相同的狀態與訊息，既有帳號的密碼與資料不變

#### Scenario: 信箱已有純 Google 帳號
- **WHEN** 以已存在純 Google 帳號（沒有密碼）的信箱註冊
- **THEN** 回應通用訊息，不建立新帳號，也不替既有帳號設定密碼

#### Scenario: 重複註冊尚未驗證的帳號
- **WHEN** 信箱已有待驗證的帳號，又有人以同一信箱註冊
- **THEN** 既有帳號的密碼不被覆蓋；若信箱驗證已啟用，系統重新寄出驗證信（受重寄頻率限制）

### Requirement: 註冊結果依信箱驗證設定而定
信箱驗證開啟時，註冊成功的帳號狀態 SHALL 為 `pending_verification`，並寄出驗證信。信箱驗證關閉時（僅限 development、testing、staging），註冊成功的帳號 SHALL 直接為 `active` 且信箱驗證時間保持空值，且不寄任何信。註冊 SHALL NOT 自動登入。

#### Scenario: 驗證開啟
- **WHEN** 信箱驗證開啟，使用者註冊成功
- **THEN** 帳號為待驗證狀態，使用者收到驗證信，且尚未登入

#### Scenario: 驗證關閉
- **WHEN** 信箱驗證關閉，使用者註冊成功
- **THEN** 帳號直接為啟用狀態、信箱驗證時間為空，不寄信，且尚未登入

### Requirement: 註冊時記錄條款同意
註冊成功時，系統 SHALL 記錄使用者同意的條款版本與同意時間。條款版本 SHALL 為目前頁面所顯示的版本。

#### Scenario: 記錄同意
- **WHEN** 使用者勾選同意並註冊成功
- **THEN** 帳號保存當時的條款版本與同意時間

### Requirement: 服務條款與隱私權政策頁面
系統 SHALL 提供不需登入即可瀏覽的 `/terms` 與 `/privacy` 繁體中文頁面。隱私權政策 SHALL 告知對話內容會傳送至 Google 的 Gemini 服務、資料保存期限與當事人權利。在法務覆核完成前，兩個頁面 SHALL 清楚標示為草稿。

#### Scenario: 開啟條款頁
- **WHEN** 未登入的訪客開啟 `/terms` 與 `/privacy`
- **THEN** 回應 200 的繁體中文頁面，且頁面標示內容為草稿

#### Scenario: 隱私權政策的必要告知
- **WHEN** 檢視 `/privacy`
- **THEN** 內容包含對話傳送至 Gemini 服務的說明與各類資料的保存期限
